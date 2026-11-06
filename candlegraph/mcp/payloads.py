"""Domain objects to JSON-able data. Pure: nothing here reads or writes.

A tool result crosses a protocol boundary, so two things have to be true of
every value in it and are true here rather than in each tool:

- **It survives JSON.** ``NaN`` and ``inf`` are what pandas returns for "not
  computable yet", and neither is valid JSON. :func:`finite` maps them to
  ``None`` — the same rule the rest of the codebase follows (invariant 11: a
  figure we do not have is null, never zero).
- **It is redacted.** :func:`redact_payload` runs every string through the
  agents' :func:`~candlegraph.agents.shell.redact`, because a tool can
  return a database error that quotes a DSN, exactly as a report can
  (``docs/knowledge/mcp/DESIGN.md``, "Rules the server inherits").
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pandas as pd

from candlegraph.agents.base import AgentReport
from candlegraph.agents.shell import redact
from candlegraph.analytics.pairwise import PairwiseComparison
from candlegraph.knowledge.explain import Explanation
from candlegraph.knowledge.retrieve import RetrievalResult
from candlegraph.strategies.backtest import BacktestResult
from candlegraph.strategies.base import StrategyResult

# Bars a tool returns by default when the caller does not say. Enough to see
# the recent shape of a series without pushing a megabyte through stdio.
DEFAULT_TAIL = 20


def finite(value: Any) -> float | None:
    """A float, or ``None`` for ``NaN``/``inf``/missing. Never raises."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def iso(moment: datetime | None) -> str | None:
    """ISO 8601 in UTC, or ``None``. Naive input is assumed to be UTC."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def stamp_iso(stamp: Any) -> str | None:
    """ISO 8601 for a pandas index label.

    ``DataFrame.iterrows`` types its label as ``Hashable``; every frame here
    is time-indexed, so the conversion is safe and this keeps the cast in
    one place instead of at each call site.
    """
    return iso(pd.Timestamp(stamp).to_pydatetime())


def plain(value: Any) -> Any:
    """Coerce one database or pandas value into something JSON can hold.

    Containers recurse, because a ``jsonb`` column (``ingest_run.errors``,
    ``agent_run.findings``) arrives as a nested dict and stringifying it
    would hand the caller a Python ``repr`` to parse.
    """
    if isinstance(value, Decimal | float):
        return finite(value)
    if isinstance(value, pd.Timestamp):
        return iso(value.to_pydatetime())
    if isinstance(value, datetime):
        return iso(value)
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [plain(item) for item in value]
    return str(value)


def rows_to_dicts(rows: Sequence[Mapping[str, Any]], keys: Sequence[str] | None = None) -> list[Any]:
    """Database rows as JSON-able dicts, optionally narrowed to ``keys``."""
    wanted = list(keys) if keys is not None else None
    return [
        {key: plain(row[key]) for key in (wanted if wanted is not None else row) if key in row}
        for row in rows
    ]


def redact_payload(value: Any) -> Any:
    """Mask anything credential-shaped anywhere in a result, at any depth."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {key: redact_payload(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [redact_payload(item) for item in value]
    return value


# --- analytics ---------------------------------------------------------------


def stats_payload(symbol: str, window: int, summary: pd.DataFrame) -> dict[str, Any]:
    """The latest complete row of a rolling summary, plus how it was computed."""
    latest = summary.dropna().tail(1)
    payload: dict[str, Any] = {
        "symbol": symbol,
        "window": window,
        "bars": len(summary),
        "complete_windows": len(latest),
    }
    if latest.empty:
        # `min_periods=window` (invariant 16): an incomplete window has no
        # value, and saying so beats inventing one from fewer bars.
        payload["note"] = f"not enough history for a {window}-bar window"
        return payload
    row = latest.iloc[0]
    payload |= {
        "as_of": stamp_iso(latest.index[0]),
        "price": finite(row["price"]),
        "rolling_mean": finite(row["rolling_mean"]),
        "rolling_return_per_bar": finite(row["rolling_return"]),
        "annualised_volatility": finite(row["rolling_volatility"]),
    }
    return payload


def comparison_payload(comparison: PairwiseComparison) -> dict[str, Any]:
    """A pairwise comparison, computed over aligned bars only."""
    rolling = comparison.frame[["ratio", "rolling_correlation"]].dropna().tail(DEFAULT_TAIL)
    return {
        "symbol_a": comparison.symbol_a,
        "symbol_b": comparison.symbol_b,
        "aligned_bars": comparison.bars,
        "window": comparison.window,
        "correlation": finite(comparison.correlation),
        "spearman": finite(comparison.spearman),
        "beta": finite(comparison.beta),
        "total_return_a": finite(comparison.total_return_a),
        "total_return_b": finite(comparison.total_return_b),
        "relative_return": finite(comparison.relative_return),
        "volatility_a": finite(comparison.volatility_a),
        "volatility_b": finite(comparison.volatility_b),
        "sharpe_a": finite(comparison.sharpe_a),
        "sharpe_b": finite(comparison.sharpe_b),
        "tracking_error": finite(comparison.tracking_error),
        "rolling_correlation": [
            {
                "time": stamp_iso(stamp),
                "ratio": finite(row["ratio"]),
                "correlation": finite(row["rolling_correlation"]),
            }
            for stamp, row in rolling.iterrows()
        ],
        "note": "correlation, beta and the ratio use only hours both tokens traded; nothing is filled",
    }


