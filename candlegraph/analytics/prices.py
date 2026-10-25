"""Loading stored hourly rows into aligned pandas frames.

The only database-touching module in :mod:`candlegraph.analytics`, and it only
reads. Everything downstream takes a frame, so the numeric code stays testable
without a database.

Alignment is the whole job here. Tokens have genuinely different trading
hours — a thin token emits no row for an hour with no swaps — so a naive
join produces a ragged matrix, and a covariance estimated from ragged windows
is not a covariance. :func:`load_price_frame` resolves that explicitly and
says which policy it used.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import pandas as pd

from candlegraph.storage import DatabaseManager

PriceColumn = Literal["close", "price_usd", "open", "high", "low"]
FillPolicy = Literal["ffill", "drop", "none"]

HOUR = 3600


@dataclass(frozen=True)
class PriceHistory:
    """Per-symbol hourly rows, long format, plus how they were fetched."""

    frame: pd.DataFrame
    symbols: tuple[str, ...]
    start_unix: int | None
    end_unix: int | None

    @property
    def empty(self) -> bool:
        return self.frame.empty

    def for_symbol(self, symbol: str) -> pd.DataFrame:
        """One symbol's rows, indexed by UTC timestamp."""
        rows = self.frame[self.frame["symbol"] == symbol]
        return rows.set_index("timestamp").sort_index()

    def close(self, symbol: str, column: PriceColumn = "price_usd") -> pd.Series:
        """One symbol's price series, indexed by UTC timestamp."""
        series = self.for_symbol(symbol)[column].astype("float64")
        series.name = symbol
        return series


def window_bounds(
    days: int | None = None,
    end: datetime | None = None,
    now: datetime | None = None,
) -> tuple[int | None, int | None]:
    """Unix bounds for a trailing window of ``days``.

    ``None`` days means "everything stored", which is the default for
    analytics: history was kept precisely so it could be used.
    """
    if days is None:
        return None, None
    finish = end or now or datetime.now(tz=UTC)
    return int((finish - timedelta(days=days)).timestamp()), int(finish.timestamp())


def load_price_history(
    db: DatabaseManager,
    symbols: list[str],
    start_unix: int | None = None,
    end_unix: int | None = None,
) -> PriceHistory:
    """Read hourly rows for the given symbols.

    Returns:
        A :class:`PriceHistory`; ``frame`` is empty when nothing is stored
        rather than raising, so callers can report "no data" themselves.
    """
    if not symbols:
        return PriceHistory(_empty_frame(), (), start_unix, end_unix)

    rows = db.fetch_all(
        db.sql.get_price_history,
        {"symbols": list(symbols), "start_unix": start_unix, "end_unix": end_unix},
    )
    frame = _empty_frame() if not rows else _to_frame(rows)
    return PriceHistory(frame, tuple(symbols), start_unix, end_unix)


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": pd.Series(dtype="object"),
            "timestamp": pd.Series(dtype="datetime64[ns, UTC]"),
            "period_start_unix": pd.Series(dtype="int64"),
            "close": pd.Series(dtype="float64"),
            "price_usd": pd.Series(dtype="float64"),
        }
    )


def _to_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    # Everything arrives as Decimal from psycopg2; GraphQL and numpy both
    # want floats, and Decimal silently breaks numpy's vectorised paths.
    for column in ("open", "high", "low", "close", "price_usd", "volume_usd", "total_value_locked_usd"):
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("float64")
    frame["period_start_unix"] = frame["period_start_unix"].astype("int64")
    frame["timestamp"] = pd.to_datetime(frame["period_start_unix"], unit="s", utc=True)
    return frame


def to_wide(
    history: PriceHistory,
    column: PriceColumn = "price_usd",
    fill: FillPolicy = "ffill",
    max_fill: int | None = 24,
) -> pd.DataFrame:
    """Pivot long rows into one column per symbol on a shared hourly index.

    Args:
        history: Rows from :func:`load_price_history`.
        column: Which price to take.
        fill: How to resolve hours a symbol is missing.
            ``"ffill"`` carries the last known price forward, which is the
            right assumption for an asset that simply did not trade;
            ``"drop"`` keeps only hours every symbol has, which is strict but
            can throw away most of a thin token's history; ``"none"`` leaves
            the holes in place.
        max_fill: Cap on consecutive forward-filled hours. Beyond this the
            last price is stale enough to be misleading, so the hole stays.
            ``None`` fills without limit.

    Returns:
        A frame indexed by UTC timestamp with one float column per symbol,
        reindexed onto a complete hourly grid so gaps are visible as ``NaN``
        rather than silently closed up.
    """
    if history.empty:
        return pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC", name="timestamp"))

    wide = history.frame.pivot_table(
        index="timestamp", columns="symbol", values=column, aggfunc="last"
    ).sort_index()
    wide.columns.name = None

    # A complete grid makes a missing hour explicit instead of letting the
    # index skip it, which would make returns span a gap without saying so.
    full_index = pd.date_range(wide.index.min(), wide.index.max(), freq="h", tz="UTC")
    wide = wide.reindex(full_index)
    wide.index.name = "timestamp"

    match fill:
        case "ffill":
            wide = wide.ffill(limit=max_fill)
        case "drop":
            wide = wide.dropna(how="any")
        case "none":
            pass

    return wide.astype("float64")


def load_price_frame(
    db: DatabaseManager,
    symbols: list[str],
    days: int | None = None,
    column: PriceColumn = "price_usd",
    fill: FillPolicy = "ffill",
    now: datetime | None = None,
) -> pd.DataFrame:
    """Load a wide price frame in one call. The common entry point."""
    start_unix, end_unix = window_bounds(days, now=now)
    history = load_price_history(db, symbols, start_unix, end_unix)
    return to_wide(history, column=column, fill=fill)
