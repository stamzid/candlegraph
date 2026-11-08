"""Discrete-event execution: what the vectorised backtester assumes away.

Pure, like the rest of :mod:`candlegraph.analytics`. Prices and a signal
series in, a realised position path and its accounting out.

**This does not replace the vectorised backtester. It is the reference
implementation that one is checked against.**
``strategies/backtest.py`` encodes three assumptions as arithmetic:

1. the decision at bar *t* becomes a position at *t+1* exactly
   (``shift(1)``);
2. the fill happens at the close, in full, always;
3. the cost is a flat ``(fee + slippage)`` bps regardless of size.

Here each is a parameter instead. Run with no extra latency, full fills
and a flat cost and the position path must come out **identical** to
``signal.shift(1)``; that equality is what makes the fast path
trustworthy, and a test pins it. Then one assumption is relaxed at a time
and the divergence is a number rather than an intuition.

The correspondence is worth stating precisely, because the off-by-one is
the whole subject:

``ExecutionModel(latency_bars=k)`` matches ``BacktestConfig(execution_lag=k + 1)``.

The ``+ 1`` is not latency. It is the fact that a decision taken at a
bar's close cannot be acted on before the next bar exists — structural,
and true even for an infinitely fast trader. ``latency_bars`` is whatever
delay comes *on top* of that.

**The accounting is deliberately shared** with the backtester
(``equity_curve``, ``drawdown_curve``, the same cost formula). Writing a
second ``position * returns`` here is how invariant 15 comes back. What
this module derives independently is the **position path**, through an
event queue; that is the thing worth reconciling.
"""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass, field
from enum import IntEnum

import numpy as np
import pandas as pd

from candlegraph.analytics.rolling import drawdown_curve, equity_curve, simple_returns

BPS = 1e-4
# How close two floats must be for the reconciliation to count as exact.
# Positions are sums of a few fractions; equity compounds, so it is looser.
POSITION_TOLERANCE = 1e-12
EQUITY_TOLERANCE = 1e-9


class ExecutionError(ValueError):
    """The execution model is not usable as configured."""


class Phase(IntEnum):
    """Ordering of events that land on the same bar.

    A decision taken at a bar's close has to be queued before a fill that
    the same bar is due to receive, or a zero-latency order would be
    processed a bar late and the reconciliation would fail for a reason
    that has nothing to do with execution.
    """

    DECIDE = 0
    FILL = 1


@dataclass(frozen=True)
class ExecutionModel:
    """What the backtester assumes, as parameters that can be moved.

    Attributes:
        latency_bars: Delay *on top of* the structural one bar. ``0``
            reproduces the vectorised backtester.
        fill_ratio: Fraction of the outstanding gap closed per bar. ``1.0``
            fills in full immediately; ``0.25`` takes four bars to turn a
            flat book long, paying costs the whole way.
        cost_bps: Charged per unit of position changed, like the
            backtester's ``fee + slippage``.
        participation_limit: Optional cap on position change per bar as a
            multiple of the bar's share of median volume. A liquidity
            proxy, not microstructure — candlegraph has hourly bars, so a
            queue-position model would be inventing data it does not have.
    """

    latency_bars: int = 0
    fill_ratio: float = 1.0
    cost_bps: float = 35.0
    participation_limit: float | None = None

    def __post_init__(self) -> None:
        if self.latency_bars < 0:
            raise ExecutionError("latency_bars must not be negative")
        if not 0.0 < self.fill_ratio <= 1.0:
            raise ExecutionError("fill_ratio must be in (0, 1]")
        if self.cost_bps < 0:
            raise ExecutionError("cost_bps must not be negative")
        if self.participation_limit is not None and self.participation_limit <= 0:
            raise ExecutionError("participation_limit must be positive")

    @property
    def cost_per_unit(self) -> float:
        return self.cost_bps * BPS

    @property
    def matching_execution_lag(self) -> int:
        """The ``BacktestConfig.execution_lag`` this model corresponds to."""
        return self.latency_bars + 1


@dataclass(frozen=True)
class Fill:
    """One execution: what was wanted, what was got, where it left us."""

    bar: int
    requested: float
    filled: float
    position_after: float

    @property
    def complete(self) -> bool:
        return abs(self.position_after - self.requested) < POSITION_TOLERANCE


@dataclass
class ExecutionResult:
    """A finished event-driven run."""

    frame: pd.DataFrame
    """``price``, ``signal``, ``position``, ``returns``, ``costs``,
    ``strategy_returns``, ``equity``, ``drawdown`` — the same columns the
    vectorised backtester produces, so the two can be compared directly."""
    model: ExecutionModel
    fills: list[Fill] = field(default_factory=list)

    @property
    def positions(self) -> pd.Series:
        return self.frame["position"]

    @property
    def equity(self) -> pd.Series:
        return self.frame["equity"]

    @property
    def total_return(self) -> float:
        return float(self.equity.iloc[-1]) - 1.0 if len(self.frame) else 0.0

    @property
    def total_costs(self) -> float:
        return float(self.frame["costs"].sum())

    @property
    def trades(self) -> int:
        return len(self.fills)

    @property
    def incomplete_fills(self) -> int:
        """Orders that could not be filled in one bar. Zero at ``fill_ratio=1``."""
        return sum(1 for fill in self.fills if not fill.complete)