def strategy_payload(result: StrategyResult, tail: int = DEFAULT_TAIL) -> dict[str, Any]:
    """A strategy's target positions: the headline, then the last ``tail`` bars."""
    frame = result.frame.tail(max(0, tail))
    return {
        "strategy": result.name,
        "symbol": result.symbol,
        "bars": len(result.frame),
        "trades": result.trades(),
        "exposure": finite(result.exposure()),
        "latest_position": int(result.latest),
        "latest_position_label": result.latest.name.lower(),
        "params": {key: plain(value) for key, value in result.params.items()},
        "bars_tail": [
            {
                "time": stamp_iso(stamp),
                "price": finite(row.get("price")),
                "score": finite(row.get("score")),
                "signal": int(row["signal"]) if finite(row["signal"]) is not None else None,
            }
            for stamp, row in frame.iterrows()
        ],
    }


def backtest_payload(result: BacktestResult) -> dict[str, Any]:
    """A costed, lagged backtest. ``excess`` against buy-and-hold is the number that matters."""
    return {
        "strategy": result.strategy,
        "symbol": result.symbol,
        "bars": result.bars,
        "total_return": finite(result.total_return),
        "buy_and_hold_return": finite(result.buy_and_hold_return),
        "excess_return": finite(result.excess_return),
        "annualised_return": finite(result.annualised_return),
        "annualised_volatility": finite(result.annualised_volatility),
        "sharpe": finite(result.sharpe),
        "sortino": finite(result.sortino),
        "max_drawdown": finite(result.max_drawdown),
        "trades": result.trades,
        "exposure": finite(result.exposure),
        "win_rate": finite(result.win_rate),
        "costs_paid": finite(result.total_costs),
        "fee_bps": finite(result.config.fee_bps),
        "slippage_bps": finite(result.config.slippage_bps),
        "note": (
            "execution lags one bar and every position change pays fees and slippage; "
            "compare total_return with buy_and_hold_return before reading it as edge"
        ),
    }


# --- agents ------------------------------------------------------------------


def report_payload(report: AgentReport, markdown: bool = False) -> dict[str, Any]:
    """An agent report as data. ``markdown`` adds the rendered report too."""
    payload = dict(report.to_dict())
    if markdown:
        payload["markdown"] = report.markdown
    return payload


# --- knowledge ---------------------------------------------------------------


def retrieval_payload(result: RetrievalResult, query: str) -> dict[str, Any]:
    """Retrieved chunks with their provenance and the bound they respected."""
    return {
        "query": query,
        "bar": iso(result.bar),
        "mode": result.mode,
        "dense_used": result.dense_used,
        "note": result.note,
        "count": len(result.chunks),
        "chunks": [
            {
                "n": n,
                "chunk_id": chunk.chunk_id,
                "document_id": chunk.document_id,
                "url": chunk.url,
                "title": chunk.title,
                "published_at": iso(chunk.published_at),
                "retrieved_at": iso(chunk.retrieved_at),
                "legs": chunk.legs,
                "score": finite(chunk.score),
                "text": chunk.text,
            }
            for n, chunk in enumerate(result.chunks, 1)
        ],
    }


def explanation_payload(explanation: Explanation) -> dict[str, Any]:
    """One explained signal: the numbers, the validated prose, the citations."""
    return {
        "strategy": explanation.strategy,
        "symbol": explanation.symbol,
        "bar": iso(explanation.bar),
        "period_start_unix": explanation.period_start_unix,
        "position": explanation.position,
        "position_label": explanation.position_label,
        "score": finite(explanation.score),
        "rationale": explanation.rationale,
        "model": explanation.model,
        "generated_at": iso(explanation.generated_at),
        "numeric_context": {key: plain(value) for key, value in explanation.numeric_context.items()},
        "citations": [citation.to_dict() for citation in explanation.citations],
        "news_status": explanation.news_status,
        "news_message": explanation.news_message,
        "dense_used": explanation.dense_used,
        "retrieval_note": explanation.retrieval_note,
        "rejected_citations": list(explanation.rejected_citations),
        "note": "every [n] in the rationale is a citation below; invented ones were stripped",
    }
