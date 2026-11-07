"""The derived-fill cache: what it stores, and what it refuses to touch.

The arithmetic belongs to `analytics.prices.apply_fill` and is tested there.
What this covers is the part that can corrupt something: which rows are
written, that `token_hours_data` is never one of them, and that a superseded
estimate is removed rather than left to shadow a real observation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pandas as pd
import pytest

from candlegraph.ingestion.fills import (
    LINEAR,
    fill_cache_summary,
    fill_rows,
    load_fills,
    method_is_causal,
    refresh_fill_cache,
)
from tests.conftest import FakeDatabase

HOUR = 3600
BASE = 1_700_000_000 // (24 * HOUR) * (24 * HOUR)
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def row(symbol: str, index: int, price: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "token_id": f"0x{symbol.lower()}",
        "period_start_unix": BASE + index * HOUR,
        "timestamp": datetime.fromtimestamp(BASE + index * HOUR, tz=UTC),
        "open": Decimal(price),
        "high": Decimal(price),
        "low": Decimal(price),
        "close": Decimal(price),
        "price_usd": Decimal(price),
        "volume_usd": Decimal("1000"),
        "total_value_locked_usd": Decimal("5000"),
    }


def ramp_with_hole(symbol: str = "WBTC") -> list[dict[str, Any]]:
    """Hours 0,1,2 and 5,6,7 stored; 3 and 4 never emitted."""
    return [row(symbol, i, str(100 + i)) for i in (0, 1, 2, 5, 6, 7)]


# --- what the cache is allowed to be ----------------------------------------


def test_the_stored_method_is_not_causal_and_says_so() -> None:
    """Linear interpolation reads the gap's right endpoint.

    Nothing that trades may consume it, which is why `load_symbol_series`
    never reads this table and why the method is labelled rather than
    assumed.
    """
    assert method_is_causal(LINEAR) is False
    assert method_is_causal("something-new") is False, "an unknown method is unsafe, not fine"


def test_an_unknown_method_is_refused_rather_than_stored() -> None:
    with pytest.raises(ValueError, match="unknown fill method"):
        refresh_fill_cache(FakeDatabase(), ["WBTC"], method="kalman")


# --- what gets written ------------------------------------------------------


def test_only_the_missing_hours_are_stored_never_a_copy_of_an_observation() -> None:
    """The table is a fraction of the history, not a second copy of it."""
    db = FakeDatabase(get_price_history=ramp_with_hole())

    summary = refresh_fill_cache(db, ["WBTC"], now=NOW)

    assert summary.rows_written == 2, "two missing hours, six observed"
    written = [records for label, records in db.upserts if label == "insert_token_hours_fill"]
    assert len(written) == 1
    hours = [r["period_start_unix"] for r in written[0]]
    assert hours == [BASE + 3 * HOUR, BASE + 4 * HOUR]
    assert all(r["method"] == LINEAR for r in written[0])
    # The straight line between 102 and 105.
    assert [r["price_usd"] for r in written[0]] == [pytest.approx(103.0), pytest.approx(104.0)]


def test_nothing_is_ever_written_to_the_market_table() -> None:
    """`docs/roadmap/DESIGN.md` §0: a filled row in `token_hours_data` would
    make coverage read 100% by construction and destroy invariant 11."""
    db = FakeDatabase(get_price_history=ramp_with_hole())

    refresh_fill_cache(db, ["WBTC"], now=NOW)

    assert all(label == "insert_token_hours_fill" for label, _ in db.upserts)
    assert all("token_hours" not in label or label.endswith("_fill") for label, _ in db.upserts)


def test_each_filled_hour_records_the_length_of_its_whole_run() -> None:
    """Interpolation gives a point with no error bar; the run length is the
    honest proxy, so a one-hour fill reads differently from a long guess."""
    db = FakeDatabase(get_price_history=ramp_with_hole())

    refresh_fill_cache(db, ["WBTC"], now=NOW)

    records = next(r for label, r in db.upserts if label == "insert_token_hours_fill")
    assert [r["gap_hours"] for r in records] == [2, 2], "both hours belong to one 2-hour run"


def test_a_gap_longer_than_the_cap_is_counted_as_skipped_not_filled() -> None:
    db = FakeDatabase(get_price_history=ramp_with_hole())

    summary = refresh_fill_cache(db, ["WBTC"], max_fill=1, now=NOW)

    assert summary.rows_written == 0
    assert summary.runs[0].filled_hours == 0
    assert summary.runs[0].skipped_hours == 2
    assert summary.runs[0].longest_gap_hours == 2


def test_the_method_is_cleared_before_it_is_rebuilt() -> None:
    """A partial refresh cannot tell a row that is still right from one whose
    gap has since been ingested, so the method is rebuilt wholesale."""
    db = FakeDatabase(get_price_history=ramp_with_hole())

    refresh_fill_cache(db, ["WBTC"], now=NOW)

    labels = [label for label, _ in db.executed]
    assert "delete_fills_for_method" in labels
    assert labels.index("delete_fills_for_method") == 0, "cleared before anything is written"


def test_an_estimate_superseded_by_a_real_row_is_deleted() -> None:
    """Once ingestion supplies the hour, the cached fill is not stale but wrong."""
    db = FakeDatabase(get_price_history=ramp_with_hole())

    refresh_fill_cache(db, ["WBTC"], now=NOW)

    assert any(label == "delete_superseded_fills" for label, _ in db.executed)


def test_refreshing_nothing_touches_nothing() -> None:
    db = FakeDatabase()

    summary = refresh_fill_cache(db, [], now=NOW)

    assert summary.rows_written == 0
    assert db.executed == [] and db.upserts == []


def test_a_window_with_no_stored_history_writes_no_rows() -> None:
    db = FakeDatabase(get_price_history=[])

    summary = refresh_fill_cache(db, ["WBTC"], now=NOW)

    assert summary.rows_written == 0 and summary.runs == []


# --- the pure row builder ---------------------------------------------------


def test_fill_rows_marks_only_the_synthetic_cells() -> None:
    index = pd.date_range("2026-01-01", periods=4, freq="h", tz="UTC")
    observed = pd.Series([1.0, 2.0, 3.0, 4.0], index=index)
    synthetic = pd.Series([False, True, False, False], index=index)

    rows = fill_rows("WBTC", "0xwbtc", observed, synthetic, LINEAR, NOW)

    assert len(rows) == 1
    assert rows[0]["price_usd"] == pytest.approx(2.0)
    assert rows[0]["computed_at"] == NOW
    assert fill_rows("WBTC", "0xwbtc", observed, synthetic & False, LINEAR, NOW) == []


# --- reading it back --------------------------------------------------------


def test_an_unpopulated_cache_reads_as_empty_rather_than_failing() -> None:
    """An un-refreshed cache is a normal state, not an error."""
    frame = load_fills(FakeDatabase(), ["WBTC"])

    assert frame.empty
    assert list(frame.columns) == [
        "symbol",
        "timestamp",
        "period_start_unix",
        "price_usd",
        "gap_hours",
        "method",
    ]
    assert load_fills(FakeDatabase(), []).empty


def test_stored_fills_come_back_with_a_usable_timestamp() -> None:
    db = FakeDatabase(
        get_token_hours_fill=[
            {
                "symbol": "WBTC",
                "token_id": "0xwbtc",
                "period_start_unix": BASE + 3 * HOUR,
                "method": LINEAR,
                "price_usd": Decimal("103"),
                "close": None,
                "gap_hours": 2,
                "computed_at": NOW,
            }
        ]
    )

    frame = load_fills(db, ["WBTC"])

    assert frame["price_usd"].iloc[0] == pytest.approx(103.0)
    assert frame["timestamp"].iloc[0] == datetime.fromtimestamp(BASE + 3 * HOUR, tz=UTC)


def test_the_summary_is_empty_before_the_first_refresh() -> None:
    assert fill_cache_summary(FakeDatabase()).empty