# --- the event loop, in small pieces -----------------------------------------


def _volume_capacity(volume: pd.Series | None, model: ExecutionModel, bars: int) -> np.ndarray:
    """Per-bar cap on position change implied by how much traded.

    A bar with half the usual volume absorbs half the usual order. Scaled
    against the median so the number is unit-free.

    Without a participation limit the capacity is **infinite**, not one
    unit. That distinction is not pedantic: a long-to-short flip moves two
    units, so a cap of 1.0 would silently split every reversal across two
    bars and the reconciliation against the vectorised backtester would
    fail for a reason that has nothing to do with liquidity.
    """
    if model.participation_limit is None or volume is None:
        return np.full(bars, np.inf)

    values = volume.to_numpy(dtype="float64")
    median = float(np.nanmedian(values))
    if not np.isfinite(median) or median <= 0:
        return np.full(bars, np.inf)
    share = np.nan_to_num(values / median, nan=0.0, posinf=1.0)
    return np.asarray(model.participation_limit * share, dtype="float64")


def _step_towards(current: float, target: float, fraction: float, cap: float) -> float:
    """Move ``current`` towards ``target`` by a fraction of the gap, under a cap.

    Two different limits, which is why they are separate arguments and
    why conflating them was a bug:

    * ``fraction`` is how much of the *remaining distance* this bar closes
      — the order's own aggression. At ``1.0`` the gap closes completely,
      however wide it is.
    * ``cap`` is how much the *market* can absorb this bar, in position
      units, and is infinite unless a participation limit says otherwise.

    A long-to-short flip is a two-unit move, so a cap expressed in units
    has to be allowed to exceed one or every reversal silently takes two
    bars.
    """
    gap = target - current
    if abs(gap) <= POSITION_TOLERANCE:
        return target
    step = min(abs(gap) * fraction, cap)
    moved = current + np.sign(gap) * step
    # Snap rather than approach asymptotically: a fractional fill would
    # otherwise never quite arrive and would pay costs forever.
    return target if abs(target - moved) <= POSITION_TOLERANCE else float(moved)


def _run_queue(
    signals: np.ndarray, model: ExecutionModel, capacity: np.ndarray
) -> tuple[np.ndarray, list[Fill]]:
    """Walk the event queue and return the position held during each bar.

    The queue holds ``(bar, phase, token)`` so same-bar events resolve in a
    defined order and ties never depend on dictionary iteration.

    **An order carries the target it was placed with**, held in ``pending``
    rather than read from a shared variable at fill time. That is not a
    detail: with any latency at all, the decision has moved on by the time
    the fill lands, and reading the current target would execute a trade
    nobody placed. It also makes explicit something the vectorised model
    implies but never says — at ``execution_lag`` above one you are
    filling *stale decisions*, not the latest one, and there is no
    cancel-replace.

    A partially filled order re-queues itself for the next bar, so a
    multi-bar fill emerges from the machinery rather than from a formula.

    Returns the position held *during* each bar, so it earns that bar's
    return — the same convention the vectorised backtester uses.
    """
    bars = len(signals)
    position_after_bar = np.zeros(bars)
    fills: list[Fill] = []

    position = 0.0
    tokens = itertools.count()
    pending: dict[int, float] = {}
    queue: list[tuple[int, int, int]] = [(bar, Phase.DECIDE, next(tokens)) for bar in range(bars)]
    heapq.heapify(queue)

    settled_through = 0
    while queue:
        bar, phase, token = heapq.heappop(queue)
        if bar >= bars:
            continue

        # Bars between the last event and this one simply hold.
        position_after_bar[settled_through : bar + 1] = position
        settled_through = bar

        if phase == Phase.DECIDE:
            arrival = bar + model.latency_bars
            if arrival < bars:
                order = next(tokens)
                pending[order] = float(signals[bar])
                heapq.heappush(queue, (arrival, Phase.FILL, order))
            continue

        target = pending.get(token)
        if target is None:
            continue

        moved_to = _step_towards(position, target, model.fill_ratio, float(capacity[bar]))
        if abs(moved_to - position) > POSITION_TOLERANCE:
            fills.append(Fill(bar, target, moved_to - position, moved_to))
            position = moved_to
        position_after_bar[bar] = position

        if abs(position - target) > POSITION_TOLERANCE and bar + 1 < bars:
            heapq.heappush(queue, (bar + 1, Phase.FILL, token))
        else:
            pending.pop(token, None)

    position_after_bar[settled_through:] = position
    # A position reached at the close of bar t is held *during* bar t+1.
    return np.concatenate([[0.0], position_after_bar[:-1]]), fills


