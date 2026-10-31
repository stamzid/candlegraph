"""The Streamlit terminal.

Deliberately thin: every number on screen comes from
:mod:`candlegraph.ui.viewmodels`, which is plain Python and carries the
tests. This module is widgets, layout, and charts.

The layout is a trader's terminal, not a notebook: one dashboard renders at
a time (picked in the sidebar, so an ARIMA fit never runs while you are
reading the tape), every dashboard opens with a metric strip, and every
chart has a title that says what to look for. Units are stated where they
are entered: **rates are percent per year, trading costs are basis points.**

Run it with ``candlegraph ui`` (or ``streamlit run candlegraph/ui/app.py``).
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from candlegraph.analytics.arima import ArimaConfig
from candlegraph.analytics.portfolio import OBJECTIVES, PortfolioConfig
from candlegraph.analytics.rates import real_rate
from candlegraph.analytics.risk import METHODS, RiskConfig
from candlegraph.settings import Settings, get_settings
from candlegraph.storage import DatabaseManager, get_db
from candlegraph.strategies import available
from candlegraph.strategies.arima_signal import ArimaSignalConfig
from candlegraph.strategies.backtest import BacktestConfig
from candlegraph.strategies.bollinger import BollingerConfig
from candlegraph.strategies.crossover import CrossoverConfig
from candlegraph.ui.viewmodels import (
    BACKFILL_CHOICES,
    DEFAULT_BENCHMARK,
    MAX_WEIGHT_STEP,
    ViewError,
    available_symbols,
    build_agents_view,
    build_forecast,
    build_market_view,
    build_pair_view,
    build_portfolio_view,
    build_risk_view,
    build_strategy_view,
    build_token_view,
    candle_frame,
    default_index,
    default_max_weight,
    default_portfolio_symbols,
    forecast_frame,
    run_backfill,
    run_observer_now,
)

WINDOW_CHOICES = {"7 days": 7, "30 days": 30, "90 days": 90, "180 days": 180, "Everything": None}
PAGES = ("Market", "Token", "Risk", "Compare", "Portfolio", "Strategy", "Forecast", "Agents", "Data")
HORIZON_CHOICES = {"1 hour": 1, "4 hours": 4, "1 day": 24, "1 week": 168}
UP, DOWN = "#00c853", "#ff5252"
SEVERITY_COLORS = {
    "ok": "#00c853",
    "info": "#2962ff",
    "warning": "#ffab00",
    "critical": "#ff5252",
    "error": "#ff5252",
}


# Cached because the sidebar re-runs the whole script on every widget change,
# and re-querying 20k rows per keystroke makes the UI feel broken.
@st.cache_data(ttl=300, show_spinner=False)
def _symbols(_db: DatabaseManager) -> list[str]:
    return available_symbols(_db)


def _title(text: str, caption: str | None = None) -> None:
    """One chart, one headline. The caption is the footnote, not the title."""
    st.markdown(f"##### {text}")
    if caption:
        st.caption(caption)


def _plot(figure: go.Figure, height: int = 320) -> None:
    figure.update_layout(
        height=height,
        margin={"l": 10, "r": 10, "t": 10, "b": 10},
        legend={"orientation": "h", "y": 1.02, "x": 0.0},
    )
    st.plotly_chart(figure, width="stretch")


def _sidebar(settings: Settings, symbols: list[str]) -> dict[str, Any]:
    st.sidebar.title("candlegraph")
    st.sidebar.caption("Uniswap v3 hourly market data · deterministic analytics")

    page = st.sidebar.radio("Dashboard", PAGES, label_visibility="collapsed")
    label = st.sidebar.selectbox("History", list(WINDOW_CHOICES), index=2)

    st.sidebar.divider()
    st.sidebar.subheader("Rates — percent per year")
    rate_pct = st.sidebar.number_input(
        "Risk-free rate (% / yr)",
        min_value=-10.0,
        max_value=100.0,
        value=float(settings.risk_free_rate) * 100.0,
        step=0.25,
        format="%.2f",
        help="Percent, not basis points: enter 4.25 for a 4.25% annual yield "
        "(a T-bill-like rate). Feeds every Sharpe ratio and the tangency portfolio.",
    )
    inflation_pct = st.sidebar.number_input(
        "Inflation (% / yr)",
        min_value=-20.0,
        max_value=100.0,
        value=float(settings.inflation_rate) * 100.0,
        step=0.25,
        format="%.2f",
        help="Percent per year. Only used to show the real (inflation-adjusted) "
        "rate below; ratios use the nominal rate by convention.",
    )
    risk_free = rate_pct / 100.0
    st.sidebar.caption(
        f"Nominal {risk_free:.2%} → real {real_rate(risk_free, inflation_pct / 100.0):+.2%} after inflation"
    )

    st.sidebar.subheader("Trading costs — basis points")
    fee_bps = st.sidebar.number_input(
        "Fee (bps per side)",
        0.0,
        500.0,
        settings.backtest_fee_bps,
        5.0,
        help="Basis points: 100 bps = 1%. Uniswap v3 fee tiers run 5-100 bps.",
    )
    slippage_bps = st.sidebar.number_input(
        "Slippage (bps per side)",
        0.0,
        500.0,
        settings.backtest_slippage_bps,
        1.0,
        help="Basis points of extra cost per unit traded: 100 bps = 1%.",
    )

    return {
        "page": page,
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


def _pct(value: float) -> str:
    return "—" if pd.isna(value) else f"{value:.2%}"


# --- MARKET ------------------------------------------------------------------


def _market_page(db: DatabaseManager, settings: Settings, ctx: dict[str, Any]) -> None:
    st.subheader("Market — the whole tape")
    view = build_market_view(db, ctx["symbols"], ctx["days"], settings.analytics_interval_hours)

    snapshot = view.snapshot
    best, worst = snapshot.index[0], snapshot.index[-1]
    cols = st.columns(4)
    cols[0].metric("Tokens on tape", f"{len(snapshot)}")
    cols[1].metric("Hours on screen", f"{view.bars:,}")
    cols[2].metric(f"Top mover 24h — {best}", _pct(snapshot.loc[best, "change_24h"]))
    cols[3].metric(f"Laggard 24h — {worst}", _pct(snapshot.loc[worst, "change_24h"]))

    _title("Relative Strength — every token rebased to 100 at window start")
    st.line_chart(view.rebased, height=340)

    _title(
        "The Tape — movers, risk and flow at a glance",
        "Sorted by 24h move. VaR-free view: see the Risk dashboard for tails.",
    )
    st.dataframe(
        snapshot.style.format(
            {
                "last": "{:,.4f}",
                "change_24h": "{:+.2%}",
                "change_7d": "{:+.2%}",
                "change_30d": "{:+.2%}",
                "volatility": "{:.1%}",
                "max_drawdown": "{:.1%}",
                "volume_24h": "${:,.0f}",
            },
            na_rep="—",
        ).map(
            lambda v: f"color: {UP}" if isinstance(v, float) and v > 0 else f"color: {DOWN}",
            subset=["change_24h", "change_7d", "change_30d"],
        ),
        width="stretch",
    )

    left, right = st.columns([3, 2])
    with left:
        _title(
            "Co-Movement — hourly return correlation", "Aligned bars only: hours both tokens actually traded."
        )
        heat = go.Figure(
            go.Heatmap(
                z=view.correlation.to_numpy(),
                x=list(view.correlation.columns),
                y=list(view.correlation.index),
                zmin=-1,
                zmax=1,
                colorscale="RdBu",
                reversescale=True,
            )
        )
        _plot(heat, height=380)
    with right:
        _title("Volatility Ladder — annualised")
        ladder = snapshot["volatility"].dropna().sort_values()
        _plot(go.Figure(go.Bar(x=ladder.to_numpy(), y=list(ladder.index), orientation="h")), height=380)


# --- TOKEN -------------------------------------------------------------------


def _token_page(db: DatabaseManager, settings: Settings, ctx: dict[str, Any]) -> None:
    left, mid, right = st.columns(3)
    symbol = left.selectbox("Token", ctx["symbols"], index=default_index(ctx["symbols"]), key="token_symbol")
    window = int(mid.number_input("Rolling window (bars)", 2, 2000, settings.rolling_window, 1))
    num_std = float(right.number_input("Band width (std)", 0.5, 5.0, 2.0, 0.25))

    view = build_token_view(db, symbol, ctx["days"], window, num_std, settings.analytics_interval_hours)
    latest = view.latest

    cols = st.columns(5)
    cols[0].metric("Last", f"{latest['price']:,.4f}", delta=_pct(latest["change_24h"]) + " 24h")
    cols[1].metric("Realised vol (ann.)", _pct(latest.get("rolling_volatility", float("nan"))))
    cols[2].metric(
        "VaR 95% 1d", _pct(latest["var_1d"]), help="Historical. The loss exceeded one day in twenty."
    )
    cols[3].metric("Max drawdown", _pct(latest["max_drawdown"]))
    cols[4].metric("Bars", f"{view.bars:,}")

    _title(
        f"Price Action — {symbol} candles with Bollinger envelope",
        f"{view.window}-bar mean ± {num_std:g} std. Candles are resampled for readability on long windows.",
    )
    candles = candle_frame(view.ohlc)
    figure = go.Figure(
        go.Candlestick(
            x=candles.index,
            open=candles["open"],
            high=candles["high"],
            low=candles["low"],
            close=candles["close"],
            increasing_line_color=UP,
            decreasing_line_color=DOWN,
            name=symbol,
        )
    )
    for band, dash in (("upper", "dot"), ("middle", "dash"), ("lower", "dot")):
        figure.add_trace(
            go.Scatter(
                x=view.bands.index,
                y=view.bands[band],
                name=band,
                line={"width": 1, "dash": dash, "color": "#90a4ae"},
            )
        )
    figure.update_layout(xaxis_rangeslider_visible=False)
    _plot(figure, height=420)

    _title("Volume Pulse — USD traded per candle")
    _plot(go.Figure(go.Bar(x=candles.index, y=candles["volume_usd"], marker_color="#607d8b")), height=160)

    left, right = st.columns(2)
    with left:
        _title("Realised Volatility — rolling, annualised")
        st.line_chart(view.summary[["rolling_volatility"]], height=220)
        _title("Stretch — z-score vs its own band", "±2 is the classic mean-reversion trigger.")
        st.line_chart(view.bands[["zscore"]], height=220)
    with right:
        _title("Underwater — drawdown from peak")
        st.area_chart(view.drawdown.to_frame("drawdown"), height=220, color=DOWN)
        _title("Trend vs Price — rolling mean overlay")
        st.line_chart(view.summary[["price", "rolling_mean"]], height=220)


# --- RISK --------------------------------------------------------------------


def _risk_page(db: DatabaseManager, settings: Settings, ctx: dict[str, Any]) -> None:
    a, b, c, d, e = st.columns(5)
    symbol = a.selectbox("Token", ctx["symbols"], index=default_index(ctx["symbols"]), key="risk_symbol")
    default_benchmark = settings.risk_benchmark if settings.risk_benchmark in ctx["symbols"] else None
    others = ["(none)"] + [s for s in ctx["symbols"] if s != symbol]
    benchmark_pick = b.selectbox(
        "Benchmark", others, index=others.index(default_benchmark) if default_benchmark in others else 0
    )
    confidence = float(c.slider("Confidence", 0.90, 0.99, settings.risk_confidence, 0.01))
    horizon_label = d.selectbox("Horizon", list(HORIZON_CHOICES), index=2)
    method = e.selectbox("Estimator", METHODS, index=0)

    config = RiskConfig(
        confidence=confidence,
        horizon_bars=HORIZON_CHOICES[horizon_label],
        method=method,
        interval_hours=settings.analytics_interval_hours,
        risk_free_rate=ctx["risk_free"],
    )
    benchmark = None if benchmark_pick == "(none)" else benchmark_pick
    view = build_risk_view(db, symbol, benchmark, ctx["days"], config)
    report = view.report

    cols = st.columns(6)
    cols[0].metric(
        f"VaR {confidence:.0%} {horizon_label}",
        _pct(report.var),
        help="The loss exceeded with probability 1 - confidence. Positive number = loss.",
    )
    cols[1].metric(
        "CVaR (expected shortfall)",
        _pct(report.cvar),
        help="The average loss once the VaR is breached. The bill, not the threshold.",
    )
    cols[2].metric("Vol (ann.)", _pct(report.annualised_volatility))
    cols[3].metric("Max drawdown", _pct(report.max_drawdown))
    cols[4].metric("Sharpe", f"{report.sharpe:.2f}")
    cols[5].metric("Sortino", f"{report.sortino:.2f}")

    left, right = st.columns(2)
    with left:
        _title(
            "Loss Profile — hourly returns vs the VaR cut",
            f"{view.returns.size:,} bars. Skew {report.skew:+.2f}, "
            f"excess kurtosis {report.excess_kurtosis:+.2f}: "
            "fat tails make the normal estimator optimistic.",
        )
        per_bar_var = report.var / (config.horizon_bars**0.5)
        hist = go.Figure(go.Histogram(x=view.returns.to_numpy(), nbinsx=80, marker_color="#607d8b"))
        hist.add_vline(
            x=-per_bar_var, line_color=DOWN, annotation_text="VaR / bar", annotation_position="top left"
        )
        _plot(hist, height=300)

        _title(
            "Estimator Disagreement — the fat-tail check", "When the bars disagree, believe the bigger one."
        )
        est = view.estimators
        bars = go.Figure(
            [
                go.Bar(name="VaR", x=list(est.index), y=est["VaR"], marker_color="#ffab00"),
                go.Bar(name="CVaR", x=list(est.index), y=est["CVaR"], marker_color=DOWN),
            ]
        )
        bars.update_layout(barmode="group", yaxis_tickformat=".1%")
        _plot(bars, height=260)
    with right:
        _title("Underwater — drawdown from peak")
        st.area_chart(view.drawdown.to_frame("drawdown"), height=300, color=DOWN)

        split = report.decomposition
        if split is not None:
            _title(
                f"Risk Anatomy — market ({split.benchmark}) vs token",
                f"β {split.beta:.2f}, R² {split.r_squared:.2f} over {split.bars:,} aligned bars. "
                "The market share is the part diversification cannot remove.",
            )
            donut = go.Figure(
                go.Pie(
                    labels=[f"Systematic ({split.benchmark})", "Idiosyncratic"],
                    values=[split.systematic_share, split.idiosyncratic_share],
                    hole=0.55,
                    marker={"colors": ["#2962ff", "#90a4ae"]},
                )
            )
            _plot(donut, height=260)
        else:
            st.info("Pick a benchmark to split risk into market and token-specific parts.")

    st.caption(
        f"Risk-free rate {config.risk_free_rate:.2%}/yr (nominal, from the sidebar) "
        "feeds the Sharpe and Sortino above."
    )


# --- COMPARE -----------------------------------------------------------------


def _compare_page(db: DatabaseManager, settings: Settings, ctx: dict[str, Any]) -> None:
    a, b, c = st.columns(3)
    symbol_a = a.selectbox("Token A", ctx["symbols"], index=default_index(ctx["symbols"]), key="pair_a")
    others = [s for s in ctx["symbols"] if s != symbol_a] or ctx["symbols"]
    symbol_b = b.selectbox(
        "Token B", others, index=default_index(others, DEFAULT_BENCHMARK, exclude=symbol_a), key="pair_b"
    )
    window = int(c.number_input("Rolling window (bars)", 2, 2000, settings.rolling_window * 7, 1))

    view = build_pair_view(
        db, symbol_a, symbol_b, ctx["days"], window, settings.analytics_interval_hours, ctx["risk_free"]
    )
    result = view.comparison

    cols = st.columns(5)
    cols[0].metric("Correlation", f"{result.correlation:.3f}")
    cols[1].metric(f"Beta ({symbol_a} on {symbol_b})", f"{result.beta:.3f}")
    cols[2].metric("Tracking error (ann.)", _pct(result.tracking_error))
    cols[3].metric(f"Relative return {symbol_a} - {symbol_b}", _pct(result.relative_return))
    cols[4].metric("Aligned bars", f"{result.bars:,}")

    _title(
        f"Relative Value — {symbol_a} priced in {symbol_b}",
        "A rising line means A outruns B. Hours only one token traded are excluded, not filled.",
    )
    st.line_chart(result.frame[["ratio"]], height=280)

    left, right = st.columns(2)
    with left:
        _title(
            "Correlation Regime — rolling", f"{window}-bar window. A regime break here breaks a pairs trade."
        )
        st.line_chart(result.frame[["rolling_correlation"]], height=280)
    with right:
        _title("Beta Cloud — hourly returns", "The slope of the red line is beta.")
        points = view.scatter
        cloud = go.Figure(
            go.Scattergl(
                x=points.iloc[:, 0],
                y=points.iloc[:, 1],
                mode="markers",
                marker={"size": 4, "color": "#607d8b", "opacity": 0.45},
                name="bars",
            )
        )
        line = view.beta_line
        if not line.empty:
            cloud.add_trace(
                go.Scatter(
                    x=line["x"], y=line["y"], mode="lines", line={"color": DOWN, "width": 2}, name="β fit"
                )
            )
        cloud.update_layout(xaxis_title=symbol_b, yaxis_title=symbol_a)
        _plot(cloud, height=280)


# --- PORTFOLIO ---------------------------------------------------------------


def _portfolio_page(db: DatabaseManager, settings: Settings, ctx: dict[str, Any]) -> None:
    chosen = st.multiselect(
        "Tokens",
        ctx["symbols"],
        default=default_portfolio_symbols(ctx["symbols"]),
        help="Majors first, stablecoins left out by default: "
        "a USD-pegged token swallows any minimum-risk objective.",
    )
    left, right, third, fourth = st.columns(4)
    objective = left.selectbox("Objective", OBJECTIVES, index=0)
    max_weight = right.slider(
        "Max weight per token",
        MAX_WEIGHT_STEP,
        1.0,
        default_max_weight(len(chosen)),
        MAX_WEIGHT_STEP,
        help="Opens at 25% (or the smallest cap the chosen tokens can satisfy).",
    )
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
        target = st.number_input("Target return (% / yr)", -99.0, 500.0, 20.0, 5.0) / 100.0

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
    risk = RiskConfig(
        confidence=settings.risk_confidence,
        horizon_bars=settings.risk_horizon_bars,
        interval_hours=settings.analytics_interval_hours,
        risk_free_rate=ctx["risk_free"],
    )
    view = build_portfolio_view(db, chosen, ctx["days"], config, frontier_points, risk)
    result = view.result

    cols = st.columns(6)
    cols[0].metric("Expected return (ann.)", _pct(result.expected_return))
    cols[1].metric("Volatility (ann.)", _pct(result.volatility))
    cols[2].metric(
        "Sharpe",
        f"{result.sharpe:.2f}",
        help=f"At the sidebar's {result.risk_free_rate:.2%}/yr risk-free rate.",
    )
    cols[3].metric(
        f"VaR {settings.risk_confidence:.0%} 1d",
        _pct(view.var),
        help="Of the solved allocation, on its weighted return series.",
    )
    cols[4].metric("CVaR", _pct(view.cvar))
    cols[5].metric("Aligned bars", f"{view.bars:,}")

    if not result.converged:
        st.warning(f"The optimiser did not converge: {result.message}")
    if result.capped:
        st.info(
            "Expected return shown as -100%: the annualised estimate was lower, and a long-only book "
            "cannot lose more than everything. The window contains a collapsing token."
        )

    left, right = st.columns(2)
    with left:
        _title("Capital Allocation — where the money sits")
        weights = view.weights_frame
        _plot(
            go.Figure(go.Pie(labels=list(weights.index), values=weights["weight"].abs(), hole=0.55)),
            height=300,
        )
    with right:
        _title(
            "Risk Budget — who carries the book",
            "Each asset's share of portfolio volatility. Equal slices = risk parity.",
        )
        budget = view.risk_contributions
        if budget.empty:
            st.info("No risk budget for a single-asset book.")
        else:
            bar = go.Figure(
                go.Bar(x=budget["risk_share"], y=list(budget.index), orientation="h", marker_color="#2962ff")
            )
            bar.update_layout(xaxis_tickformat=".0%")
            _plot(bar, height=300)

    if view.frontier:
        _title(
            "Efficient Frontier — the price of return",
            "Each dot is the cheapest volatility for its return. ★ is your allocation.",
        )
        frontier = view.frontier_frame
        fig = go.Figure(
            go.Scatter(
                x=frontier["volatility"],
                y=frontier["expected_return"],
                mode="lines+markers",
                marker={
                    "color": frontier["sharpe"],
                    "colorscale": "Viridis",
                    "showscale": True,
                    "colorbar": {"title": "Sharpe"},
                },
                name="frontier",
            )
        )
        fig.add_trace(
            go.Scatter(
                x=[result.volatility],
                y=[result.expected_return],
                mode="markers+text",
                marker={"symbol": "star", "size": 16, "color": "#ffab00"},
                text=[result.objective],
                textposition="top center",
                name="chosen",
            )
        )
        fig.update_layout(
            xaxis_title="volatility (ann.)",
            yaxis_title="return (ann.)",
            xaxis_tickformat=".0%",
            yaxis_tickformat=".0%",
        )
        _plot(fig, height=360)

    st.dataframe(view.weights_frame.style.format({"weight": "{:.2%}"}), width="stretch")


# --- STRATEGY ----------------------------------------------------------------


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


def _strategy_page(db: DatabaseManager, ctx: dict[str, Any]) -> None:
    left, right = st.columns(2)
    symbol = left.selectbox(
        "Token", ctx["symbols"], index=default_index(ctx["symbols"]), key="strategy_symbol"
    )
    name = right.selectbox("Strategy", available(), index=1)

    config = _strategy_config(name)
    if name == "arima":
        st.caption("ARIMA refits on every cadence bar, so a long window takes a moment.")

    with st.spinner("Running…"):
        view = build_strategy_view(db, symbol, name, ctx["days"], config, ctx["backtest"])

    report = view.report
    cols = st.columns(6)
    cols[0].metric("Total return", _pct(report.total_return))
    cols[1].metric("vs buy & hold", _pct(report.buy_and_hold_return), delta=f"{report.excess_return:+.2%}")
    cols[2].metric("Sharpe", f"{report.sharpe:.2f}")
    cols[3].metric("Max drawdown", _pct(report.max_drawdown))
    cols[4].metric("Trades", f"{report.trades:,}")
    cols[5].metric(
        "Costs paid",
        _pct(report.total_costs),
        help="Fees + slippage as a fraction of capital. If this beats the excess, there is no edge.",
    )

    _title(
        "Equity — strategy vs buy & hold, net of costs",
        "If the orange line is below the blue one, holding was better.",
    )
    st.line_chart(view.equity_frame, height=320)

    left, right = st.columns(2)
    with left:
        _title("Exposure Timeline — long, flat, short")
        st.area_chart(report.frame[["position"]], height=200)
    with right:
        _title("Underwater — strategy drawdown")
        st.area_chart(report.frame[["drawdown"]], height=200, color=DOWN)

    _title(f"Trade Ledger — every position change (latest signal: {view.strategy.latest.name})")
    st.dataframe(view.trades_frame.tail(30).style.format({"price": "{:,.4f}"}), width="stretch")


# --- FORECAST ----------------------------------------------------------------


def _forecast_page(db: DatabaseManager, ctx: dict[str, Any]) -> None:
    a, b, c, d = st.columns(4)
    symbol = a.selectbox("Token", ctx["symbols"], index=default_index(ctx["symbols"]), key="forecast_symbol")
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
        f"Forecast (+{steps}h)", f"{forecast.predicted:,.4f}", delta=f"{forecast.expected_return:+.2%}"
    )
    cols[2].metric("Order", f"ARIMA{forecast.order}")
    cols[3].metric(
        "Interval width",
        _pct(forecast.interval_width),
        help="Interval span over price. Wide = the model is saying it does not know.",
    )

    _title(
        f"Projection — ARIMA path, {confidence:.0%} interval at the horizon",
        f"Interval at +{steps}h: {forecast.lower:,.4f} .. {forecast.upper:,.4f}. "
        "A wide interval is not worth trading on.",
    )
    history = prices.tail(min(len(prices), 24 * 14))
    path = forecast_frame(prices, forecast)
    fig = go.Figure(
        go.Scatter(x=history.index, y=history.to_numpy(), name="price", line={"color": "#607d8b"})
    )
    fig.add_trace(
        go.Scatter(
            x=path.index, y=path["forecast"], name="forecast", line={"color": "#ffab00", "dash": "dash"}
        )
    )
    horizon = path.index[-1]
    fig.add_trace(
        go.Scatter(
            x=[horizon, horizon],
            y=[forecast.lower, forecast.upper],
            mode="lines+markers",
            line={"color": "#ffab00", "width": 1},
            marker={"symbol": "line-ew-open", "size": 10},
            name=f"{confidence:.0%} interval",
        )
    )
    _plot(fig, height=360)


# --- AGENTS ------------------------------------------------------------------


def _agents_page(db: DatabaseManager, settings: Settings) -> None:
    st.subheader("Watchtower — is the data fresh, complete, and still the same market?")

    if st.button("▶ Run observer now", type="primary", help="One observer run, written to the agent ledger."):
        with st.spinner("Observing…"):
            report = run_observer_now(settings, db)
        color = SEVERITY_COLORS.get(report.status_label, "#90a4ae")
        st.markdown(
            f"**Observer finished :{'red' if color == DOWN else 'blue'}"
            f"[{report.status_label.upper()}]** — {report.summary}"
        )
        _symbols.clear()

    view = build_agents_view(db)

    status = view.status
    cols = st.columns(4)
    cols[0].metric("Latest observer verdict", status.upper())
    cols[1].metric("Agent runs on record", f"{len(view.agent_runs):,}")
    cols[2].metric("Ingest runs on record", f"{len(view.ingest_runs):,}")
    worst = view.coverage.iloc[0] if not view.coverage.empty else None
    cols[3].metric(
        "Thinnest coverage",
        f"{worst['coverage']:.0%} {worst.name}" if worst is not None else "—",
        help="Low coverage on a thin token is the market, not a fault.",
    )

    _title(
        "Findings — latest observer report",
        "Severity is computed from the numbers; the prose is only a summary.",
    )
    if view.findings.empty:
        st.info("No observer report yet. Press “Run observer now” above.")
    else:
        st.dataframe(
            view.findings.style.map(
                lambda v: f"color: {SEVERITY_COLORS.get(str(v), '#90a4ae')}; font-weight: bold",
                subset=["severity"],
            ),
            width="stretch",
        )

    left, right = st.columns(2)
    with left:
        _title(
            "Ingest Cadence — rows written per run",
            "A flat stretch of zeros on a liquid token is worth a look.",
        )
        if view.ingest_runs.empty:
            st.info("No ingest runs recorded yet.")
        else:
            runs = view.ingest_runs.iloc[::-1]
            fig = go.Figure(
                [
                    go.Bar(
                        x=runs["started_at"],
                        y=runs["token_hours_written"],
                        name="token hours",
                        marker_color="#2962ff",
                    ),
                    go.Bar(
                        x=runs["started_at"],
                        y=runs["pool_hours_written"],
                        name="pool hours",
                        marker_color="#90a4ae",
                    ),
                ]
            )
            fig.update_layout(barmode="stack")
            _plot(fig, height=260)
    with right:
        _title(
            "Coverage — stored vs expected hours", "100% is not the target: an hour with no swaps has no row."
        )
        if view.coverage.empty:
            st.info("No stored history yet.")
        else:
            fig = go.Figure(
                go.Bar(
                    x=view.coverage["coverage"],
                    y=list(view.coverage.index),
                    orientation="h",
                    marker_color=[
                        "#ffab00" if v < settings.min_expected_coverage else UP
                        for v in view.coverage["coverage"]
                    ],
                )
            )
            fig.update_layout(xaxis_tickformat=".0%")
            _plot(fig, height=260)

    _title("Run History — every agent report, newest first")
    if view.agent_runs.empty:
        st.info("No agent runs recorded yet.")
    else:
        st.dataframe(
            view.agent_runs.style.map(
                lambda v: f"color: {SEVERITY_COLORS.get(str(v), '#90a4ae')}; font-weight: bold",
                subset=["status"],
            ),
            width="stretch",
        )


# --- DATA ---------------------------------------------------------------------


def _data_page(db: DatabaseManager, settings: Settings) -> None:
    st.subheader("Data — what is stored, and loading more of it")
    st.caption(
        f"A first-time token is fetched {settings.lookback_days} days back (LOOKBACK_DAYS). "
        "A backfill reaches further; it rewinds past what is stored and re-writes nothing twice."
    )

    view = build_agents_view(db)
    cols = st.columns(3)
    cols[0].metric("Tokens with history", f"{len(view.coverage):,}")
    cols[1].metric("Ingest runs on record", f"{len(view.ingest_runs):,}")
    newest = view.ingest_runs.iloc[0] if not view.ingest_runs.empty else None
    cols[2].metric(
        "Last run",
        f"{newest['status']}" if newest is not None else "—",
        delta=f"{int(newest['token_hours_written']):,} token hours" if newest is not None else None,
        delta_color="off",
    )

    _title("Backfill — load more history", "Pick a window, then confirm. Nothing starts until you do.")
    left, right = st.columns([1, 2])
    label = left.selectbox("Window", list(BACKFILL_CHOICES), index=2, key="backfill_window")
    days = BACKFILL_CHOICES[label]

    if left.button("Backfill…", key="backfill_ask"):
        st.session_state["backfill_pending"] = days

    pending = st.session_state.get("backfill_pending")
    if pending is not None:
        with right:
            st.warning(
                f"**Backfill {pending} days for {len(view.coverage) or 'all configured'} tokens?** "
                f"This calls The Graph for roughly {pending * 24:,} hours per token and per pool, "
                "takes a few minutes per 90 days, and writes to the database. Stored rows are only "
                "refreshed, never deleted."
            )
            confirm, cancel = st.columns(2)
            if confirm.button("Confirm backfill", type="primary", key="backfill_confirm"):
                st.session_state.pop("backfill_pending", None)
                try:
                    with st.spinner(f"Backfilling {pending} days…"):
                        result = run_backfill(settings, db, int(pending))
                except ViewError as exc:
                    st.error(str(exc))
                else:
                    _symbols.clear()
                    tone = st.success if result.ok else st.warning
                    tone(
                        f"Backfill finished: {result.hours_written:,} token hours and "
                        f"{result.pool_hours_written:,} pool hours written"
                        + (f", {len(result.errors)} error(s)" if result.errors else "")
                        + ". Reload the other dashboards to see the new history."
                    )
                    for target, message in result.errors.items():
                        st.caption(f"{target}: {message}")
            if cancel.button("Cancel", key="backfill_cancel"):
                st.session_state.pop("backfill_pending", None)
                st.rerun()

    left, right = st.columns(2)
    with left:
        _title(
            "Coverage — stored vs expected hours", "100% is not the target: an hour with no swaps has no row."
        )
        if view.coverage.empty:
            st.info("No stored history yet.")
        else:
            fig = go.Figure(
                go.Bar(
                    x=view.coverage["coverage"],
                    y=list(view.coverage.index),
                    orientation="h",
                    marker_color=[
                        "#ffab00" if v < settings.min_expected_coverage else UP
                        for v in view.coverage["coverage"]
                    ],
                )
            )
            fig.update_layout(xaxis_tickformat=".0%")
            _plot(fig, height=280)
    with right:
        _title("Recent Ingest Runs — newest first")
        if view.ingest_runs.empty:
            st.info("No ingest runs recorded yet.")
        else:
            st.dataframe(view.ingest_runs, width="stretch")


# --- entry point --------------------------------------------------------------


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

    try:
        match ctx["page"]:
            case "Market":
                _market_page(db, settings, ctx)
            case "Token":
                _token_page(db, settings, ctx)
            case "Risk":
                _risk_page(db, settings, ctx)
            case "Compare":
                _compare_page(db, settings, ctx)
            case "Portfolio":
                _portfolio_page(db, settings, ctx)
            case "Strategy":
                _strategy_page(db, ctx)
            case "Forecast":
                _forecast_page(db, ctx)
            case "Agents":
                _agents_page(db, settings)
            case "Data":
                _data_page(db, settings)
    except ViewError as exc:
        st.info(str(exc))


# Streamlit executes this script with ``__name__ == "__main__"``, so the guard
# both runs the app and keeps the module importable by the tests.
if __name__ == "__main__":
    main()
