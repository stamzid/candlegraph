"""The Streamlit UI.

Deliberately thin: every number on screen comes from
:mod:`candlegraph.ui.viewmodels`, which is plain Python and carries the
tests. This module is widgets, layout, and charts.

Run it with ``candlegraph ui`` (or ``streamlit run candlegraph/ui/app.py``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pandas as pd
import streamlit as st

from candlegraph.analytics.arima import ArimaConfig
from candlegraph.analytics.portfolio import OBJECTIVES, PortfolioConfig
from candlegraph.settings import Settings, get_settings
from candlegraph.storage import DatabaseManager, get_db
from candlegraph.strategies import available
from candlegraph.strategies.arima_signal import ArimaSignalConfig
from candlegraph.strategies.backtest import BacktestConfig
from candlegraph.strategies.bollinger import BollingerConfig
from candlegraph.strategies.crossover import CrossoverConfig
from candlegraph.ui.viewmodels import (
    ViewError,
    available_symbols,
    build_forecast,
    build_portfolio_view,
    build_price_view,
    build_strategy_view,
    forecast_frame,
)

WINDOW_CHOICES = {"7 days": 7, "30 days": 30, "90 days": 90, "180 days": 180, "Everything": None}


# Cached because the sidebar re-runs the whole script on every widget change,
# and re-querying 20k rows per keystroke makes the UI feel broken.
@st.cache_data(ttl=300, show_spinner=False)
def _symbols(_db: DatabaseManager) -> list[str]:
    return available_symbols(_db)


def _sidebar(settings: Settings, symbols: list[str]) -> dict[str, Any]:
    st.sidebar.title("candlegraph")
    st.sidebar.caption("Uniswap v3 hourly market data and deterministic analytics")

    label = st.sidebar.selectbox("History", list(WINDOW_CHOICES), index=2)
    st.sidebar.divider()
    st.sidebar.subheader("Risk")
    risk_free = st.sidebar.number_input(
        "Risk-free rate (annual)",
        min_value=-0.10,
        max_value=1.00,
        value=float(settings.risk_free_rate),
        step=0.005,
        format="%.3f",
        help="Used for every Sharpe ratio and for the tangency portfolio.",
    )
    fee_bps = st.sidebar.number_input("Fee (bps per side)", 0.0, 500.0, settings.backtest_fee_bps, 5.0)
    slippage_bps = st.sidebar.number_input("Slippage (bps)", 0.0, 500.0, settings.backtest_slippage_bps, 1.0)

    return {
        "days": WINDOW_CHOICES[label],
        "risk_free": risk_free,
        "symbols": symbols,
        "backtest": BacktestConfig(
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            risk_free_rate=risk_free,
            interval_hours=settings.analytics_interval_hours,
        ),
    }


def _prices_tab(db: DatabaseManager, settings: Settings, ctx: dict[str, Any]) -> None:
    st.subheader("Price, rolling mean and volatility")
    left, right, mid = st.columns(3)
    symbol = left.selectbox("Token", ctx["symbols"], key="price_symbol")
    window = right.number_input("Rolling window (bars)", 2, 2000, settings.rolling_window, 1)
    num_std = mid.number_input("Band width (std)", 0.5, 5.0, 2.0, 0.25)

    view = build_price_view(
        db, symbol, ctx["days"], int(window), float(num_std), settings.analytics_interval_hours
    )
    latest = view.latest

    cols = st.columns(4)
    cols[0].metric("Last price", f"{latest['price']:,.4f}")
    cols[1].metric("Rolling mean", f"{latest.get('rolling_mean', 0):,.4f}")
    cols[2].metric("Volatility (annual)", f"{latest.get('rolling_volatility', 0):.1%}")
    cols[3].metric("Bars", f"{view.bars:,}")

    st.line_chart(view.summary[["price", "rolling_mean"]], height=300)
    st.caption(f"Rolling annualised volatility over a {view.window}-bar window")
    st.line_chart(view.summary[["rolling_volatility"]], height=200)

    st.caption("Bollinger bands")
    st.line_chart(view.bands[["middle", "upper", "lower"]].assign(price=view.prices), height=300)


def _portfolio_tab(db: DatabaseManager, settings: Settings, ctx: dict[str, Any]) -> None:
    st.subheader("Mean-variance portfolio")

    chosen = st.multiselect("Tokens", ctx["symbols"], default=ctx["symbols"][: min(5, len(ctx["symbols"]))])
    left, right, third, fourth = st.columns(4)
    objective = left.selectbox("Objective", OBJECTIVES, index=0)
    max_weight = right.slider("Max weight per token", 0.05, 1.0, 1.0, 0.05)
    shrinkage = third.slider(
        "Covariance shrinkage",
        0.0,
        1.0,
        0.0,
        0.05,
        help="Pull the covariance towards its diagonal. A short window is noisy.",
    )
    allow_short = fourth.checkbox("Allow shorting", value=False)

    target = None
    if objective == "target_return":
        target = st.number_input("Target return (annual)", -1.0, 5.0, 0.20, 0.05)

    frontier_points = st.slider("Frontier points (0 to skip)", 0, 40, 20, 5)

    config = PortfolioConfig(
        risk_free_rate=ctx["risk_free"],
        objective=objective,
        allow_short=allow_short,
        max_weight=max_weight,
        target_return=target,
        interval_hours=settings.analytics_interval_hours,
        shrinkage=shrinkage,
    )
    view = build_portfolio_view(db, chosen, ctx["days"], config, frontier_points)
    result = view.result

    cols = st.columns(4)
    cols[0].metric("Expected return", f"{result.expected_return:.2%}")
    cols[1].metric("Volatility", f"{result.volatility:.2%}")
    cols[2].metric("Sharpe", f"{result.sharpe:.3f}")
    cols[3].metric("Aligned bars", f"{view.bars:,}")

    if not result.converged:
        st.warning(f"The optimiser did not converge: {result.message}")

    left, right = st.columns([1, 2])
    with left:
        st.caption("Weights")
        st.bar_chart(view.weights_frame, height=320)
    with right:
        if view.frontier:
            st.caption("Efficient frontier")
            st.scatter_chart(
                view.frontier_frame, x="volatility", y="expected_return", color="sharpe", height=320
            )
        else:
            st.caption("Raise the frontier slider to trace the frontier.")

    st.dataframe(view.weights_frame.style.format({"weight": "{:.2%}"}), width="stretch")


def _strategy_config(name: str) -> Any:
    """Per-strategy widgets, so each one exposes only its own knobs."""
    match name:
        case "bollinger":
            a, b, c, d = st.columns(4)
            return BollingerConfig(
                window=int(a.number_input("Window", 2, 500, 20, 1)),
                num_std=float(b.number_input("Band std", 0.5, 5.0, 2.0, 0.25)),
                entry_z=float(c.number_input("Entry z", 0.1, 6.0, 2.0, 0.1)),
                exit_z=float(d.number_input("Exit z", 0.0, 5.9, 0.5, 0.1)),
                allow_short=st.checkbox("Allow shorting", value=True, key="boll_short"),
            )
        case "crossover":
            a, b, c = st.columns(3)
            return CrossoverConfig(
                fast=int(a.number_input("Fast", 2, 500, 12, 1)),
                slow=int(b.number_input("Slow", 3, 2000, 48, 1)),
                min_separation=float(c.number_input("Min separation", 0.0, 0.5, 0.0, 0.005)),
                allow_short=st.checkbox("Allow shorting", value=False, key="cross_short"),
            )
        case "arima":
            a, b, c, d = st.columns(4)
            p = int(a.number_input("p", 0, 5, 1, 1))
            q = int(b.number_input("q", 0, 5, 1, 1))
            return ArimaSignalConfig(
                arima=ArimaConfig(order=(p, 1, q)),
                threshold=float(c.number_input("Threshold", 0.0, 0.1, 0.002, 0.0005, format="%.4f")),
                refit_every=int(d.number_input("Refit every (bars)", 1, 500, 24, 1)),
                allow_short=st.checkbox("Allow shorting", value=True, key="arima_short"),
            )
        case _:
            return None


def _strategies_tab(db: DatabaseManager, ctx: dict[str, Any]) -> None:
    st.subheader("Strategy signals and backtest")

    left, right = st.columns(2)
    symbol = left.selectbox("Token", ctx["symbols"], key="strategy_symbol")
    name = right.selectbox("Strategy", available(), index=1)

    config = _strategy_config(name)
    if name == "arima":
        st.caption("ARIMA refits on every cadence bar, so a long window takes a moment.")

    with st.spinner("Running…"):
        view = build_strategy_view(db, symbol, name, ctx["days"], config, ctx["backtest"])

    report = view.report
    cols = st.columns(5)
    cols[0].metric("Total return", f"{report.total_return:.2%}")
    cols[1].metric("vs buy & hold", f"{report.buy_and_hold_return:.2%}", delta=f"{report.excess_return:+.2%}")
    cols[2].metric("Sharpe", f"{report.sharpe:.2f}")
    cols[3].metric("Max drawdown", f"{report.max_drawdown:.2%}")
    cols[4].metric("Trades", f"{report.trades:,}")

    st.caption("Equity curve, net of fees and slippage")
    st.line_chart(view.equity_frame, height=320)

    left, right = st.columns(2)
    with left:
        st.caption("Position")
        st.area_chart(report.frame[["position"]], height=200)
    with right:
        st.caption("Drawdown")
        st.area_chart(report.frame[["drawdown"]], height=200)

    st.caption(f"Latest signal: **{view.strategy.latest.name}**")
    with st.expander("Recent bars"):
        st.dataframe(view.strategy.frame.tail(50), width="stretch")


def _forecast_tab(db: DatabaseManager, ctx: dict[str, Any]) -> None:
    st.subheader("ARIMA forecast")

    a, b, c, d = st.columns(4)
    symbol = a.selectbox("Token", ctx["symbols"], key="forecast_symbol")
    p = int(b.number_input("p", 0, 5, 1, 1, key="f_p"))
    q = int(c.number_input("q", 0, 5, 1, 1, key="f_q"))
    steps = int(d.number_input("Steps ahead", 1, 168, 24, 1))

    left, right = st.columns(2)
    auto = left.checkbox("Pick p and q by AIC", value=False)
    confidence = right.slider("Confidence", 0.50, 0.99, 0.95, 0.01)

    with st.spinner("Fitting…"):
        prices, forecast = build_forecast(
            db,
            symbol,
            ctx["days"],
            ArimaConfig(order=(p, 1, q), auto_order=auto, forecast_steps=steps, confidence=confidence),
        )

    if not forecast.ok:
        st.error(f"No forecast: {forecast.message}")
        return

    cols = st.columns(4)
    cols[0].metric("Last price", f"{forecast.last_price:,.4f}")
    cols[1].metric(
        f"Forecast (+{steps}h)",
        f"{forecast.predicted:,.4f}",
        delta=f"{forecast.expected_return:+.2%}",
    )
    cols[2].metric("Order", f"ARIMA{forecast.order}")
    cols[3].metric("Interval width", f"{forecast.interval_width:.2%}")

    history = prices.tail(min(len(prices), 24 * 14)).to_frame("price")
    st.line_chart(pd.concat([history, forecast_frame(prices, forecast)]), height=340)
    st.caption(
        f"{confidence:.0%} interval at the horizon: "
        f"{forecast.lower:,.4f} .. {forecast.upper:,.4f}. "
        "A wide interval means the point forecast is not worth trading on."
    )


def main() -> None:
    """Render the app. Called by Streamlit on every interaction."""
    st.set_page_config(page_title="candlegraph", page_icon="📈", layout="wide")

    settings = get_settings()
    db = get_db()

    try:
        symbols = _symbols(db)
    except Exception as exc:
        st.error(f"Cannot reach the database: {exc}")
        st.stop()

    if not symbols:
        st.warning("No price history stored yet. Run `make backfill days=90` and reload.")
        st.stop()

    ctx = _sidebar(settings, symbols)

    prices, portfolio, strategies, forecast = st.tabs(
        ["Prices & volatility", "Portfolio", "Strategies", "Forecast"]
    )
    renderers: list[tuple[Any, Callable[[], None]]] = [
        (prices, lambda: _prices_tab(db, settings, ctx)),
        (portfolio, lambda: _portfolio_tab(db, settings, ctx)),
        (strategies, lambda: _strategies_tab(db, ctx)),
        (forecast, lambda: _forecast_tab(db, ctx)),
    ]
    for tab, render in renderers:
        with tab:
            try:
                render()
            except ViewError as exc:
                st.info(str(exc))


# Streamlit executes this script with ``__name__ == "__main__"``, so the guard
# both runs the app and keeps the module importable by the tests.
if __name__ == "__main__":
    main()
