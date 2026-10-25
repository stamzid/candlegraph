"""The observer's judgement, exercised without a database.

These pin the rules the ``backfill-and-verify`` skill asks a human to apply:
a gap is only a finding on a liquid token, thin windows are "not enough
data" rather than drift, and staleness is measured against the poll interval.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from candlegraph.agents.base import Severity
from candlegraph.agents.observer.assess import (
    assess,
    assess_drift,
    assess_freshness,
    assess_ledger,
    assess_quality,
    assess_run_drift,
    drift_ratio,
    run_metrics,
    symbol_metrics,
)
from candlegraph.agents.observer.state import ObserverThresholds, RunMetrics, SymbolMetrics
from candlegraph.ingestion.quality import Gap, QualityReport, TokenQuality
from tests.unit.agents.conftest import run_row

HOUR = 3600
T = ObserverThresholds(poll_interval=300, stale_intervals=3)  # stale after 900s
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def metrics(
    symbol: str = "WETH",
    recent_bars: int = 24,
    baseline_bars: int = 168,
    recent_volume: float | None = 1e6,
    baseline_volume: float | None = 1e6,
    baseline_mean: float | None = None,
    recent_vol: float | None = 0.01,
    baseline_vol: float | None = 0.01,
) -> SymbolMetrics:
    return SymbolMetrics(
        symbol,
        recent_bars,
        baseline_bars,
        recent_volume,
        baseline_volume,
        baseline_mean if baseline_mean is not None else baseline_volume,
        recent_vol,
        baseline_vol,
    )


def quality(*tokens: TokenQuality, missing: list[str] | None = None, violations: int = 0) -> QualityReport:
    return QualityReport(
        generated_at=NOW,
        tokens=list(tokens),
        missing_tokens=missing or [],
        violations=[{"token_id": "x"}] * violations,
        min_expected_coverage=0.8,
    )


def token(symbol: str, stored: int = 168, expected: int = 168, gap_hours: int = 0) -> TokenQuality:
    gaps = [Gap(1_700_000_000, 1_700_000_000 + (gap_hours - 1) * HOUR)] if gap_hours else []
    return TokenQuality(f"0x{symbol}", symbol, stored, expected, 1_690_000_000, 1_700_000_000, gaps)


# --- freshness -------------------------------------------------------------


def test_fresh_data_is_an_ok_finding_that_says_how_old() -> None:
    (f,) = assess_freshness(240.0, [run_row(NOW)], T)

    assert f.severity == Severity.OK and f.kind == "fresh"
    assert "4m ago" in f.message


def test_stale_data_warns_and_doubly_stale_is_critical() -> None:
    assert assess_freshness(901.0, [run_row(NOW)], T)[0].severity == Severity.WARNING
    assert assess_freshness(1801.0, [run_row(NOW)], T)[0].severity == Severity.CRITICAL


def test_no_success_ever_is_critical_but_an_empty_ledger_is_just_new() -> None:
    assert assess_freshness(None, [], T)[0].severity == Severity.INFO
    assert assess_freshness(None, [run_row(NOW, status="failed")], T)[0].severity == Severity.CRITICAL


# --- ledger ----------------------------------------------------------------


def test_a_failure_streak_is_critical_and_a_single_failure_is_a_warning() -> None:
    failed = [
        run_row(NOW - timedelta(minutes=5 * i), status="failed", errors={"WETH": "auth"}) for i in range(3)
    ]

    streak = assess_ledger(failed, T)
    assert streak[0].severity == Severity.CRITICAL and streak[0].kind == "failed_runs"
    assert "auth" in streak[0].message

    single = assess_ledger([failed[0], run_row(NOW - timedelta(minutes=5))], T)
    assert single[0].severity == Severity.WARNING


def test_a_partial_run_names_the_targets_that_failed() -> None:
    (partial, _summary) = assess_ledger(
        [run_row(NOW, status="partial", errors={"GNO": "timeout", "SHIB": "x"})], T
    )

    assert partial.severity == Severity.WARNING and partial.kind == "partial_run"
    assert "GNO, SHIB" in partial.message


def test_the_ledger_summary_counts_finished_runs_and_ignores_running_ones() -> None:
    runs = [run_row(NOW, status="running"), run_row(NOW, status="succeeded"), run_row(NOW, status="partial")]

    *_, summary = assess_ledger(runs, T)
    assert summary.severity == Severity.OK
    assert summary.evidence["counts"] == {"succeeded": 1, "partial": 1, "failed": 0}
    assert assess_ledger([run_row(NOW, status="running")], T) == []


def test_errors_stored_as_a_json_string_are_decoded() -> None:
    (partial, _) = assess_ledger([{**run_row(NOW, status="partial"), "errors": '{"UNI": "500"}'}], T)

    assert "UNI" in partial.message


# --- quality ---------------------------------------------------------------


def test_a_clean_report_is_one_ok_coverage_finding() -> None:
    (f,) = assess_quality(quality(token("WETH"), token("WBTC")), {}, T)

    assert f.severity == Severity.OK and "2 tokens, 100.0% coverage" in f.message


def test_missing_tokens_and_impossible_rows_are_critical() -> None:
    findings = assess_quality(quality(token("WETH"), missing=["GNO"], violations=2), {}, T)

    kinds = {f.kind: f.severity for f in findings}
    assert kinds["missing_token"] == Severity.CRITICAL
    assert kinds["impossible_rows"] == Severity.CRITICAL


def test_low_coverage_warns_and_says_whether_the_token_is_thin() -> None:
    thin = token("GNO", stored=80, expected=168)
    liquid = token("WETH", stored=80, expected=168)
    m = {"GNO": metrics("GNO", baseline_mean=400.0), "WETH": metrics("WETH", baseline_mean=5e6)}

    findings = {f.subject: f for f in assess_quality(quality(thin, liquid), m, T)}

    assert "thin token" in findings["GNO"].message and findings["GNO"].evidence["liquid"] is False
    assert "liquid token" in findings["WETH"].message and findings["WETH"].evidence["liquid"] is True


def test_a_long_gap_is_a_finding_on_a_liquid_token_and_not_on_a_thin_one() -> None:
    """Invariant 19: an hour with no swaps has no row. GNO going quiet is the market."""
    gapped_weth = token("WETH", gap_hours=7)
    gapped_gno = token("GNO", gap_hours=7)
    m = {"WETH": metrics("WETH", baseline_mean=5e6), "GNO": metrics("GNO", baseline_mean=400.0)}

    findings = [f for f in assess_quality(quality(gapped_weth, gapped_gno), m, T) if f.kind == "gap"]

    assert [f.subject for f in findings] == ["WETH"]
    assert findings[0].severity == Severity.WARNING and "7h gap" in findings[0].message


def test_a_short_gap_on_a_liquid_token_is_not_a_finding() -> None:
    m = {"WETH": metrics("WETH", baseline_mean=5e6)}

    findings = assess_quality(quality(token("WETH", gap_hours=5)), m, T)

    assert [f.kind for f in findings] == ["coverage"] and findings[0].severity == Severity.OK


def test_no_report_means_no_quality_findings() -> None:
    assert assess_quality(None, {}, T) == []


# --- drift -----------------------------------------------------------------


def test_drift_ratio_is_none_without_both_sides_or_with_a_zero_baseline() -> None:
    assert drift_ratio(None, 1.0) is None
    assert drift_ratio(1.0, None) is None
    assert drift_ratio(1.0, 0.0) is None
    assert drift_ratio(2.0, 1.0) == 2.0


def test_volume_five_times_baseline_is_drift_and_twice_is_not() -> None:
    drifted = assess_drift({"SHIB": metrics("SHIB", recent_volume=5e6, baseline_volume=1e6)}, T)
    steady = assess_drift({"SHIB": metrics("SHIB", recent_volume=2e6, baseline_volume=1e6)}, T)

    assert drifted[0].severity == Severity.WARNING and "x5.0" in drifted[0].message
    assert drifted[0].evidence["metric"] == "volume_usd"
    assert [f.severity for f in steady] == [Severity.OK]


def test_a_collapse_in_volume_is_drift_too() -> None:
    (f,) = [
        f
        for f in assess_drift({"UNI": metrics("UNI", recent_volume=1e5, baseline_volume=1e6)}, T)
        if f.severity > Severity.OK
    ]

    assert f.kind == "drift" and f.evidence["ratio"] == 0.1


def test_volatility_tripling_is_drift() -> None:
    findings = assess_drift({"WETH": metrics("WETH", recent_vol=0.04, baseline_vol=0.01)}, T)

    assert findings[0].evidence["metric"] == "volatility" and findings[0].severity == Severity.WARNING


def test_a_thin_window_is_not_enough_data_never_drift() -> None:
    findings = assess_drift({"GNO": metrics("GNO", recent_bars=3, recent_volume=9e9, baseline_volume=1.0)}, T)

    assert [f.severity for f in findings] == [Severity.INFO]
    assert "GNO (3/168)" in findings[0].message


def test_drift_severity_does_not_escalate_with_the_ratio() -> None:
    (f,) = [
        f
        for f in assess_drift({"X": metrics("X", recent_volume=1e9, baseline_volume=1.0)}, T)
        if f.kind == "drift" and f.severity > Severity.OK
    ]

    assert f.severity == Severity.WARNING


# --- run drift -------------------------------------------------------------


def test_run_drift_needs_two_runs_a_side() -> None:
    assert assess_run_drift(RunMetrics(1, 10, 100.0, 10.0, 30.0, 30.0), T) == []
    assert assess_run_drift(None, T) == []


def test_slower_runs_and_a_very_different_row_count_are_drift() -> None:
    slow = assess_run_drift(RunMetrics(3, 10, 100.0, 10.0, 30.0, 30.0), T)
    rows = assess_run_drift(RunMetrics(3, 10, 10.0, 10.0, 5.0, 30.0), T)

    assert slow[0].evidence["metric"] == "duration_seconds" and "x10.0" in slow[0].message
    assert rows[0].evidence["metric"] == "token_hours_written"


def test_faster_runs_are_not_drift() -> None:
    assert assess_run_drift(RunMetrics(3, 10, 2.0, 10.0, 30.0, 30.0), T) == []


# --- metric computation ----------------------------------------------------


def frame(
    hours: int, start_unix: int, price: float = 100.0, volume: float = 1e6, noise: float = 0.0
) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    prices = price * np.exp(np.cumsum(rng.normal(0, noise, hours)))
    return pd.DataFrame(
        {
            "symbol": "WETH",
            "period_start_unix": [start_unix + i * HOUR for i in range(hours)],
            "price_usd": prices,
            "volume_usd": volume,
        }
    )


def test_symbol_metrics_split_the_rows_at_the_window_boundary() -> None:
    start = 1_700_000_000
    rows = pd.concat([frame(168, start, volume=1e6), frame(24, start + 168 * HOUR, volume=3e6)])

    m = symbol_metrics("WETH", rows, split_unix=start + 168 * HOUR)

    assert (m.recent_bars, m.baseline_bars) == (24, 168)
    assert m.recent_volume_median == pytest.approx(3e6)
    assert m.baseline_volume_median == pytest.approx(1e6)
    assert m.baseline_volume_mean == pytest.approx(1e6)


def test_volatility_is_the_sample_std_of_log_returns_and_none_below_two_returns() -> None:
    start = 1_700_000_000
    rows = frame(48, start, noise=0.02)

    m = symbol_metrics("WETH", rows, split_unix=start + 24 * HOUR)
    expected = np.diff(np.log(rows["price_usd"].to_numpy()[24:])).std(ddof=1)

    assert m.recent_volatility == pytest.approx(expected)
    assert symbol_metrics("WETH", frame(2, start), split_unix=start).recent_volatility is None


def test_a_constant_price_has_zero_volatility_not_none() -> None:
    m = symbol_metrics("WETH", frame(24, 1_700_000_000), split_unix=1_700_000_000)

    assert m.recent_volatility == 0.0


def test_run_metrics_exclude_backfills_and_running_runs() -> None:
    split = NOW - timedelta(hours=24)
    runs = [
        run_row(NOW - timedelta(hours=1), duration=10, token_hours=30),
        run_row(NOW - timedelta(hours=2), duration=20, token_hours=50),
        run_row(NOW - timedelta(hours=3), trigger="backfill", duration=900, token_hours=9000),
        run_row(NOW - timedelta(hours=4), status="running", duration=None),  # type: ignore[arg-type]
        run_row(NOW - timedelta(days=2), duration=12, token_hours=30),
        run_row(NOW - timedelta(days=30), duration=1, token_hours=1),  # outside the baseline
    ]

    rm = run_metrics(runs, split_at=split, baseline_start=split - timedelta(days=7))

    assert (rm.recent_runs, rm.baseline_runs) == (2, 1)
    assert rm.recent_duration_median == 15.0 and rm.recent_rows_median == 40.0
    assert rm.baseline_duration_median == 12.0


# --- the node --------------------------------------------------------------


def test_the_assess_node_combines_every_assessment_and_reports_collect_errors() -> None:
    state = {
        "thresholds": T,
        "runs": [run_row(NOW)],
        "seconds_since_success": 60.0,
        "quality": quality(token("WETH")),
        "metrics": {"WETH": metrics()},
        "run_metrics": None,
        "errors": ["metrics: boom"],
    }

    findings = assess(state)["findings"]  # type: ignore[arg-type]

    kinds = [f.kind for f in findings]
    assert kinds == ["fresh", "ledger", "coverage", "drift", "error"]
    assert findings[-1].severity == Severity.WARNING and "boom" in findings[-1].message
