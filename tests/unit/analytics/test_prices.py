"""Loading and aligning stored rows into frames the numeric code can use."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pandas as pd
import pytest

from candlegraph.analytics.prices import (
    FILL_RULES,
    PriceError,
    apply_fill,
    fill_rule,
    is_causal,
    load_price_frame,
    load_price_history,
    load_symbol_series,
    require_causal,
    run_lengths,
    to_wide,
    to_wide_filled,
    window_bounds,
)
from candlegraph.tokens import TokenRegistry
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


# --- the fill seam: who may consume what -----------------------------------


def test_every_fill_policy_declares_whether_it_is_causal() -> None:
    """The seam's whole job: the distinction stops being a comment.

    `docs/roadmap/DESIGN.md` §0 scopes filling by who is asking. Interpolation
    reads the gap's right endpoint — a future price — so it is the one policy
    nothing that trades may see.
    """
    assert {name: rule.causal for name, rule in FILL_RULES.items()} == {
        "none": True,
        "drop": True,
        "ffill": True,
        "interpolate": False,
    }
    assert is_causal("ffill") and not is_causal("interpolate")


def test_an_unknown_fill_policy_names_the_real_ones() -> None:
    with pytest.raises(PriceError, match="unknown fill policy"):
        fill_rule("magic")


def test_a_strategy_may_not_consume_an_interpolated_series() -> None:
    """Invariant 17 does not relax for synthetic bars.

    It refuses rather than quietly downgrading: a silent substitution would
    make the chosen policy a lie and the equity curve unexplainable.
    """
    with pytest.raises(PriceError, match="not causal"):
        require_causal("interpolate", "a strategy or backtest")
    for policy in ("none", "drop", "ffill"):
        require_causal(policy, "a strategy or backtest")


def test_the_strategy_loader_refuses_a_look_ahead_fill(registry: TokenRegistry) -> None:
    """The seam is enforced at the door strategies actually walk through."""
    db = FakeDatabase(get_price_history=[row("WBTC", i) for i in range(10)])

    with pytest.raises(PriceError, match="not causal"):
        load_symbol_series(db, registry, "WBTC", fill="interpolate")

    assert not load_symbol_series(db, registry, "WBTC", fill="ffill").empty


# --- run lengths: why a long gap is left alone ------------------------------


def test_run_lengths_measure_the_whole_run_each_position_belongs_to() -> None:
    mask = pd.Series([False, True, True, True, False, True])

    assert list(run_lengths(mask)) == [0, 3, 3, 3, 0, 1]
    assert run_lengths(pd.Series([], dtype="bool")).empty


# --- linear interpolation: display only, capped, flagged --------------------


def wide_with_hole(hole: int = 2) -> pd.DataFrame:
    """A straight ramp with `hole` interior hours removed."""
    index = pd.date_range("2026-01-01", periods=10, freq="h", tz="UTC")
    values = [float(i) for i in range(10)]
    frame = pd.DataFrame({"WBTC": values}, index=index)
    frame.iloc[3 : 3 + hole] = float("nan")
    return frame


def test_interpolation_draws_a_straight_line_across_an_interior_gap() -> None:
    result = apply_fill(wide_with_hole(2), fill="interpolate")

    assert list(result.frame["WBTC"]) == [float(i) for i in range(10)], "the ramp is restored"
    assert result.filled_cells == 2
    assert list(result.synthetic["WBTC"]) == [False] * 3 + [True] * 2 + [False] * 5


def test_a_gap_longer_than_the_cap_stays_a_gap_entirely() -> None:
    """Not half a line and half a hole.

    pandas' own `limit` fills the first N hours of an over-long run and
    leaves the rest, which would draw a line that stops in mid-air. Beyond
    the cap the line is fiction and the whole hole stays.
    """
    result = apply_fill(wide_with_hole(5), fill="interpolate", max_fill=3)

    assert result.frame["WBTC"].isna().sum() == 5, "nothing filled"
    assert result.filled_cells == 0
    # Exactly at the cap it does fill, so the boundary is the cap itself.
    assert apply_fill(wide_with_hole(3), fill="interpolate", max_fill=3).filled_cells == 3


def test_interpolation_never_invents_a_price_before_the_first_real_bar() -> None:
    """A token that started late has a short history, not a leading gap."""
    index = pd.date_range("2026-01-01", periods=6, freq="h", tz="UTC")
    frame = pd.DataFrame(
        {"WBTC": [float("nan"), float("nan"), 2.0, 3.0, float("nan"), float("nan")]}, index=index
    )

    result = apply_fill(frame, fill="interpolate")

    assert result.frame["WBTC"].isna().tolist() == [True, True, False, False, True, True]
    assert result.filled_cells == 0, "no extrapolation at either end"


def test_the_synthetic_mask_marks_carried_bars_too() -> None:
    """`ffill` manufactures a value just as surely as a line does."""
    result = apply_fill(wide_with_hole(2), fill="ffill")

    assert list(result.frame["WBTC"][3:5]) == [2.0, 2.0], "carried, not interpolated"
    assert result.filled_cells == 2
    assert list(result.filled_index("WBTC")) == list(wide_with_hole(2).index[3:5])


def test_leaving_the_holes_alone_marks_nothing_as_synthetic() -> None:
    for policy in ("none", "drop"):
        result = apply_fill(wide_with_hole(2), fill=policy)

        assert result.filled_cells == 0, policy
        assert not result.synthetic.to_numpy().any(), policy
    # `drop` removes the rows rather than filling them.
    assert len(apply_fill(wide_with_hole(2), fill="drop").frame) == 8


def test_to_wide_is_unchanged_by_the_seam() -> None:
    """Every existing caller keeps getting a plain frame and the same numbers."""
    db = FakeDatabase(get_price_history=[row("WBTC", i) for i in (0, 1, 3, 4)])
    history = load_price_history(db, ["WBTC"])

    plain = to_wide(history, fill="ffill")
    carried = to_wide_filled(history, fill="ffill")

    assert plain.equals(carried.frame)
    assert carried.filled_cells == 1, "the missing hour was carried"
    assert carried.synthetic.shape == carried.frame.shape


def test_an_empty_history_yields_an_empty_mask_rather_than_raising() -> None:
    result = to_wide_filled(load_price_history(FakeDatabase(), ["WBTC"]))

    assert result.frame.empty and result.synthetic.empty
    assert result.filled_cells == 0
    assert list(result.filled_index("WBTC")) == []


# --- the look-ahead, demonstrated rather than asserted in prose -------------


def ramp_with_hole(n: int) -> pd.DataFrame:
    """`n` hours of a ramp, with hour 3 missing."""
    index = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    frame = pd.DataFrame({"WBTC": [float(i) for i in range(n)]}, index=index)
    frame.iloc[3] = float("nan")
    return frame


def test_interpolation_rewrites_a_past_bar_when_a_future_one_arrives() -> None:
    """Why `interpolate` is display only, shown as behaviour.

    The filled value for hour 3 depends on hour 4 — a bar that had not
    happened yet. Feed this to a backtester and it trades on information it
    could not have had, which invariant 17 forbids and which no test one
    layer down would notice.
    """
    short = apply_fill(ramp_with_hole(5), fill="interpolate").frame
    assert short["WBTC"].iloc[3] == pytest.approx(3.0)

    # Move only the bar *after* the hole; the filled bar before it moves too.
    moved = ramp_with_hole(5)
    moved.iloc[4] = 100.0
    rewritten = apply_fill(moved, fill="interpolate").frame

    assert rewritten["WBTC"].iloc[3] == pytest.approx(51.0), "a future bar moved a past one"
    assert rewritten["WBTC"].iloc[2] == short["WBTC"].iloc[2], "observations are untouched"


def test_a_causal_fill_is_unmoved_by_a_future_bar() -> None:
    """The same experiment against `ffill`: the past stays put.

    This is the property that makes a policy safe for a strategy, and the
    pair of tests is the seam's justification: one rewrites history, one
    does not.
    """
    moved = ramp_with_hole(5)
    moved.iloc[4] = 100.0

    before = apply_fill(ramp_with_hole(5), fill="ffill").frame
    after = apply_fill(moved, fill="ffill").frame

    assert after["WBTC"].iloc[3] == before["WBTC"].iloc[3] == pytest.approx(2.0)
    assert after["WBTC"].iloc[:4].equals(before["WBTC"].iloc[:4])


def test_a_strategy_run_is_identical_whether_or_not_the_seam_exists() -> None:
    """The stage's exit criterion: signals are unchanged by the fill work.

    `load_symbol_series` has always used `ffill`; adding the seam must not
    have moved a single bar of it, or every stored signal silently changed
    meaning.
    """
    from candlegraph.strategies import run_strategy

    db = FakeDatabase(get_price_history=[row("WBTC", i, price=str(100 + i % 7)) for i in range(200)])
    registry_free = load_price_frame(db, ["WBTC"], fill="ffill")["WBTC"].dropna()
    registry_free.name = "WBTC"

    through_seam = (
        apply_fill(to_wide(load_price_history(db, ["WBTC"]), fill="none"), fill="ffill")
        .frame["WBTC"]
        .dropna()
    )
    through_seam.name = "WBTC"

    assert run_strategy("bollinger", registry_free, None).signals.equals(
        run_strategy("bollinger", through_seam, None).signals
    )
