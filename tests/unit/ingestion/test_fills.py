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

import numpy as np
import pandas as pd
import pytest

from candlegraph.ingestion.fills import (
    LINEAR,
    METHODS,
    fill_cache_summary,
    fill_rows,
    load_fills,
    method_is_causal,
    refresh_fill_cache,
    score_methods,
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


def ramp_with_hole(n: int = 8, hole: tuple[int, ...] = (3, 4), symbol: str = "WBTC") -> list[dict[str, Any]]:
    """`n` hours stored except those in `hole`, which were never emitted.

    The default is the smallest case that exercises a gap. A test that needs
    the *causal* estimator to produce a band must push the hole later: it
    estimates volatility from the bars **before** each gap only, so three
    leading observations buy a mean and no uncertainty.
    """
    return [row(symbol, i, str(100 + i)) for i in range(n) if i not in hole]


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
        "sigma_log",
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


# --- three estimators, one table --------------------------------------------


def test_all_three_methods_are_known_and_only_one_of_them_is_causal() -> None:
    """They are alternatives for the same missing hour, not layers.

    `method` being part of the natural key is what lets all three hold an
    estimate for one hour so they can be compared; only one is ever
    selected for a read.
    """
    assert METHODS == ("linear", "causal", "bridge")
    assert {m: method_is_causal(m) for m in METHODS} == {
        "linear": False,
        "causal": True,
        "bridge": False,
    }


def test_each_method_stores_its_own_estimate_for_the_same_hour() -> None:
    """Which is the whole point of keying on method."""
    written: dict[str, list[dict[str, Any]]] = {}
    for method in METHODS:
        db = FakeDatabase(get_price_history=ramp_with_hole())
        refresh_fill_cache(db, ["WBTC"], method=method, now=NOW)
        written[method] = next(r for label, r in db.upserts if label == "insert_token_hours_fill")

    hours = {m: [r["period_start_unix"] for r in rows] for m, rows in written.items()}
    assert len(set(map(tuple, hours.values()))) == 1, "all three cover the same two hours"
    prices = {m: [r["price_usd"] for r in rows] for m, rows in written.items()}
    assert prices["causal"] == [pytest.approx(102.0)] * 2, "flat: the random walk's own answer"
    assert prices["linear"] != prices["causal"]


def test_only_the_model_based_methods_store_an_error_bar() -> None:
    """A NULL sigma is "this estimator has none", never "the band is zero"."""
    bands = {}
    for method in METHODS:
        # The hole sits at hours 30-31 so the causal estimator has real
        # history behind it to scale its band by.
        db = FakeDatabase(get_price_history=ramp_with_hole(60, hole=(30, 31)))
        refresh_fill_cache(db, ["WBTC"], method=method, now=NOW)
        rows = next(r for label, r in db.upserts if label == "insert_token_hours_fill")
        bands[method] = [r["sigma_log"] for r in rows]

    assert all(value is None for value in bands["linear"])
    assert all(isinstance(value, float) for value in bands["causal"])
    assert all(isinstance(value, float) for value in bands["bridge"])


def test_refreshing_one_method_does_not_disturb_another() -> None:
    """The delete is scoped to the method, or comparing them would be impossible."""
    db = FakeDatabase(get_price_history=ramp_with_hole())

    refresh_fill_cache(db, ["WBTC"], method="bridge", now=NOW)

    deletes = [params for label, params in db.executed if label == "delete_fills_for_method"]
    assert deletes and all(params["method"] == "bridge" for params in deletes if params)


# --- scoring the estimators against known prices -----------------------------


def random_walk(n: int = 1200, seed: int = 7) -> pd.Series:
    rng = np.random.default_rng(seed)
    walk = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.01, n)))
    return pd.Series(walk, index=pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC"))


def test_the_two_sided_estimators_beat_the_one_sided_one() -> None:
    """ "Which is best" answered by holding out real prices, not by taste.

    A bridge that reads both endpoints should beat a flat forecast that
    reads one. If this ever inverts, the estimator is wrong, not the market.
    """
    scores = {s.method: s for s in score_methods(random_walk(), gap_hours=4)}

    assert set(scores) == set(METHODS)
    assert scores["bridge"].mae < scores["causal"].mae
    assert scores["linear"].mae < scores["causal"].mae


def test_the_scores_report_which_estimates_could_have_been_made_at_the_time() -> None:
    """A low error is not a licence to trade on it."""
    scores = score_methods(random_walk(), gap_hours=4)

    assert [s.method for s in scores] == sorted(
        (s.method for s in scores), key=lambda m: {s.method: s.mae for s in scores}[m]
    ), "ranked best first"
    assert [s.causal for s in scores if s.method == "causal"] == [True]
    assert all(s.hours > 0 for s in scores)
    assert all(0.0 <= s.mae <= s.worst for s in scores)


def test_a_wider_hole_is_harder_to_estimate() -> None:
    """A sanity check on the scorer itself: the numbers must respond."""
    narrow = {s.method: s.mae for s in score_methods(random_walk(), gap_hours=2)}
    wide = {s.method: s.mae for s in score_methods(random_walk(), gap_hours=12)}

    assert wide["bridge"] > narrow["bridge"]
    assert wide["causal"] > narrow["causal"]


def test_too_little_history_scores_nothing_rather_than_dividing_by_zero() -> None:
    assert score_methods(random_walk(n=5)) == []
    assert score_methods(pd.Series(dtype="float64")) == []


def test_the_causal_band_needs_history_before_the_gap_not_around_it() -> None:
    """A subtle look-ahead the whole-series shortcut would have introduced.

    Scaling the band by volatility measured over the entire column would
    mean a band that depends on bars after the gap — invisible in the mean,
    and wrong in anything that sized a position by it. So an early gap with
    three bars behind it gets a value and no band, while the bridge (which
    is allowed to look ahead) gets both.
    """
    early = FakeDatabase(get_price_history=ramp_with_hole(60, hole=(3, 4)))
    refresh_fill_cache(early, ["WBTC"], method="causal", now=NOW)
    rows = next(r for label, r in early.upserts if label == "insert_token_hours_fill")

    assert [r["price_usd"] for r in rows] == [pytest.approx(102.0)] * 2, "a mean, still"
    assert [r["sigma_log"] for r in rows] == [None, None], "but no band it could have earned"

    late = FakeDatabase(get_price_history=ramp_with_hole(60, hole=(30, 31)))
    refresh_fill_cache(late, ["WBTC"], method="causal", now=NOW)
    later = next(r for label, r in late.upserts if label == "insert_token_hours_fill")
    assert all(r["sigma_log"] is not None for r in later)
