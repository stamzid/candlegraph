"""Persisting generated signals to the ``signal`` table.

Same rule as every other write in this repo: upsert on the natural key
``(strategy, symbol, period_start_unix)``, so re-running a strategy over an
overlapping window refreshes those bars instead of duplicating them.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from candlegraph.storage import DatabaseManager
from candlegraph.strategies.base import StrategyResult
from candlegraph.utils.logging_utils import service_logger


def _score_of(value: Any) -> float | None:
    """A NaN score is "no opinion"; store NULL, which SQL can reason about."""
    if value is None:
        return None
    number = float(value)
    return None if math.isnan(number) or math.isinf(number) else number


def signals_to_rows(
    result: StrategyResult,
    symbol: str | None = None,
    generated_at: datetime | None = None,
    include_flat: bool = True,
) -> list[dict[str, Any]]:
    """Turn a strategy result into rows the signal upsert accepts.

    Args:
        result: The strategy output.
        symbol: Overrides ``result.symbol``, for when the series was unnamed.
        generated_at: Stamp for the whole batch; defaults to now.
        include_flat: When False, only non-flat bars are stored. Useful for a
            sparse signal table; the default keeps the full history so a
            backtest can be reconstructed from the table alone.

    Returns:
        One row per bar, in index order.
    """
    stamped = generated_at or datetime.now(tz=UTC)
    name = symbol or result.symbol
    if not name:
        raise ValueError("cannot store signals without a symbol")

    params = json.dumps(result.params, default=str)
    frame = result.frame
    index = pd.DatetimeIndex(frame.index)

    rows: list[dict[str, Any]] = []
    for timestamp, signal, score in zip(
        index, frame["signal"].to_numpy(), frame["score"].to_numpy(), strict=True
    ):
        position = int(signal)
        if not include_flat and position == 0:
            continue
        moment = timestamp.to_pydatetime()
        rows.append(
            {
                "strategy": result.name,
                "symbol": name,
                "period_start_unix": int(moment.timestamp()),
                "timestamp": moment,
                "signal": position,
                "score": _score_of(score),
                "params": params,
                "generated_at": stamped,
            }
        )
    return rows


def store_signals(
    db: DatabaseManager,
    result: StrategyResult,
    symbol: str | None = None,
    generated_at: datetime | None = None,
    include_flat: bool = True,
) -> int:
    """Write a strategy's signals, returning the number of rows upserted."""
    rows = signals_to_rows(result, symbol, generated_at, include_flat)
    if not rows:
        service_logger.warning("Strategy %s produced no signals to store", result.name)
        return 0
    written = db.upsert_many(db.sql.insert_signal, db.sql.insert_signal_template, rows)
    service_logger.info("Stored %d %s signals for %s", written, result.name, rows[0]["symbol"])
    return written