def _account(
    prices: pd.Series, signals: pd.Series, position: pd.Series, model: ExecutionModel
) -> pd.DataFrame:
    """Turn a position path into returns, costs and equity.

    Deliberately the *same* arithmetic the vectorised backtester uses —
    ``equity_curve``, ``drawdown_curve``, costs on turnover. Writing a
    second version here is how invariant 15 comes back; what this module
    derives independently is the position path above.
    """
    asset_returns = simple_returns(prices).fillna(0.0)
    gross = position * asset_returns
    turnover = position.diff().abs().fillna(position.abs())
    costs = turnover * model.cost_per_unit
    net = gross - costs
    equity = equity_curve(net, 1.0)

    return pd.DataFrame(
        {
            "price": prices,
            "signal": signals,
            "position": position,
            "returns": asset_returns,
            "gross_returns": gross,
            "costs": costs,
            "strategy_returns": net,
            "equity": equity,
            "drawdown": drawdown_curve(equity),
        }
    )


def run_execution(
    prices: pd.Series,
    signals: pd.Series,
    model: ExecutionModel | None = None,
    volume: pd.Series | None = None,
) -> ExecutionResult:
    """Simulate the order lifecycle bar by bar.

    Args:
        prices: Marks, indexed by bar.
        signals: Target positions decided at each bar's close.
        model: What to assume about latency, fills and cost.
        volume: Optional per-bar volume, used only when the model sets
            ``participation_limit``.

    Returns:
        An :class:`ExecutionResult` whose frame matches the vectorised
        backtester's columns.

    Raises:
        ExecutionError: the two series cannot be lined up.
    """
    model = model or ExecutionModel()
    values = prices.astype("float64")
    aligned = signals.reindex(values.index).fillna(0.0).astype("float64")
    if values.empty:
        raise ExecutionError("no prices to execute against")

    capacity = _volume_capacity(volume, model, len(values))
    held, fills = _run_queue(aligned.to_numpy(dtype="float64"), model, capacity)
    position = pd.Series(held, index=values.index, name="position")
    return ExecutionResult(frame=_account(values, aligned, position, model), model=model, fills=fills)


# --- reconciliation: is the fast path telling the truth? ----------------------


@dataclass(frozen=True)
class Reconciliation:
    """How far the event-driven run is from the vectorised one."""

    execution_lag: int
    max_position_difference: float
    max_equity_difference: float
    event_total_return: float
    vector_total_return: float

    @property
    def agrees(self) -> bool:
        """Whether the two agree to floating-point tolerance."""
        return (
            self.max_position_difference <= POSITION_TOLERANCE
            and self.max_equity_difference <= EQUITY_TOLERANCE
        )


def reconcile(
    prices: pd.Series, signals: pd.Series, latency_bars: int = 0, cost_bps: float = 35.0
) -> Reconciliation:
    """Run both engines on the same inputs and measure the gap.

    At ``latency_bars=0``, full fills and a flat cost the gap must be
    zero: that is what licenses using the fast path everywhere else. A
    non-zero answer means one of the two is wrong, and finding out which
    is the work.
    """
    from candlegraph.strategies.backtest import BacktestConfig, backtest
    from candlegraph.strategies.base import StrategyResult

    model = ExecutionModel(latency_bars=latency_bars, cost_bps=cost_bps, fill_ratio=1.0)
    event_run = run_execution(prices, signals, model)

    vector_run = backtest(
        StrategyResult(
            name="reconciliation",
            symbol=str(prices.name or ""),
            frame=pd.DataFrame({"price": prices, "signal": signals, "score": 0.0}),
            params={},
        ),
        prices=prices,
        config=BacktestConfig(
            fee_bps=cost_bps,
            slippage_bps=0.0,
            initial_capital=1.0,
            execution_lag=model.matching_execution_lag,
        ),
    )

    return Reconciliation(
        execution_lag=model.matching_execution_lag,
        max_position_difference=float((event_run.positions - vector_run.frame["position"]).abs().max()),
        max_equity_difference=float((event_run.equity - vector_run.frame["equity"]).abs().max()),
        event_total_return=event_run.total_return,
        vector_total_return=vector_run.total_return,
    )


def latency_cost(
    prices: pd.Series,
    signals: pd.Series,
    latencies: tuple[int, ...] = (0, 1, 2, 4),
    cost_bps: float = 35.0,
    fill_ratios: tuple[float, ...] = (1.0,),
) -> pd.DataFrame:
    """What each relaxed assumption is worth, as a table.

    One row per ``(latency, fill_ratio)`` combination, with the total
    return and how far it sits from the zero-latency, full-fill baseline —
    which is the vectorised backtester's answer. That difference is the
    price of the assumptions, which no amount of reasoning about
    ``shift(1)`` can produce.
    """
    rows = []
    baseline: float | None = None
    for ratio in fill_ratios:
        for latency in latencies:
            model = ExecutionModel(latency_bars=latency, fill_ratio=ratio, cost_bps=cost_bps)
            run = run_execution(prices, signals, model)
            if baseline is None:
                baseline = run.total_return
            rows.append(
                {
                    "latency_bars": latency,
                    "fill_ratio": ratio,
                    "total_return": run.total_return,
                    "vs_baseline": run.total_return - baseline,
                    "costs": run.total_costs,
                    "fills": run.trades,
                    "incomplete_fills": run.incomplete_fills,
                }
            )
    return pd.DataFrame(rows)
