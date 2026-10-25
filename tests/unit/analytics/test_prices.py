"""Loading and aligning stored rows into frames the numeric code can use."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pandas as pd

from candlegraph.analytics.prices import (
    load_price_frame,
    load_price_history,
    to_wide,
    window_bounds,
)
from tests.conftest import FakeDatabase

HOUR = 3600
BASE = 1_700_000_000 // (24 * HOUR) * (24 * HOUR)
NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)


def row(symbol: str, index: int, price: str = "100", close: str = "99") -> dict:
    return {
        "symbol": symbol,
        "token_id": f"0x{symbol.lower()}",
        "period_start_unix": BASE + index * HOUR,
        "timestamp": datetime.fromtimestamp(BASE + index * HOUR, tz=UTC),
        "open": Decimal(close),
        "high": Decimal(close),
        "low": Decimal(close),
        "close": Decimal(close),
        "price_usd": Decimal(price),
        "volume_usd": Decimal("1000"),
        "total_value_locked_usd": Decimal("5000"),
    }


# --- window bounds ---------------------------------------------------------


def test_no_window_means_everything_stored() -> None:
    """History was kept precisely so analytics could use all of it."""
    assert window_bounds(None) == (None, None)


def test_a_window_is_measured_back_from_now() -> None:
    start, end = window_bounds(7, now=NOW)

    assert end == int(NOW.timestamp())
    assert end - start == 7 * 86400


# --- loading ---------------------------------------------------------------


def test_no_symbols_skips_the_query() -> None:
    db = FakeDatabase()

    history = load_price_history(db, [])

    assert history.empty
    assert db.fetches == []


def test_the_query_carries_the_symbols_and_window() -> None:
    db = FakeDatabase()

    load_price_history(db, ["WETH", "WBTC"], start_unix=BASE, end_unix=BASE + HOUR)

    [(label, params)] = db.fetches
    assert label == "get_price_history"
    assert params == {"symbols": ["WETH", "WBTC"], "start_unix": BASE, "end_unix": BASE + HOUR}


def test_an_empty_result_is_an_empty_frame_not_an_error() -> None:
    history = load_price_history(FakeDatabase(), ["WETH"])

    assert history.empty
    assert to_wide(history).empty


def test_decimals_are_converted_to_floats() -> None:
    """psycopg2 returns Decimal, which silently breaks numpy's vectorised paths."""
    db = FakeDatabase(get_price_history=[row("WETH", 0)])

    frame = load_price_history(db, ["WETH"]).frame

    assert frame["price_usd"].dtype == "float64"
    assert frame["close"].dtype == "float64"


def test_timestamps_are_rebuilt_from_the_unix_column_in_utc() -> None:
    db = FakeDatabase(get_price_history=[row("WETH", 0)])

    frame = load_price_history(db, ["WETH"]).frame

    # pandas 3 defaults to microsecond resolution; the zone is what matters.
    assert isinstance(frame["timestamp"].dtype, pd.DatetimeTZDtype)
    assert str(frame["timestamp"].dtype.tz) == "UTC"
    assert int(frame["timestamp"].iloc[0].timestamp()) == BASE


def test_a_single_symbol_series_is_named_and_sorted() -> None:
    db = FakeDatabase(get_price_history=[row("WETH", 2), row("WETH", 0), row("WETH", 1)])

    series = load_price_history(db, ["WETH"]).close("WETH")

    assert series.name == "WETH"
    assert list(series.index) == sorted(series.index)
    assert len(series) == 3


# --- pivoting and alignment ------------------------------------------------


def test_symbols_become_columns() -> None:
    db = FakeDatabase(get_price_history=[row("WETH", 0, "100"), row("WBTC", 0, "50000")])

    wide = to_wide(load_price_history(db, ["WETH", "WBTC"]))

    assert set(wide.columns) == {"WETH", "WBTC"}
    assert wide["WETH"].iloc[0] == 100.0
    assert wide["WBTC"].iloc[0] == 50000.0


def test_a_missing_hour_becomes_an_explicit_nan_row() -> None:
    """Letting the index skip an hour would hide a gap inside a return."""
    db = FakeDatabase(get_price_history=[row("WETH", 0), row("WETH", 2)])

    wide = to_wide(load_price_history(db, ["WETH"]), fill="none")

    assert len(wide) == 3
    assert pd.isna(wide["WETH"].iloc[1])


def test_forward_fill_carries_the_last_known_price() -> None:
    db = FakeDatabase(get_price_history=[row("WETH", 0, "100"), row("WETH", 2, "120")])

    wide = to_wide(load_price_history(db, ["WETH"]), fill="ffill")

    assert wide["WETH"].iloc[1] == 100.0


def test_forward_fill_stops_after_the_limit() -> None:
    """A price stale by days is misleading, not merely approximate."""
    db = FakeDatabase(get_price_history=[row("WETH", 0), row("WETH", 10)])

    wide = to_wide(load_price_history(db, ["WETH"]), fill="ffill", max_fill=2)

    assert wide["WETH"].notna().iloc[:3].all()
    assert pd.isna(wide["WETH"].iloc[5])


def test_drop_keeps_only_hours_every_symbol_has() -> None:
    db = FakeDatabase(get_price_history=[row("WETH", 0), row("WETH", 1), row("WBTC", 1)])

    wide = to_wide(load_price_history(db, ["WETH", "WBTC"]), fill="drop")

    assert len(wide) == 1
    assert wide.notna().all().all()


def test_the_price_column_is_selectable() -> None:
    db = FakeDatabase(get_price_history=[row("WETH", 0, price="100", close="7")])

    assert to_wide(load_price_history(db, ["WETH"]), column="close")["WETH"].iloc[0] == 7.0


def test_the_index_is_utc_and_hourly() -> None:
    db = FakeDatabase(get_price_history=[row("WETH", i) for i in range(4)])

    wide = to_wide(load_price_history(db, ["WETH"]))

    assert str(wide.index.tz) == "UTC"
    assert wide.index.name == "timestamp"
    assert (wide.index.to_series().diff().dropna() == pd.Timedelta(hours=1)).all()


def test_load_price_frame_does_it_all_in_one_call() -> None:
    db = FakeDatabase(get_price_history=[row("WETH", i) for i in range(3)])

    wide = load_price_frame(db, ["WETH"], days=7, now=NOW)

    assert list(wide.columns) == ["WETH"]
    assert len(wide) == 3
    params = next(p for label, p in db.fetches if label == "get_price_history")
    assert params["start_unix"] is not None


def test_everything_is_loaded_when_no_window_is_given() -> None:
    db = FakeDatabase(get_price_history=[row("WETH", 0)])

    load_price_frame(db, ["WETH"], days=None)

    params = next(p for label, p in db.fetches if label == "get_price_history")
    assert params["start_unix"] is None
    assert params["end_unix"] is None


def test_duplicate_rows_for_one_hour_keep_the_last() -> None:
    """The natural key makes this impossible in the database; be robust anyway."""
    db = FakeDatabase(get_price_history=[row("WETH", 0, "100"), row("WETH", 0, "200")])

    wide = to_wide(load_price_history(db, ["WETH"]))

    assert len(wide) == 1
    assert wide["WETH"].iloc[0] == 200.0
