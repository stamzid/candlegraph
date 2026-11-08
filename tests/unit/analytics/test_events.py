"""Discrete-event execution, and the equality that licenses the fast path.

The headline test here is `test_the_event_engine_reproduces_the_vectorised
_backtester`. The vectorised backtester encodes three assumptions as
arithmetic; this module turns them into parameters. If the two disagree
when every parameter is set back to the assumption, one of them is wrong,
and the fast one is the one everything else uses.

Both bugs found while writing this module were found by that test:
`fill_ratio=1.0` being read as a one-unit cap (so a long-to-short flip
silently took two bars), and a fill reading the *current* target rather
than the one its order was placed with (so any latency executed a trade
nobody placed).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from candlegraph.analytics.events import (
    POSITION_TOLERANCE,
    ExecutionError,
    ExecutionModel,
    latency_cost,
    reconcile,
    run_execution,
)

BARS = 300


def prices(n: int = BARS, seed: int = 11) -> pd.Series:
    rng = np.random.default_rng(seed)
    walk = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.01, n)))
    return pd.Series(walk, index=pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC"), name="WBTC")


def signals(n: int = BARS, seed: int = 12) -> pd.Series:
    """Positions that flip often, including straight long-to-short."""
    rng = np.random.default_rng(seed)
    return pd.Series(
        rng.choice([-1.0, 0.0, 1.0], size=n),
        index=pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC"),
    )


# --- the reconciliation ------------------------------------------------------


@pytest.mark.parametrize("latency", [0, 1, 2, 5])
def test_the_event_engine_reproduces_the_vectorised_backtester(latency: int) -> None:
    """The deliverable of the whole module.

    With no extra latency, full fills and a flat cost, an engine built out
    of an event queue must land on exactly the position path `shift()`
    produces. That equality is what makes the fast path trustworthy
    everywhere else.
    """
    result = reconcile(prices(), signals(), latency_bars=latency)

    assert result.agrees, (
        f"latency {latency}: positions differ by {result.max_position_difference:.2e}, "
        f"equity by {result.max_equity_difference:.2e}"
    )
    assert result.max_position_difference == 0.0, "the position path must match exactly"
    assert result.event_total_return == pytest.approx(result.vector_total_return)


def test_zero_latency_means_one_bar_of_structural_delay() -> None:
    """`latency_bars` is delay *on top of* the bar you cannot trade inside.

    A decision taken at a close cannot be acted on before the next bar
    exists; that is true for an infinitely fast trader, so it is not
    latency. Getting this correspondence wrong is the off-by-one the
    whole module is about.
    """
    assert ExecutionModel(latency_bars=0).matching_execution_lag == 1
    assert ExecutionModel(latency_bars=3).matching_execution_lag == 4


def test_the_position_path_is_the_signal_delayed() -> None:
    """Stated directly, not only through the reconciliation."""
    for latency in (0, 1, 2):
        run = run_execution(prices(), signals(), ExecutionModel(latency_bars=latency, cost_bps=0.0))

        expected = signals().shift(1 + latency).fillna(0.0)

        assert np.allclose(run.positions.to_numpy(), expected.to_numpy()), latency


def test_a_long_to_short_flip_completes_in_one_bar() -> None:
    """Two units of movement, and `fill_ratio=1.0` means *all* of the gap.

    Reading it as a one-unit cap was the first bug the reconciliation
    caught: every reversal silently took two bars.
    """
    index = pd.date_range("2026-01-01", periods=5, freq="h", tz="UTC")
    flat = pd.Series(100.0, index=index)
    flip = pd.Series([1.0, -1.0, -1.0, -1.0, -1.0], index=index)

    run = run_execution(flat, flip, ExecutionModel(cost_bps=0.0))

    assert run.positions.tolist() == [0.0, 1.0, -1.0, -1.0, -1.0]


def test_an_order_fills_at_the_target_it_was_placed_with() -> None:
    """The second bug: with latency, the decision has moved on by fill time.

    Reading the live target would execute a trade nobody placed. There is
    no cancel-replace here, which is also what `shift(k>1)` quietly
    assumes — you are filling stale decisions.
    """
    index = pd.date_range("2026-01-01", periods=6, freq="h", tz="UTC")
    flat = pd.Series(100.0, index=index)
    # Decide +1 at bar 0, then immediately change your mind to -1.
    changing = pd.Series([1.0, -1.0, 0.0, 0.0, 0.0, 0.0], index=index)

    run = run_execution(flat, changing, ExecutionModel(latency_bars=1, cost_bps=0.0))

    # Bar 0's +1 arrives at bar 1 and is held during bar 2, even though
    # the view had already changed.
    assert run.positions.iloc[2] == pytest.approx(1.0)
    assert run.positions.iloc[3] == pytest.approx(-1.0)


# --- relaxing the assumptions -------------------------------------------------


def test_a_partial_fill_takes_several_bars_and_pays_the_whole_way() -> None:
    index = pd.date_range("2026-01-01", periods=10, freq="h", tz="UTC")
    flat = pd.Series(100.0, index=index)
    go_long = pd.Series([1.0] * 10, index=index)

    run = run_execution(flat, go_long, ExecutionModel(fill_ratio=0.5, cost_bps=10.0))

    reached = run.positions.to_numpy()
    assert reached[0] == pytest.approx(0.0), "nothing is held during the deciding bar"
    assert reached[1] == pytest.approx(0.5), "half the gap closed on the first fill"
    assert 0.5 < reached[2] < 1.0, "still arriving"
    assert reached[-1] == pytest.approx(1.0), "and it does arrive, rather than approaching forever"
    assert run.incomplete_fills > 0, "it says it took more than one go"
    assert run.total_costs > 0, "and paid on every leg of the way in"


def test_full_fills_report_no_incomplete_orders() -> None:
    run = run_execution(prices(), signals(), ExecutionModel(fill_ratio=1.0))

    assert run.incomplete_fills == 0


def test_a_thin_bar_absorbs_less_of_the_order() -> None:
    """A liquidity proxy, not microstructure: hourly bars cannot support
    a queue-position model, so this scales against median volume."""
    index = pd.date_range("2026-01-01", periods=8, freq="h", tz="UTC")
    flat = pd.Series(100.0, index=index)
    go_long = pd.Series([1.0] * 8, index=index)
    # The thin bars come first, or the order completes before reaching them.
    thin = pd.Series([0.01, 0.01, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0], index=index)

    unconstrained = run_execution(flat, go_long, ExecutionModel())
    constrained = run_execution(flat, go_long, ExecutionModel(participation_limit=0.5), volume=thin)

    assert unconstrained.positions.iloc[1] == pytest.approx(1.0), "no limit, no delay"
    assert constrained.positions.iloc[1] < 0.02, "a bar at 1% of median volume absorbs ~1%"
    assert constrained.positions.iloc[3] == pytest.approx(1.0), "a normal bar completes it"


def test_latency_costs_are_reported_as_a_number() -> None:
    """ "What the convenient assumptions are worth", which no amount of
    reasoning about `shift(1)` produces."""
    table = latency_cost(prices(), signals(), latencies=(0, 1, 2))

    assert list(table["latency_bars"]) == [0, 1, 2]
    assert table["vs_baseline"].iloc[0] == pytest.approx(0.0), "the baseline is the fast path"
    assert (table["vs_baseline"].iloc[1:] != 0).any(), "latency has to cost or save something"
    assert {"total_return", "costs", "fills", "incomplete_fills"} <= set(table.columns)


# --- refusals -----------------------------------------------------------------


def test_an_impossible_model_is_refused_at_construction() -> None:
    with pytest.raises(ExecutionError, match="latency_bars"):
        ExecutionModel(latency_bars=-1)
    with pytest.raises(ExecutionError, match="fill_ratio"):
        ExecutionModel(fill_ratio=0.0)
    with pytest.raises(ExecutionError, match="fill_ratio"):
        ExecutionModel(fill_ratio=1.5)
    with pytest.raises(ExecutionError, match="cost_bps"):
        ExecutionModel(cost_bps=-1.0)


def test_no_prices_is_a_refusal_not_an_empty_result() -> None:
    with pytest.raises(ExecutionError, match="no prices"):
        run_execution(pd.Series(dtype="float64"), pd.Series(dtype="float64"))


def test_costs_scale_with_how_much_the_position_moved() -> None:
    """Flipping long to short costs twice opening from flat, as in the
    backtester — the formula is shared, not reimplemented."""
    index = pd.date_range("2026-01-01", periods=4, freq="h", tz="UTC")
    flat = pd.Series(100.0, index=index)

    opening = run_execution(
        flat, pd.Series([1.0, 1.0, 1.0, 1.0], index=index), ExecutionModel(cost_bps=100.0)
    )
    flipping = run_execution(
        flat, pd.Series([1.0, -1.0, -1.0, -1.0], index=index), ExecutionModel(cost_bps=100.0)
    )

    assert flipping.total_costs == pytest.approx(3 * opening.total_costs), (
        "one unit in, then two units across"
    )
    assert POSITION_TOLERANCE < 1e-6
