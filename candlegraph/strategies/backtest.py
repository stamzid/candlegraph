"""Vectorised backtesting of a position series against realised returns.

The single most important line in this module is the ``shift(1)``.

A strategy decides its position using the close of bar *t*. It cannot trade at
a price it has not seen yet, so that position earns bar *t+1*'s return, not
bar *t*'s. Multiplying an unshifted signal by same-bar returns is the most
common way to produce a backtest that looks extraordinary and is worth
nothing, and it is silent — the equity curve just goes up. A test pins this.

Costs are charged on **position changes**, sized by how much the position
moved, so flipping long to short costs twice as much as opening from flat.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

import numpy as np
import pandas as pd

from candlegraph.analytics.rolling import (
    drawdown_curve,
    equity_curve,
    max_drawdown,
    periods_per_year,
    sharpe_ratio,
    simple_returns,
    sortino_ratio,
)
from candlegraph.strategies.base import StrategyResult

BPS = 1e-4


class BacktestError(ValueError):
    """The backtest configuration is invalid."""


@dataclass(frozen=True)
class BacktestConfig:
    """Execution assumptions.

    Attributes:
        fee_bps: Round-trip-per-side trading fee, in basis points. Uniswap v3
            pools charge 5-100 bps; 30 is the common tier.
        slippage_bps: Extra cost per unit traded, in basis points.
        initial_capital: Starting equity, for the reported curve.
        risk_free_rate: Annual rate used in the Sharpe and Sortino ratios.
        interval_hours: Width of one bar, for annualisation.
        execution_lag: Bars between deciding and trading. 1 is the honest
            default; 0 is look-ahead and is rejected unless you deliberately
            ask for it to demonstrate the bias.
    """

    fee_bps: float = 30.0
    slippage_bps: float = 5.0
    initial_capital: float = 10_000.0
    risk_free_rate: float = 0.0
    interval_hours: int = 1
    execution_lag: int = 1

    def __post_init__(self) -> None:
        if self.fee_bps < 0 or self.slippage_bps < 0:
            raise BacktestError("costs must not be negative")
        if self.initial_capital <= 0:
            raise BacktestError("initial_capital must be positive")
        if self.execution_lag < 0:
            raise BacktestError("execution_lag must not be negative")

    @property
    def cost_per_unit(self) -> float:
        """Total cost of changing position by one unit."""
        return (self.fee_bps + self.slippage_bps) * BPS


@dataclass
class BacktestResult:
    """A finished backtest: the curves, and the numbers that summarise them."""

    strategy: str
    symbol: str
    frame: pd.DataFrame
    config: BacktestConfig
    params: dict[str, Any] = field(default_factory=dict)

    # headline metrics
    total_return: float = 0.0
    buy_and_hold_return: float = 0.0
    annualised_return: float = 0.0
    annualised_volatility: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    max_drawdown: float = 0.0
    trades: int = 0
    exposure: float = 0.0
    win_rate: float = 0.0
    total_costs: float = 0.0
    bars: int = 0

    @property
    def equity(self) -> pd.Series:
        return self.frame["equity"]

    @property
    def excess_return(self) -> float:
        """How much the strategy beat simply holding the asset."""
        return self.total_return - self.buy_and_hold_return


def backtest(
    result: StrategyResult,
    prices: pd.Series | None = None,
    config: BacktestConfig | None = None,
) -> BacktestResult:
    """Run a strategy's positions against realised returns.

    Args:
        result: The strategy output, carrying the ``signal`` column.
        prices: Prices to mark against. Defaults to the ``price`` column the
            strategies all emit.
        config: Costs and execution assumptions.

    Returns:
        A :class:`BacktestResult` whose frame holds ``position``, ``returns``,
        ``strategy_returns``, ``costs``, ``equity`` and ``drawdown`` per bar.

    Raises:
        BacktestError: no price series available to mark against.
    """
    config = config or BacktestConfig()

    if prices is None:
        if "price" not in result.frame:
            raise BacktestError(f"strategy {result.name!r} emitted no 'price' column; pass prices explicitly")
        prices = result.frame["price"]

    values = prices.astype("float64")
    signals = result.signals.reindex(values.index).fillna(0).astype("float64")

    # The whole point: act on bar t's decision at bar t+1.
    position = signals.shift(config.execution_lag).fillna(0.0)

    asset_returns = simple_returns(values).fillna(0.0)
    gross = position * asset_returns

    turnover = position.diff().abs().fillna(position.abs())
    costs = turnover * config.cost_per_unit
    net = gross - costs

    equity = equity_curve(net, config.initial_capital)
    drawdown = drawdown_curve(equity)

    frame = pd.DataFrame(
        {
            "price": values,
            "signal": signals,
            "position": position,
            "returns": asset_returns,
            "gross_returns": gross,
            "costs": costs,
            "strategy_returns": net,
            "equity": equity,
            "drawdown": drawdown,
        }
    )

    return _summarise(result, frame, config, net, asset_returns, position, turnover)


def _summarise(
    result: StrategyResult,
    frame: pd.DataFrame,
    config: BacktestConfig,
    net: pd.Series,
    asset_returns: pd.Series,
    position: pd.Series,
    turnover: pd.Series,
) -> BacktestResult:
    equity = frame["equity"]
    bars = len(frame)
    factor = periods_per_year(config.interval_hours)

    final_equity = float(equity.iloc[-1]) if bars else config.initial_capital
    total_return = final_equity / config.initial_capital - 1.0
    # pandas-stubs types `.prod()` as the union of every dtype it could
    # hold; the series is float64 by construction.
    hold = float(cast(float, (1.0 + asset_returns).prod())) - 1.0 if bars else 0.0

    # Geometric annualisation. A -100% strategy has no defined CAGR, so clamp.
    growth = 1.0 + total_return
    annualised = float(growth ** (factor / bars) - 1.0) if bars > 0 and growth > 0 else -1.0

    active = net[position != 0]
    volatility = float(net.std(ddof=1) * np.sqrt(factor)) if bars > 1 else 0.0

    return BacktestResult(
        strategy=result.name,
        symbol=result.symbol,
        frame=frame,
        config=config,
        params=result.params,
        total_return=total_return,
        buy_and_hold_return=hold,
        annualised_return=annualised,
        annualised_volatility=volatility,
        sharpe=sharpe_ratio(net, config.risk_free_rate, config.interval_hours),
        sortino=sortino_ratio(net, config.risk_free_rate, config.interval_hours),
        max_drawdown=max_drawdown(equity),
        trades=int((turnover > 0).sum()),
        exposure=float((position != 0).mean()) if bars else 0.0,
        win_rate=float((active > 0).mean()) if len(active) else 0.0,
        total_costs=float(frame["costs"].sum()),
        bars=bars,
    )


def format_backtest(result: BacktestResult) -> str:
    """Render a backtest as a fixed-width report for the CLI."""
    config = result.config
    rows = [
        ("bars", f"{result.bars}"),
        ("total return", f"{result.total_return:.2%}"),
        ("buy & hold", f"{result.buy_and_hold_return:.2%}"),
        ("excess", f"{result.excess_return:+.2%}"),
        ("annualised", f"{result.annualised_return:.2%}"),
        ("volatility", f"{result.annualised_volatility:.2%}"),
        ("sharpe", f"{result.sharpe:.3f}"),
        ("sortino", f"{result.sortino:.3f}"),
        ("max drawdown", f"{result.max_drawdown:.2%}"),
        ("trades", f"{result.trades}"),
        ("exposure", f"{result.exposure:.1%}"),
        ("win rate", f"{result.win_rate:.1%}"),
        ("costs paid", f"{result.total_costs:.2%}"),
    ]
    lines = [
        f"Backtest: {result.strategy} on {result.symbol or '?'}",
        f"fees {config.fee_bps:.0f}bps + slippage {config.slippage_bps:.0f}bps, "
        f"execution lag {config.execution_lag} bar(s)",
        "-" * 44,
    ]
    lines += [f"{label:<18}{value:>14}" for label, value in rows]
    if result.params:
        lines += ["-" * 44, "params: " + ", ".join(f"{k}={v}" for k, v in result.params.items())]
    return "\n".join(lines)


def compare(results: list[BacktestResult]) -> pd.DataFrame:
    """Headline metrics for several backtests, side by side."""
    return pd.DataFrame(
        [
            {
                "strategy": r.strategy,
                "symbol": r.symbol,
                "total_return": r.total_return,
                "buy_and_hold": r.buy_and_hold_return,
                "excess": r.excess_return,
                "sharpe": r.sharpe,
                "max_drawdown": r.max_drawdown,
                "trades": r.trades,
                "exposure": r.exposure,
            }
            for r in results
        ]
    )
