"""From collected facts to findings. Pure: no database, no clock, no I/O.

Each ``assess_*`` function takes what a collect node stored plus the
thresholds and returns findings. The rules they encode are the ones the
``backfill-and-verify`` skill tells a human to apply, which is the point: the
observer applies them the same way every fifteen minutes.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from candlegraph.agents.base import Finding, Severity
from candlegraph.agents.observer.state import ObserverState, ObserverThresholds, RunMetrics, SymbolMetrics
from candlegraph.analytics.rolling import log_returns
from candlegraph.ingestion.quality import QualityReport

# --- metric computation ----------------------------------------------------


def _median(values: pd.Series) -> float | None:
    clean = values.dropna()
    return float(clean.median()) if len(clean) else None


def _mean(values: pd.Series) -> float | None:
    clean = values.dropna()
    return float(clean.mean()) if len(clean) else None


def _volatility(prices: pd.Series) -> float | None:
    """Standard deviation of hourly log returns, ``ddof=1``; None below two returns."""
    returns = log_returns(prices).dropna()
    return float(returns.std(ddof=1)) if len(returns) >= 2 else None


def symbol_metrics(symbol: str, rows: pd.DataFrame, split_unix: int) -> SymbolMetrics:
    """Split one token's rows at ``split_unix`` and summarise both sides.

    ``rows`` is the long frame from :func:`~candlegraph.analytics.prices.load_price_history`
    filtered to one symbol: ``period_start_unix``, ``price_usd``, ``volume_usd``.
    Returns span whatever hours are present; a gap-spanning return is one
    observation, not several, which is the honest reading of sparse data.
    """
    ordered = rows.sort_values("period_start_unix")
    recent = ordered[ordered["period_start_unix"] >= split_unix]
    baseline = ordered[ordered["period_start_unix"] < split_unix]
    volume_col = "volume_usd" if "volume_usd" in ordered else None

    def volume(frame: pd.DataFrame) -> pd.Series:
        if volume_col is None:
            return pd.Series(dtype="float64")
        return pd.to_numeric(frame[volume_col], errors="coerce")

    return SymbolMetrics(
        symbol=symbol,
        recent_bars=len(recent),
        baseline_bars=len(baseline),
        recent_volume_median=_median(volume(recent)),
        baseline_volume_median=_median(volume(baseline)),
        baseline_volume_mean=_mean(volume(baseline)),
        recent_volatility=_volatility(recent["price_usd"].astype("float64")),
        baseline_volatility=_volatility(baseline["price_usd"].astype("float64")),
    )


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return None


def run_metrics(runs: Sequence[dict[str, Any]], split_at: datetime, baseline_start: datetime) -> RunMetrics:
    """Median duration and rows written, recent runs against baseline runs.

    Backfills are excluded: a 90-day backfill writes a thousand times what a
    poll does, and a single one would read as drift in either direction.
    """
    recent: list[dict[str, Any]] = []
    baseline: list[dict[str, Any]] = []
    for run in runs:
        started = _as_datetime(run.get("started_at"))
        if started is None or run.get("status") == "running" or run.get("trigger") == "backfill":
            continue
        if started >= split_at:
            recent.append(run)
        elif started >= baseline_start:
            baseline.append(run)

    def median_of(window: list[dict[str, Any]], key: str) -> float | None:
        values = [float(run[key]) for run in window if run.get(key) is not None]
        return statistics.median(values) if values else None

    return RunMetrics(
        recent_runs=len(recent),
        baseline_runs=len(baseline),
        recent_duration_median=median_of(recent, "duration_seconds"),
        baseline_duration_median=median_of(baseline, "duration_seconds"),
        recent_rows_median=median_of(recent, "token_hours_written"),
        baseline_rows_median=median_of(baseline, "token_hours_written"),
    )


def drift_ratio(recent: float | None, baseline: float | None) -> float | None:
    """``recent / baseline``, or None when either side is missing or the baseline is zero."""
    if recent is None or baseline is None or baseline <= 0:
        return None
    return recent / baseline


def _outside(ratio: float | None, k: float) -> bool:
    return ratio is not None and (ratio > k or ratio < 1 / k)


def _humanise(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172_800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86_400:.1f}d"


# --- assessments -----------------------------------------------------------


def assess_freshness(
    age: float | None, runs: Sequence[dict[str, Any]], t: ObserverThresholds
) -> list[Finding]:
    """Stale data: time since the last run that wrote something."""
    if age is None:
        if not runs:
            return [
                Finding("stale", Severity.INFO, "ingest", "no ingest runs recorded yet; the stack may be new")
            ]
        return [
            Finding(
                "stale",
                Severity.CRITICAL,
                "ingest",
                f"no successful ingest run has ever been recorded ({len(runs)} runs in the ledger)",
                {"runs": len(runs)},
            )
        ]
    evidence = {"seconds_since_success": round(age), "stale_after_seconds": t.stale_after_seconds}
    stale_message = (
        f"last successful ingest was {_humanise(age)} ago; stale after {_humanise(t.stale_after_seconds)}"
    )
    if age > 2 * t.stale_after_seconds:
        return [
            Finding(
                "stale",
                Severity.CRITICAL,
                "ingest",
                stale_message,
                evidence,
            )
        ]
    if age > t.stale_after_seconds:
        return [
            Finding(
                "stale",
                Severity.WARNING,
                "ingest",
                stale_message,
                evidence,
            )
        ]
    return [
        Finding("fresh", Severity.OK, "ingest", f"last successful ingest was {_humanise(age)} ago", evidence)
    ]


def _errors_of(run: dict[str, Any]) -> dict[str, str]:
    raw = run.get("errors") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {"?": raw}
    return {str(k): str(v) for k, v in dict(raw).items()}


def assess_ledger(runs: Sequence[dict[str, Any]], t: ObserverThresholds) -> list[Finding]:
    """Failure streaks and partial runs. ``runs`` is newest first."""
    finished = [run for run in runs if run.get("status") != "running"]
    if not finished:
        return []
    findings: list[Finding] = []
    streak = finished[: t.failure_streak]
    if len(streak) >= t.failure_streak and all(run.get("status") == "failed" for run in streak):
        findings.append(
            Finding(
                "failed_runs",
                Severity.CRITICAL,
                "ingest",
                f"the last {len(streak)} ingest runs all failed: {_errors_of(streak[0])}",
                {"streak": len(streak), "last_errors": _errors_of(streak[0])},
            )
        )
    elif finished[0].get("status") == "failed":
        findings.append(
            Finding(
                "failed_runs",
                Severity.WARNING,
                "ingest",
                f"the most recent ingest run failed: {_errors_of(finished[0])}",
                {"last_errors": _errors_of(finished[0])},
            )
        )
    elif finished[0].get("status") == "partial":
        errors = _errors_of(finished[0])
        findings.append(
            Finding(
                "partial_run",
                Severity.WARNING,
                "ingest",
                f"the most recent ingest run was partial; failed targets: {', '.join(sorted(errors)) or '?'}",
                {"last_errors": errors},
            )
        )
    counts = {
        status: sum(1 for run in finished if run.get("status") == status)
        for status in ("succeeded", "partial", "failed")
    }
    findings.append(
        Finding(
            "ledger",
            Severity.OK,
            "ingest",
            f"{len(finished)} finished runs in the window: "
            f"{counts['succeeded']} succeeded, {counts['partial']} partial, {counts['failed']} failed",
            {"counts": counts},
        )
    )
    return findings


def assess_quality(
    quality: QualityReport | None, metrics: dict[str, SymbolMetrics], t: ObserverThresholds
) -> list[Finding]:
    """Missing tokens, impossible rows, coverage, and gaps that matter.

    A gap is only a finding on a *liquid* token. Invariant 19: an hour with
    no swaps has no row, so a long quiet spell on a thin token is the market.
    """
    if quality is None:
        return []
    findings: list[Finding] = []
    for symbol in quality.missing_tokens:
        findings.append(
            Finding("missing_token", Severity.CRITICAL, symbol, "configured token has no stored rows at all")
        )
    if quality.violations:
        findings.append(
            Finding(
                "impossible_rows",
                Severity.CRITICAL,
                "token_hours_data",
                f"{len(quality.violations)} rows with high < low or OHLC outside the bar",
                {"rows": quality.violations[:5]},
            )
        )
    for token in quality.under_covered:
        m = metrics.get(token.symbol)
        liquid = (m.baseline_volume_mean or 0.0) >= t.liquid_volume_usd if m else False
        note = "liquid token; check make runs" if liquid else "thin token; likely the market"
        findings.append(
            Finding(
                "coverage",
                Severity.WARNING,
                token.symbol,
                f"coverage {token.coverage:.1%} is below {t.min_expected_coverage:.0%} ({note})",
                {"coverage": round(token.coverage, 4), "liquid": liquid, "gaps": len(token.gaps)},
            )
        )
    for token in quality.tokens:
        longest = token.longest_gap
        if longest is None or longest.hours < t.suspicious_gap_hours:
            continue
        m = metrics.get(token.symbol)
        volume = m.baseline_volume_mean if m else None
        if volume is not None and volume >= t.liquid_volume_usd:
            findings.append(
                Finding(
                    "gap",
                    Severity.WARNING,
                    token.symbol,
                    f"{longest.hours}h gap {longest} on a liquid token (mean hourly volume ${volume:,.0f})",
                    {"gap_hours": longest.hours, "start_unix": longest.start_unix, "mean_volume_usd": volume},
                )
            )
    if quality.tokens and not any(f.severity >= Severity.WARNING for f in findings):
        stored = sum(tok.stored_hours for tok in quality.tokens)
        expected = sum(tok.expected_hours for tok in quality.tokens)
        overall = stored / expected if expected else 0.0
        findings.append(
            Finding(
                "coverage",
                Severity.OK,
                "all",
                f"{len(quality.tokens)} tokens, {overall:.1%} coverage, no impossible rows",
                {"coverage": round(overall, 4), "tokens": len(quality.tokens)},
            )
        )
    return findings


def assess_drift(metrics: dict[str, SymbolMetrics], t: ObserverThresholds) -> list[Finding]:
    """Volume and volatility, recent window against baseline, by ratio of robust statistics."""
    findings: list[Finding] = []
    thin: list[str] = []
    judged = 0
    for symbol, m in sorted(metrics.items()):
        if m.recent_bars < t.min_bars or m.baseline_bars < t.min_bars:
            thin.append(f"{symbol} ({m.recent_bars}/{m.baseline_bars})")
            continue
        judged += 1
        volume = drift_ratio(m.recent_volume_median, m.baseline_volume_median)
        if _outside(volume, t.volume_drift_ratio) and volume is not None:
            findings.append(
                Finding(
                    "drift",
                    Severity.WARNING,
                    symbol,
                    f"median hourly volume x{volume:.1f} vs baseline "
                    f"(${m.baseline_volume_median or 0:,.0f} → ${m.recent_volume_median or 0:,.0f})",
                    {"metric": "volume_usd", "ratio": round(volume, 3), **m.to_dict()},
                )
            )
        sigma = drift_ratio(m.recent_volatility, m.baseline_volatility)
        if _outside(sigma, t.volatility_drift_ratio) and sigma is not None:
            findings.append(
                Finding(
                    "drift",
                    Severity.WARNING,
                    symbol,
                    f"realised volatility x{sigma:.1f} vs baseline "
                    f"({(m.baseline_volatility or 0):.4f} → {(m.recent_volatility or 0):.4f} per bar)",
                    {"metric": "volatility", "ratio": round(sigma, 3), **m.to_dict()},
                )
            )
    if thin:
        findings.append(
            Finding(
                "drift",
                Severity.INFO,
                "thin",
                f"not enough bars to judge drift (need {t.min_bars} recent/baseline): {', '.join(thin)}",
                {"min_bars": t.min_bars, "symbols": thin},
            )
        )
    if judged and not any(f.kind == "drift" and f.severity >= Severity.WARNING for f in findings):
        findings.append(
            Finding(
                "drift",
                Severity.OK,
                "all",
                f"no volume or volatility drift across {judged} symbols "
                f"(limits x{t.volume_drift_ratio:g} / x{t.volatility_drift_ratio:g})",
                {"judged": judged},
            )
        )
    return findings


def assess_run_drift(rm: RunMetrics | None, t: ObserverThresholds) -> list[Finding]:
    """Ingest runs getting slower or writing a very different amount."""
    if rm is None or rm.recent_runs < 2 or rm.baseline_runs < 2:
        return []
    findings: list[Finding] = []
    duration = drift_ratio(rm.recent_duration_median, rm.baseline_duration_median)
    if duration is not None and duration > t.run_drift_ratio:
        findings.append(
            Finding(
                "run_drift",
                Severity.WARNING,
                "ingest",
                f"ingest runs take x{duration:.1f} longer than baseline "
                f"({rm.baseline_duration_median:.0f}s → {rm.recent_duration_median:.0f}s median)",
                {"metric": "duration_seconds", "ratio": round(duration, 3)},
            )
        )
    rows = drift_ratio(rm.recent_rows_median, rm.baseline_rows_median)
    if _outside(rows, t.run_drift_ratio) and rows is not None:
        findings.append(
            Finding(
                "run_drift",
                Severity.WARNING,
                "ingest",
                f"token hours written per run x{rows:.1f} vs baseline "
                f"({rm.baseline_rows_median:.0f} → {rm.recent_rows_median:.0f} median)",
                {"metric": "token_hours_written", "ratio": round(rows, 3)},
            )
        )
    return findings


def assess(state: ObserverState) -> dict[str, Any]:
    """The graph node: every assessment over the collected state."""
    t = state["thresholds"]
    metrics = state.get("metrics", {})
    runs = state.get("runs", [])
    findings = [
        *assess_freshness(state.get("seconds_since_success"), runs, t),
        *assess_ledger(runs, t),
        *assess_quality(state.get("quality"), metrics, t),
        *assess_drift(metrics, t),
        *assess_run_drift(state.get("run_metrics"), t),
    ]
    for error in state.get("errors", []):
        findings.append(Finding("error", Severity.WARNING, "observer", error))
    return {"findings": findings}
