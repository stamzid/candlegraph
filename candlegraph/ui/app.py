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

from dataclasses import replace
from datetime import timedelta
from typing import Any, cast

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from candlegraph.agents.stages import format_stage
from candlegraph.analytics.arima import ArimaConfig
from candlegraph.analytics.portfolio import OBJECTIVES, Objective, PortfolioConfig
from candlegraph.analytics.prices import DEFAULT_MAX_FILL, fill_rule
from candlegraph.analytics.rates import real_rate
from candlegraph.analytics.risk import METHODS, Method, RiskConfig
from candlegraph.analytics.simulation import GENERATOR_NAMES, GENERATORS
from candlegraph.ingestion.fills import METHOD_POLICY, METHOD_SUMMARY
from candlegraph.ingestion.fills import METHODS as FILL_METHODS
from candlegraph.settings import Settings, get_settings
from candlegraph.storage import DatabaseManager, get_db
from candlegraph.strategies import available
from candlegraph.strategies.arima_signal import ArimaSignalConfig
from candlegraph.strategies.backtest import BacktestConfig
from candlegraph.strategies.bollinger import BollingerConfig
from candlegraph.strategies.crossover import CrossoverConfig
from candlegraph.strategies.montecarlo import SAMPLE_CAVEAT
from candlegraph.ui.viewmodels import (
    BACKFILL_CHOICES,
    DEFAULT_PAIR,
    MAX_SHADED_GAPS,
    MAX_WEIGHT_STEP,
    PIPELINES_REFRESH_SECONDS,
    Preferences,
    ViewError,
    age_text,
    available_symbols,
    build_agents_view,
    build_execution_view,
    build_forecast,
    build_lag_demonstration,
    build_market_view,
    build_method_scores,
    build_news_view,
    build_pair_view,
    build_pipelines_view,
    build_portfolio_view,
    build_posterior_forecast,
    build_quality_view,
    build_risk_view,
    build_simulation_view,
    build_strategy_comparison,
    build_strategy_view,
    build_token_view,
    candle_frame,
    default_index,
    default_max_weight,
    default_portfolio_symbols,
    estimate_pipeline_spend,
    execution_walkthrough,
    forecast_frame,
    gap_lane_segments,
    generator_catalogue,
    load_stored_explanation,
    longest_gaps,
    method_catalogue,
    posterior_diagnostics_frame,
    posterior_interval_frame,
    range_lane_segments,
    request_finished,
    request_pipeline_run,
    run_backfill,
    run_explain_now,
    run_fill_refresh_now,
    run_news_fetch_now,
    run_observer_now,
)

WINDOW_CHOICES = {"7 days": 7, "30 days": 30, "90 days": 90, "180 days": 180, "Everything": None}
PAGES = (
    "Market",
    "Token",
    "Quality",
    "Risk",
    "Compare",
    "Portfolio",
    "Strategy",
    "Simulation",
    "Forecast",
    "Agents",
    "Pipelines",
    "News",
    "Data",
)
HORIZON_CHOICES = {"1 hour": 1, "4 hours": 4, "1 day": 24, "1 week": 168}
UP, DOWN = "#00c853", "#ff5252"
SEVERITY_COLORS = {
    "ok": "#00c853",
    "info": "#2962ff",
    "warning": "#ffab00",
    "critical": "#ff5252",
    "error": "#ff5252",
}
# Run outcomes and queue states share one palette with the severities, so
# green, amber and red mean the same thing on every page.
OUTCOME_COLORS = {
    "succeeded": "#00c853",
    "done": "#00c853",
    "partial": "#ffab00",
    "failed": "#ff5252",
    "dead": "#ff5252",
    "running": "#2962ff",
    "queued": "#2962ff",
    "claimed": "#2962ff",
    "skipped": "#90a4ae",
}


# Cached because the sidebar re-runs the whole script on every widget change,
# and re-querying 20k rows per keystroke makes the UI feel broken.
@st.cache_data(ttl=300, show_spinner=False)
def _symbols(_db: DatabaseManager) -> list[str]:
    return available_symbols(_db)


def _prefs() -> Preferences:
    """The remembered choices for this browser session."""
    return Preferences(st.session_state)


def _pick(container: Any, label: str, options: list[str], name: str, fallback: int = 0, **kwargs: Any) -> str:
    """A selectbox that reopens on whatever was chosen last time.

    Streamlit discards a widget's own state as soon as a rerun does not
    render it, so a dashboard switch would otherwise reset every control on
    the page you left. The mirror in :class:`Preferences` is what survives,
    and the widget's index is read back out of it.
    """
    index = _prefs().index(name, options, fallback)
    chosen = container.selectbox(label, options, index=index, key=f"w_{name}", **kwargs)
    return str(_prefs().remember(name, chosen))


def _pick_many(
    container: Any, label: str, options: list[str], name: str, fallback: list[str], **kwargs: Any
) -> list[str]:
    """:func:`_pick` for a multiselect."""
    default = _prefs().multiselect(name, options, fallback)
    chosen = container.multiselect(label, options, default=default, key=f"w_{name}", **kwargs)
    return list(_prefs().remember(name, list(chosen)))


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

    # These two are rendered on every run, so Streamlit keeps their state by
    # itself; they need no mirror, and counting them would report selections
    # the user never made.
    page = st.sidebar.radio("Dashboard", PAGES, label_visibility="collapsed", key="nav_page")
    label = st.sidebar.selectbox("History", list(WINDOW_CHOICES), index=2, key="nav_history")

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

    st.sidebar.divider()
    remembered = len(_prefs().names())
    if st.sidebar.button(
        "↺ Reset to defaults",
        width="stretch",
        disabled=remembered == 0,
        help="Forget the tokens, windows and strategies picked this session and reopen "
        "every dashboard on its default. Stored data is not touched.",
    ):
        _prefs().reset()
        # The widgets themselves hold a copy; clearing only the mirror would
        # let them re-seed it on the very next run. `nav_` keys are left
        # alone so the reset does not also throw you off the current page.
        for key in [k for k in list(st.session_state) if str(k).startswith("w_")]:
            del st.session_state[key]
        st.rerun()
    st.sidebar.caption(
        f"{remembered} selection(s) remembered this session."
        if remembered
        else "Selections you make are remembered while this tab is open."
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


def _token_price_chart(view: Any, candles: pd.DataFrame, symbol: str, num_std: float) -> None:
    """Candles, the Bollinger envelope, shaded gaps and the chosen estimate.

    The shading and the dashed estimate are drawn here rather than in
    the page so the ordering stays explicit: gaps go underneath, the
    uncertainty ribbon under the mean, the mean under the candles.
    """
    shaded = longest_gaps(view.gaps, MAX_SHADED_GAPS)
    note = (
        f" Shaded bands are the {len(shaded)} longest of {len(view.gaps)} gaps."
        if len(shaded) < len(view.gaps)
        else " Shaded bands are hours with no stored row."
        if shaded
        else ""
    )
    _title(
        f"Price Action — {symbol} candles with Bollinger envelope",
        f"{view.window}-bar mean ± {num_std:g} std. Candles are resampled for readability "
        f"on long windows.{note}",
    )
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
    # Under the candles, so a gap reads as missing ground rather than as a mark
    # on the price. Nothing is interpolated across it — the line simply joins.
    for gap in shaded:
        figure.add_vrect(
            x0=gap.start,
            x1=gap.end + timedelta(hours=1),
            fillcolor="#ffab00",
            opacity=0.14,
            line_width=0,
            layer="below",
        )

    # The cached estimate, drawn as a dashed bridge anchored on the real bars
    # either side. Dashed and separately named because it is not an
    # observation and must never read as one.
    # The estimator's own uncertainty, under the dashed mean so the mean
    # stays readable. Only the model-based forms have one; a straight line
    # has no band, and drawing none is the honest rendering of that.
    ribbon = view.fill_band()
    if not ribbon.empty:
        figure.add_trace(
            go.Scatter(
                x=list(ribbon.index) + list(ribbon.index[::-1]),
                y=list(ribbon["upper"]) + list(ribbon["lower"][::-1]),
                fill="toself",
                fillcolor="rgba(255,171,0,0.18)",
                line={"width": 0},
                hoverinfo="skip",
                name="estimate, 1 s.d.",
            )
        )
    overlay = view.fill_overlay
    if not overlay.empty:
        figure.add_trace(
            go.Scatter(
                x=overlay.index,
                y=overlay.to_numpy(),
                mode="lines",
                connectgaps=False,
                line={"color": "#ffab00", "width": 2, "dash": "dash"},
                name=f"estimated ({view.fill_method})",
            )
        )
    figure.update_layout(xaxis_rangeslider_visible=False)
    _plot(figure, height=420)

    if view.gaps:
        if view.filled_hours:
            causal = fill_rule(METHOD_POLICY[str(view.fill_method)]).causal
            band_note = (
                " The shaded ribbon is ±1 standard deviation: it widens with the gap, which is "
                "the model admitting it has stopped knowing."
                if view.has_band
                else " This estimator carries no error bar — a one-hour fill and a twenty-hour "
                "guess are drawn identically."
            )
            consumable = (
                "It uses only data from before each gap, so it is the one form a strategy could consume."
                if causal
                else "It reads the bar *after* the gap, so it is display only: nothing that trades sees it."
            )
            st.caption(
                f"{view.filled_hours} of {view.missing_hours} missing hour(s) are drawn from the "
                f"**{view.fill_method}** estimate (dashed). {consumable}{band_note} The Bollinger "
                "bands and every statistic on this page are computed from observations alone."
            )
        else:
            st.caption(
                f"{view.missing_hours} missing hour(s) are shown as gaps only — the "
                f"**{view.fill_method}** cache has nothing for this window. Run `make fill` "
                "or use the Quality dashboard to compute it."
            )


def _token_diagnostics(view: Any, candles: pd.DataFrame) -> None:
    """Volume, then the four per-token statistics, two by two."""
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

    left, right = st.columns(2)
    with left:
        _title(
            "Band Width — the squeeze",
            "Band span as a fraction of the mean. Compression is the setup; it says "
            "a move is coming, never which way.",
        )
        squeeze = go.Figure(
            go.Scatter(
                x=view.bands.index,
                y=view.bands["bandwidth"],
                mode="lines",
                line={"color": "#2962ff", "width": 1},
                name="bandwidth",
            )
        )
        squeeze.update_layout(yaxis_tickformat=".1%")
        _plot(squeeze, height=220)
    with right:
        _title(
            "%B — where price sits in the band",
            "0 is the lower band, 1 the upper. Outside that range the price has left the envelope.",
        )
        percent_b = go.Figure(
            go.Scatter(
                x=view.bands.index,
                y=view.bands["percent_b"],
                mode="lines",
                line={"color": "#607d8b", "width": 1},
                name="%B",
            )
        )
        for level, colour in ((1.0, DOWN), (0.0, UP)):
            percent_b.add_hline(y=level, line_color=colour, line_dash="dot", line_width=1)
        _plot(percent_b, height=220)


def _token_page(db: DatabaseManager, settings: Settings, ctx: dict[str, Any]) -> None:
    left, mid, right = st.columns(3)
    symbol = _pick(left, "Token", ctx["symbols"], "token_symbol", default_index(ctx["symbols"]))
    window = int(mid.number_input("Rolling window (bars)", 2, 2000, settings.rolling_window, 1))
    num_std = float(right.number_input("Band width (std)", 0.5, 5.0, 2.0, 0.25))

    # Whichever estimator the Quality dashboard has selected. They are
    # alternatives, so exactly one is drawn.
    method = _prefs().get("fill_method", settings.fill_method)
    view = build_token_view(
        db,
        symbol,
        ctx["days"],
        window,
        num_std,
        settings.analytics_interval_hours,
        fill_method=str(method),
    )
    latest = view.latest

    cols = st.columns(6)
    cols[0].metric("Last", f"{latest['price']:,.4f}", delta=_pct(latest["change_24h"]) + " 24h")
    cols[1].metric("Realised vol (ann.)", _pct(latest.get("rolling_volatility", float("nan"))))
    cols[2].metric(
        "VaR 95% 1d", _pct(latest["var_1d"]), help="Historical. The loss exceeded one day in twenty."
    )
    cols[3].metric("Max drawdown", _pct(latest["max_drawdown"]))
    cols[4].metric("Bars", f"{view.bars:,}")
    longest = view.longest_gap
    cols[5].metric(
        "Coverage",
        _pct(view.coverage),
        delta=f"{len(view.gaps)} gaps · longest {longest.hours}h" if longest else "no gaps",
        delta_color="off",
        help="Observed hours over the plotted range. An hour with no swaps emits no row, "
        "so a thin token is legitimately sparse — see the Quality dashboard before calling it a fault.",
    )

    # Resampled once and shared: the chart and the volume pulse must show
    # the same buckets, and resampling twice invites them to drift.
    candles = candle_frame(view.ohlc)
    _token_price_chart(view, candles, symbol, num_std)
    _token_diagnostics(view, candles)


# --- QUALITY -------------------------------------------------------------------


def _gap_map(view: Any) -> None:
    """One lane per token: its stored range, with the missing hours cut out of it."""
    symbols = view.lane_symbols
    if not symbols:
        st.info("No stored history yet.")
        return

    base_x, base_y = range_lane_segments(view.tokens, symbols)
    gap_x, gap_y = gap_lane_segments(view.gaps, symbols)
    figure = go.Figure(
        go.Scatter(
            x=base_x,
            y=base_y,
            mode="lines",
            line={"color": "#2e7d32", "width": 11},
            name="stored",
            hoverinfo="skip",
        )
    )
    if gap_x:
        figure.add_trace(
            go.Scatter(
                x=gap_x,
                y=gap_y,
                mode="lines",
                line={"color": "#ffab00", "width": 11},
                name="missing",
                hoverinfo="skip",
            )
        )
    # Thinnest coverage at the top, which is where the eye lands first.
    figure.update_layout(yaxis={"categoryorder": "array", "categoryarray": symbols[::-1]})
    _plot(figure, height=max(200, 34 * len(symbols)))


def _run_fill(db: DatabaseManager, view: Any, methods: list[str]) -> None:
    """Recompute one or more estimators, reporting each. The page's only write."""
    symbols = list(view.tokens["symbol"])
    try:
        for method in methods:
            with st.spinner(f"Estimating with {method}…"):
                done = run_fill_refresh_now(db, symbols, method=method)
            st.success(
                f"`{done.method}`: cached {done.rows_written} estimated hour(s) across "
                f"{len(done.runs)} token(s)."
            )
    except ViewError as exc:
        st.error(str(exc))
    else:
        st.rerun()


def _estimator_panel(db: DatabaseManager, settings: Settings, ctx: dict[str, Any], view: Any) -> None:
    """Pick a gap estimator, recompute it, and see how the three compare."""
    _title(
        "Gap Estimators — three ways to guess, one selected",
        "Stored in `token_hours_fill` so a dashboard reads an estimate instead of recomputing "
        "it on every widget change. They are **alternatives for the same missing hour, not "
        "layers**: all three can be cached so you can compare them, and exactly one is selected "
        "for display. Nothing here touches `token_hours_data`, so the coverage above is "
        "unaffected by anything on this panel.",
    )

    method = _pick(
        st,
        "Estimator",
        list(FILL_METHODS),
        "fill_method",
        FILL_METHODS.index(settings.fill_method) if settings.fill_method in FILL_METHODS else 0,
        help="Which estimate the Token dashboard draws, and which one the button below "
        "recomputes. Only one is ever active.",
    )
    rule = fill_rule(METHOD_POLICY[method])
    (st.success if rule.causal else st.warning)(
        f"**{method}** — {METHOD_SUMMARY[method]}. "
        + (
            "Causal: it uses only data from before the gap, so this is the one form a "
            "strategy could safely consume."
            if rule.causal
            else "Display only: it reads the bar *after* the gap, so a backtest fed this "
            "would be trading on information it could not have had."
        )
    )

    cache_cols = st.columns([1, 1, 1, 2])
    cache_cols[0].metric(f"Hours cached ({method})", f"{view.cached_hours(method):,}")
    cache_cols[1].metric(
        "Fillable hours",
        f"{view.fillable_hours:,}",
        help=f"Missing hours whose run is {DEFAULT_MAX_FILL}h or shorter. A longer gap is left "
        "alone: beyond the cap any line is fiction.",
    )
    cache_cols[2].metric(
        "Estimators cached",
        f"{len(view.cached_methods)} / {len(FILL_METHODS)}",
        help="Cache all three to compare them on the same gaps.",
    )
    stale = view.cache_is_stale(method)
    with cache_cols[3]:
        compute, compute_all = st.columns(2)
        if compute.button(
            f"↻ Compute {method}",
            type="primary" if stale else "secondary",
            width="stretch",
            help="Writes to token_hours_fill only. No market data is touched, no network used.",
        ):
            _run_fill(db, view, [method])
        if compute_all.button(
            "↻ Compute all three",
            width="stretch",
            help="So the same gaps hold every estimate and the comparison below is like for like.",
        ):
            _run_fill(db, view, list(FILL_METHODS))
        if stale:
            st.caption(f"`{method}` does not match the gaps above — recompute to refresh it.")

    st.dataframe(method_catalogue(), width="stretch", hide_index=True)

    if not view.fill_cache.empty:
        _title("What is cached, per token and estimator")
        st.dataframe(
            view.fill_cache.style.format({"computed_at": "{:%Y-%m-%d %H:%M}"}, na_rep="—"),
            width="stretch",
            hide_index=True,
        )

    _title(
        "Which Estimator Is Best — measured, not argued",
        "Real hours are held out, each estimator re-guesses them, and the error is what you "
        "see. Lower is better — but a low error is not a licence to trade on it: `bridge` "
        "wins *because* it reads the bar after the gap. Among estimates that could have been "
        "made at the time, `causal` is the only entry.",
    )
    if st.button("Score the estimators", help="Pure computation over stored prices; writes nothing."):
        with st.spinner("Holding out hours and re-estimating…"):
            st.session_state["fill_scores"] = build_method_scores(
                db, list(view.tokens["symbol"]), days=ctx["days"]
            )
    scores = st.session_state.get("fill_scores")
    if scores is None:
        st.caption("Press the button to score them against hours whose real price is known.")
    elif scores.empty:
        st.info("Not enough stored history to hold any hours out yet.")
    else:
        st.dataframe(
            scores.style.format({"mae_bps": "{:,.1f}", "rmse_bps": "{:,.1f}", "worst_pct": "{:.2f}%"}),
            width="stretch",
            hide_index=True,
        )
        best = scores.iloc[0]
        causal_only = scores[scores["consumable_by"] == "a strategy"]
        note = f"Best overall: **{best['method']}** at {best['mae_bps']:,.1f} bps."
        if not causal_only.empty:
            safe = causal_only.iloc[0]
            note += (
                f" Best that a strategy could consume: **{safe['method']}** at "
                f"{safe['mae_bps']:,.1f} bps — the price of not looking ahead."
            )
        st.caption(note)


def _gap_diagnostics(view: Any) -> None:
    """Where the holes are, how long they run, and what was collected."""
    _title(
        "Gap Map — where the hours are missing",
        "One lane per token, thinnest coverage first. Green is stored, amber is a run of hours "
        "with no row. A short lane is a short history, not a gap.",
    )
    _gap_map(view)

    left, right = st.columns(2)
    with left:
        _title(
            "Gap Lengths — one outage or constant drizzle?",
            "Many one-hour holes is a quiet market (invariant 19). A single long bar is an "
            "ingestion failure worth chasing.",
        )
        if view.gaps.empty:
            st.info("No gaps in the stored ranges.")
        else:
            _plot(
                go.Figure(
                    go.Histogram(x=view.gaps["hours"].to_numpy(), nbinsx=40, marker_color="#ffab00")
                ).update_layout(xaxis_title="gap length (hours)", yaxis_title="gaps"),
                height=260,
            )
    with right:
        _title(
            "Field Completeness — what was actually collected",
            "A blank field is NULL, meaning *not collected* — never a real zero (invariant 11). "
            "Rows written before the enrichment migration genuinely have no value.",
        )
        if view.completeness.empty:
            st.info("No completeness counts for this window.")
        else:
            bars = go.Figure(
                go.Bar(
                    x=view.completeness["fraction"],
                    y=view.completeness["field"],
                    orientation="h",
                    marker_color="#2962ff",
                )
            )
            bars.update_layout(xaxis_tickformat=".0%", xaxis_range=[0, 1])
            _plot(bars, height=260)


def _quality_tables(view: Any) -> None:
    """Per token, then the longest gaps, then anything impossible."""
    _title("By Token — coverage, gaps and freshness", "Thinnest coverage first.")
    st.dataframe(
        view.tokens.style.format(
            {
                "coverage": "{:.1%}",
                "stale_hours": "{:,.1f}",
                "first": "{:%Y-%m-%d %H:%M}",
                "last": "{:%Y-%m-%d %H:%M}",
            },
            na_rep="—",
        ).map(
            lambda v: f"color: {'#ffab00' if v < view.min_expected_coverage else UP}",
            subset=["coverage"],
        ),
        width="stretch",
        hide_index=True,
    )

    _title("Longest Gaps — the runs worth explaining", "Longest first.")
    if view.gaps.empty:
        st.info("No gaps in the stored ranges.")
    else:
        st.dataframe(
            view.gaps.head(30).style.format({"start": "{:%Y-%m-%d %H:%M}", "end": "{:%Y-%m-%d %H:%M}"}),
            width="stretch",
            hide_index=True,
        )

    if not view.violations.empty:
        _title("Impossible Rows — high < low, or OHLC outside the bar")
        st.dataframe(view.violations, width="stretch", hide_index=True)


def _quality_page(db: DatabaseManager, settings: Settings, ctx: dict[str, Any]) -> None:
    st.subheader("Quality — can this history be trusted, and where is it thin?")
    view = build_quality_view(db, settings, ctx["days"])
    head = view.headline

    cols = st.columns(6)
    cols[0].metric("Tokens with history", f"{head['tokens']:,}")
    cols[1].metric("Hourly rows stored", f"{head['rows']:,}")
    cols[2].metric(
        f"Below {view.min_expected_coverage:.0%} coverage",
        f"{head['under_covered']}",
        help="Judged against MIN_EXPECTED_COVERAGE, never by the presence of a gap.",
    )
    cols[3].metric(
        "Impossible rows",
        f"{head['violations']}",
        delta=f"-{head['violations']}" if head["violations"] else None,
        delta_color="inverse",
        help="high < low, or OHLC outside the bar. Impossible whatever the market did — "
        "the only unambiguous fault on this page.",
    )
    cols[4].metric("Gaps", f"{head['gaps']:,}")
    cols[5].metric(
        "Longest gap",
        f"{head['worst_gap_hours']}h" if head["worst_gap_hours"] else "—",
        delta=head["worst_gap_symbol"] or None,
        delta_color="off",
    )

    window = "all stored history" if ctx["days"] is None else f"the last {ctx['days']} days"
    st.caption(
        f"Generated {view.generated_at:%Y-%m-%d %H:%M} UTC over {window}. "
        "Read-only: nothing on this page fills a gap or writes a row."
    )

    if view.missing_tokens:
        st.error(
            f"**No rows at all for {', '.join(view.missing_tokens)}.** "
            "These are configured in `config/tokens.yaml` but have never ingested — "
            "check `make runs` and the worker log, not the market."
        )
    if head["violations"]:
        st.warning(
            f"{head['violations']} impossible row(s) stored. Unlike a gap, this is always a bug: "
            "inspect them below and check the transform."
        )
    if head["ok"]:
        st.success(
            "No impossible rows, every configured token present, and every token above the "
            "coverage threshold."
        )

    _estimator_panel(db, settings, ctx, view)
    _gap_diagnostics(view)
    _quality_tables(view)


# --- RISK --------------------------------------------------------------------


def _risk_controls(settings: Settings, ctx: dict[str, Any]) -> tuple[str, str | None, RiskConfig, str]:
    """Token, benchmark, confidence, horizon and estimator."""
    a, b, c, d, e = st.columns(5)
    symbol = _pick(a, "Token", ctx["symbols"], "risk_symbol", default_index(ctx["symbols"]))
    default_benchmark = settings.risk_benchmark if settings.risk_benchmark in ctx["symbols"] else None
    others = ["(none)"] + [s for s in ctx["symbols"] if s != symbol]
    benchmark_pick = _pick(
        b,
        "Benchmark",
        others,
        "risk_benchmark",
        others.index(default_benchmark) if default_benchmark in others else 0,
    )
    confidence = float(c.slider("Confidence", 0.90, 0.99, settings.risk_confidence, 0.01))
    horizon_label = _pick(d, "Horizon", list(HORIZON_CHOICES), "risk_horizon", 2)
    # The picker speaks strings; the config speaks the literal union it came from.
    method = cast(Method, _pick(e, "Estimator", list(METHODS), "risk_method", 0))

    config = RiskConfig(
        confidence=confidence,
        horizon_bars=HORIZON_CHOICES[horizon_label],
        method=method,
        interval_hours=settings.analytics_interval_hours,
        risk_free_rate=ctx["risk_free"],
    )
    benchmark = None if benchmark_pick == "(none)" else benchmark_pick
    return symbol, benchmark, config, horizon_label


def _risk_metrics(view: Any, config: RiskConfig, horizon_label: str) -> None:
    """VaR and CVaR beside the plain risk statistics."""
    report = view.report
    confidence = config.confidence
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


def _risk_charts(view: Any, config: RiskConfig) -> None:
    """The loss profile, estimator disagreement, drawdown and the market split."""
    report = view.report
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


def _risk_page(db: DatabaseManager, settings: Settings, ctx: dict[str, Any]) -> None:
    symbol, benchmark, config, horizon_label = _risk_controls(settings, ctx)
    view = build_risk_view(db, symbol, benchmark, ctx["days"], config)

    _risk_metrics(view, config, horizon_label)
    _risk_charts(view, config)
    st.caption(
        f"Risk-free rate {config.risk_free_rate:.2%}/yr (nominal, from the sidebar) "
        "feeds the Sharpe and Sortino above."
    )


# --- COMPARE -----------------------------------------------------------------


def _compare_controls(
    db: DatabaseManager, settings: Settings, ctx: dict[str, Any]
) -> tuple[str, str, int, Any]:
    """The two tokens, the rolling window, and the comparison over aligned bars."""
    first, second = DEFAULT_PAIR
    a, b, c = st.columns(3)
    symbol_a = _pick(a, "Token A", ctx["symbols"], "pair_a", default_index(ctx["symbols"], first))
    others = [s for s in ctx["symbols"] if s != symbol_a] or ctx["symbols"]
    symbol_b = _pick(b, "Token B", others, "pair_b", default_index(others, second, exclude=symbol_a))
    window = int(c.number_input("Rolling window (bars)", 2, 2000, settings.rolling_window * 7, 1))

    view = build_pair_view(
        db, symbol_a, symbol_b, ctx["days"], window, settings.analytics_interval_hours, ctx["risk_free"]
    )
    return symbol_a, symbol_b, window, view


def _compare_metrics(view: Any, symbol_a: str, symbol_b: str) -> None:
    """Correlation two ways, beta, tracking error and the relative return."""
    result = view.comparison
    cols = st.columns(6)
    cols[0].metric(
        "Correlation (Pearson)",
        f"{result.correlation:.3f}",
        help="Of hourly log returns, over the aligned bars.",
    )
    cols[1].metric(
        "Correlation (Spearman)",
        f"{result.spearman:.3f}",
        help="Rank correlation: unmoved by outliers, so a large gap between this and "
        "Pearson means a few extreme hours are driving the linear number.",
    )
    cols[2].metric(f"Beta ({symbol_a} on {symbol_b})", f"{result.beta:.3f}")
    cols[3].metric("Tracking error (ann.)", _pct(result.tracking_error))
    cols[4].metric(f"Relative return {symbol_a} - {symbol_b}", _pct(result.relative_return))
    cols[5].metric("Aligned bars", f"{result.bars:,}")


def _compare_legs(view: Any) -> None:
    """Each token on its own, over the same aligned bars."""
    _title(
        "Each Leg on Its Own — over the same aligned bars",
        "Which token carried the return, and which carried the risk. Computed over the "
        "hours both traded, so these are comparable with each other — not with a "
        "full-history figure elsewhere in the app.",
    )
    st.dataframe(
        view.legs_frame.style.format(
            {"total_return": "{:+.2%}", "volatility": "{:.1%}", "sharpe": "{:.3f}"},
            na_rep="—",
        ).map(
            lambda v: f"color: {UP}" if isinstance(v, float) and v > 0 else f"color: {DOWN}",
            subset=["total_return"],
        ),
        width="stretch",
        hide_index=True,
    )


def _compare_charts(view: Any, symbol_a: str, symbol_b: str, window: int) -> None:
    """Relative value, the correlation regime, and the beta cloud."""
    result = view.comparison
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


def _compare_page(db: DatabaseManager, settings: Settings, ctx: dict[str, Any]) -> None:
    symbol_a, symbol_b, window, view = _compare_controls(db, settings, ctx)

    _compare_metrics(view, symbol_a, symbol_b)
    _compare_legs(view)
    _title(
        f"Relative Value — {symbol_a} priced in {symbol_b}",
        "A rising line means A outruns B. Hours only one token traded are excluded, not filled.",
    )
    st.line_chart(view.comparison.frame[["ratio"]], height=280)
    _compare_charts(view, symbol_a, symbol_b, window)


# --- PORTFOLIO ---------------------------------------------------------------


def _portfolio_controls(
    settings: Settings, ctx: dict[str, Any]
) -> tuple[list[str], PortfolioConfig, RiskConfig, int]:
    """The pickers and sliders. Returns what building the view needs."""
    chosen = _pick_many(
        st,
        "Tokens",
        ctx["symbols"],
        "portfolio_symbols",
        default_portfolio_symbols(ctx["symbols"]),
        help="Majors first, stablecoins left out by default: "
        "a USD-pegged token swallows any minimum-risk objective.",
    )
    left, right, third, fourth = st.columns(4)
    objective = cast(Objective, _pick(left, "Objective", list(OBJECTIVES), "portfolio_objective", 0))
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
    return chosen, config, risk, frontier_points


def _portfolio_metrics(view: Any, settings: Settings) -> None:
    """The metric strip, plus the two warnings an unusual solve deserves."""
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


def _portfolio_allocation(view: Any) -> None:
    """Where the money sits, and who carries the risk."""
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


def _portfolio_frontier(view: Any) -> None:
    """The efficient frontier, with the chosen allocation marked."""
    if not view.frontier:
        return
    result = view.result
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


def _portfolio_page(db: DatabaseManager, settings: Settings, ctx: dict[str, Any]) -> None:
    chosen, config, risk, frontier_points = _portfolio_controls(settings, ctx)
    view = build_portfolio_view(db, chosen, ctx["days"], config, frontier_points, risk)

    _portfolio_metrics(view, settings)
    _portfolio_allocation(view)
    _portfolio_frontier(view)
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


def _explanation_panel(db: DatabaseManager, settings: Settings, view: Any, symbol: str, name: str) -> None:
    """The `signals --explain` surface: read what is stored for free, run it on request."""
    _title(
        "Explain — what else was true at the latest bar",
        "The numbers are computed; the prose may cite only documents published before the bar.",
    )
    can_search = settings.tavily_api_key is not None
    left, right = st.columns([3, 1])
    search = left.checkbox(
        "Search for news first (at most one Tavily credit per token per UTC day)",
        value=False,
        disabled=not can_search,
        key=f"explain_search_{symbol}_{name}",
        help="Off: only documents already stored are cited. No TAVILY_API_KEY: the box is disabled.",
    )
    shown = None
    if right.button("▶ Explain latest signal", key=f"explain_run_{symbol}_{name}", type="primary"):
        with st.spinner("Explaining…"):
            try:
                shown = run_explain_now(settings, db, view.strategy, symbol, search=search)
            except ViewError as exc:
                st.error(str(exc))
    if shown is None:
        shown = load_stored_explanation(db, name, symbol)
    if shown is None:
        st.info("Nothing explained for this token and strategy yet. Press “Explain latest signal”.")
        return

    head = shown.headline
    cols = st.columns(5)
    cols[0].metric("Bar", f"{head['bar']:%Y-%m-%d %H:%M}")
    cols[1].metric("Signal", str(head["position"]).upper())
    cols[2].metric("Score", f"{head['score']:+.2f}" if head["score"] is not None else "—")
    cols[3].metric("24h move", _pct(head["move_24h"]) if head["move_24h"] is not None else "—")
    ref = head["reference_symbol"]
    cols[4].metric(
        f"{ref} 24h move" if ref else "Reference",
        _pct(head["reference_move_24h"]) if head["reference_move_24h"] is not None else "—",
        help="The market-wide reference (KNOWLEDGE_REFERENCE_SYMBOL): is the move idiosyncratic?",
    )
    st.markdown(shown.explanation.rationale or "_No rationale survived validation._")
    if shown.validator_note:
        st.warning(shown.validator_note)
    st.caption(shown.status_line + f" · narrator: {shown.explanation.model or 'template'}")
    if head["citations"]:
        st.dataframe(
            shown.citations_frame,
            width="stretch",
            hide_index=True,
            column_config={"url": st.column_config.LinkColumn("url")},
        )
    else:
        st.caption(
            "No documents published before the bar were retrieved; the signal stands on the numbers alone."
        )


def _backtest_explainer(config: BacktestConfig) -> None:
    """How the number on screen was produced. Open by default the first time.

    The page used to show six metrics and no account of where they came
    from, which makes a backtest unreadable: two people looking at the same
    equity curve could disagree about whether it included costs.
    """
    with st.expander("How this backtest works — read once", expanded=False):
        st.markdown(
            f"""
**A position is decided on one bar and earns the next one's return.**
The strategy sees the close of bar *t* and cannot trade at a price it has
not seen, so the position held during bar *t* is the one decided at
*t-1* — `position = signal.shift({config.execution_lag})`. Removing that
shift changes the result *silently*, and the direction depends on the
rule: a trend-following signal is flattered, because it goes long on the
very bar it then earns. A mean-reversion signal is punished, because it
shorts the bar that just rose and then collects that rise. Neither number
is real. The **Look-ahead check** below draws the gap for whichever
strategy is selected.

**Costs are charged on position changes, sized by the move.**
Each bar pays `abs(change in position) * {config.fee_bps + config.slippage_bps:.0f} bps`
({config.fee_bps:.0f} fee + {config.slippage_bps:.0f} slippage, both set in
the sidebar), so flipping long to short costs twice as much as opening from
flat. On hourly bars an active strategy routinely pays 15-20% of capital
over 90 days.

**Read `excess` before `total return`.** A strategy that made 18% while the
token made 30% lost money in the only sense that matters. Buy-and-hold is
the benchmark, not zero.

**A losing backtest is usually the honest answer.** Costs and the execution
lag are real. If `costs paid` exceeds `excess`, there is no edge here —
that is a finding, not a parameter to tune away.

Starting capital is **{config.initial_capital:,.0f}**; every currency figure
on this page is against that.
"""
        )


def _strategy_controls(ctx: dict[str, Any]) -> tuple[str, str, BacktestConfig]:
    """Token, rule, and the two execution assumptions that belong beside the result."""
    st.subheader("Strategy — does this rule beat holding the token, net of costs?")

    left, right = st.columns(2)
    symbol = _pick(left, "Token", ctx["symbols"], "strategy_symbol", default_index(ctx["symbols"]))
    name = _pick(right, "Strategy", list(available()), "strategy_name", 1)

    # Execution assumptions live beside the result they produce. The sidebar
    # still owns fees and slippage; these two are backtest-only.
    exec_cols = st.columns(2)
    capital = float(
        exec_cols[0].number_input(
            "Starting capital",
            min_value=100.0,
            max_value=10_000_000.0,
            value=float(ctx["backtest"].initial_capital),
            step=1_000.0,
            help="Every currency figure on this page is against this.",
        )
    )
    lag = int(
        exec_cols[1].number_input(
            "Execution lag (bars)",
            min_value=0,
            max_value=24,
            value=1,
            step=1,
            help="Bars between deciding and trading. 1 is the honest default. "
            "0 means trading at a close the strategy has already seen — look-ahead.",
        )
    )
    config = replace(ctx["backtest"], initial_capital=capital, execution_lag=lag)
    if lag == 0:
        st.error(
            "**Execution lag 0 — these numbers are not real.** The strategy is trading at a "
            "price it had already seen when it decided, so every figure below is contaminated "
            "by look-ahead (invariant 15). Note it does not always flatter: a mean-reversion "
            "rule is *punished* by it. Useful to look at once; never to act on."
        )

    st.caption(
        f"Costs come from the sidebar: **{config.fee_bps:.0f} bps fee + "
        f"{config.slippage_bps:.0f} bps slippage** per side, charged on every position change. "
        f"Risk-free rate **{config.risk_free_rate:.2%}/yr** feeds the Sharpe and Sortino."
    )
    _backtest_explainer(config)
    return symbol, name, config


def _strategy_metrics(view: Any) -> None:
    """The six numbers, and the one note a costed loss deserves."""
    report = view.report
    cols = st.columns(6)
    cols[0].metric(
        "Excess vs buy & hold",
        _pct(report.excess_return),
        help="The number to read first. Positive means the rule beat simply holding.",
    )
    cols[1].metric("Total return", _pct(report.total_return))
    cols[2].metric("Buy & hold", _pct(report.buy_and_hold_return))
    cols[3].metric("Sharpe", f"{report.sharpe:.2f}")
    cols[4].metric("Max drawdown", _pct(report.max_drawdown))
    cols[5].metric(
        "Costs paid",
        _pct(report.total_costs),
        help="Fees + slippage as a fraction of capital. If this beats the excess, there is no edge.",
    )

    if report.total_costs > abs(report.excess_return) and report.excess_return <= 0:
        st.info(
            f"Costs ({report.total_costs:.1%}) exceed anything this rule earned over holding. "
            "At these fees the signal would have to be considerably better to survive; that is "
            "a finding about hourly trading, not a reason to tune the parameters."
        )


def _strategy_charts(
    db: DatabaseManager,
    view: Any,
    ctx: dict[str, Any],
    symbol: str,
    name: str,
    config: BacktestConfig,
    strategy_config: Any,
) -> None:
    """Equity, where the edge went, the look-ahead check, exposure and drawdown.

    ``strategy_config`` is passed in, never rebuilt: `_strategy_config` draws
    widgets, and drawing them twice in one run is a Streamlit duplicate-id error.
    """
    report = view.report
    _title(
        "Equity — strategy vs buy & hold, net of costs",
        "Both start at the same capital. If the strategy line is below the other, holding won.",
    )
    st.line_chart(view.equity_frame, height=320)

    left, right = st.columns(2)
    with left:
        _title(
            "Where the Edge Went — gross against net",
            "The gap between the two lines is the cumulative cost of trading.",
        )
        st.line_chart(view.cost_frame, height=240)
    with right:
        _title(
            "Look-ahead Check — what removing the lag would claim",
            "The same strategy trading at a close it had already seen. A trend rule is "
            "flattered by this and a mean-reversion rule is punished — the lesson is the size "
            "of the gap, not its sign. Invariant 15 exists so neither can happen by accident.",
        )
        with st.spinner("Re-running without the lag…"):
            st.line_chart(
                build_lag_demonstration(db, symbol, name, ctx["days"], strategy_config, config),
                height=240,
            )

    left, right = st.columns(2)
    with left:
        _title("Exposure Timeline — long, flat, short")
        st.area_chart(report.frame[["position"]], height=200)
    with right:
        _title("Underwater — strategy drawdown")
        st.area_chart(report.frame[["drawdown"]], height=200, color=DOWN)


def _strategy_tables(db: DatabaseManager, view: Any, ctx: dict[str, Any], symbol: str, config: Any) -> None:
    """The full report beside every other rule on the same token."""
    left, right = st.columns([2, 3])
    with left:
        _title("Every Number — the full report", "The same thirteen `candlegraph backtest` prints.")
        st.dataframe(
            view.metrics_frame[["metric", "display"]].rename(columns={"display": "value"}),
            width="stretch",
            hide_index=True,
        )
    with right:
        _title(
            f"Strategy Comparison — every rule on {symbol}, at its defaults",
            "What `backtest --strategy all` prints. Ranked by excess over buy & hold; these "
            "are default settings, so treat it as where to start rather than a verdict.",
        )
        try:
            with st.spinner("Backtesting every strategy…"):
                table = build_strategy_comparison(db, symbol, ctx["days"], config)
        except ViewError as exc:
            st.info(str(exc))
        else:
            st.dataframe(
                table.style.format(
                    {
                        "total_return": "{:+.2%}",
                        "buy_and_hold": "{:+.2%}",
                        "excess": "{:+.2%}",
                        "sharpe": "{:.2f}",
                        "max_drawdown": "{:.1%}",
                        "exposure": "{:.0%}",
                    }
                ).map(
                    lambda v: f"color: {UP}" if isinstance(v, float) and v > 0 else f"color: {DOWN}",
                    subset=["excess"],
                ),
                width="stretch",
                hide_index=True,
            )


def _strategy_ledger(view: Any) -> None:
    """The last 30 position changes, and the newest signal."""
    report = view.report
    _title(
        f"Trade Ledger — last 30 position changes of {report.trades:,} "
        f"(latest signal: {view.strategy.latest.name})"
    )
    st.dataframe(view.trades_frame.tail(30).style.format({"price": "{:,.4f}"}), width="stretch")


def _strategy_page(db: DatabaseManager, settings: Settings, ctx: dict[str, Any]) -> None:
    st.subheader("Strategy — does this rule beat holding the token, net of costs?")
    symbol, name, config = _strategy_controls(ctx)
    strategy_config = _strategy_config(name)
    if name == "arima":
        st.caption("ARIMA refits on every cadence bar, so a long window takes a moment.")

    with st.spinner("Running…"):
        view = build_strategy_view(db, symbol, name, ctx["days"], strategy_config, config)

    _strategy_metrics(view)
    _strategy_charts(db, view, ctx, symbol, name, config, strategy_config)
    _strategy_tables(db, view, ctx, symbol, config)
    _strategy_ledger(view)
    _explanation_panel(db, settings, view, symbol, name)


# --- FORECAST ----------------------------------------------------------------


def _forecast_controls(ctx: dict[str, Any]) -> tuple[str, int, float, ArimaConfig]:
    """Token, order, horizon and confidence."""
    a, b, c, d = st.columns(4)
    symbol = _pick(a, "Token", ctx["symbols"], "forecast_symbol", default_index(ctx["symbols"]))
    p = int(b.number_input("p", 0, 5, 1, 1, key="f_p"))
    q = int(c.number_input("q", 0, 5, 1, 1, key="f_q"))
    steps = int(d.number_input("Steps ahead", 1, 168, 24, 1))

    left, right = st.columns(2)
    auto = left.checkbox("Pick p and q by AIC", value=False)
    confidence = right.slider("Confidence", 0.50, 0.99, 0.95, 0.01)
    return (
        symbol,
        steps,
        confidence,
        ArimaConfig(order=(p, 1, q), auto_order=auto, forecast_steps=steps, confidence=confidence),
    )


def _forecast_metrics(forecast: Any, steps: int) -> None:
    """Last price, the projection, the order chosen, and how wide the interval is."""
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


def _forecast_chart(prices: Any, forecast: Any, steps: int, confidence: float) -> None:
    """Recent history, the projected path, and the interval at the horizon."""
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


def _forecast_page(db: DatabaseManager, ctx: dict[str, Any]) -> None:
    symbol, steps, confidence, config = _forecast_controls(ctx)
    with st.spinner("Fitting…"):
        prices, forecast = build_forecast(db, symbol, ctx["days"], config)

    if not forecast.ok:
        st.error(f"No forecast: {forecast.message}")
        return

    _forecast_metrics(forecast, steps)
    _forecast_chart(prices, forecast, steps, confidence)
    _posterior_panel(db, ctx, symbol, steps, confidence)


def _posterior_panel(
    db: DatabaseManager, ctx: dict[str, Any], symbol: str, steps: int, confidence: float
) -> None:
    """The same horizon, with the model's parameters treated as unknown."""
    _title(
        "Parameter Uncertainty — the interval above assumes it knows the model",
        "ARIMA fits parameters and then forecasts as if they were exact, so its interval "
        "covers innovation noise only. Sampling the parameters by MCMC covers both. The "
        "difference is largest on a short window and vanishes on a long one — that "
        "convergence is the method agreeing with the point estimate, not failing.",
    )
    if not st.button("▶ Sample the posterior", help="Pure computation; writes nothing."):
        st.caption("Press to re-forecast with the parameters treated as estimated.")
        return

    with st.spinner("Sampling…"):
        try:
            forecast = build_posterior_forecast(db, symbol, ctx["days"], steps=steps, confidence=confidence)
        except ViewError as exc:
            st.error(str(exc))
            return

    if not forecast.usable:
        st.error(
            f"**Refused — the chains did not mix.** {forecast.posterior.diagnosis} "
            "An unconverged sampler produces confident nonsense and does not announce "
            "itself, so nothing below should be read as a posterior."
        )
    else:
        st.success(forecast.posterior.diagnosis)

    cols = st.columns(4)
    cols[0].metric("Posterior median", f"{forecast.median:,.4f}")
    cols[1].metric(
        "Interval width",
        _pct(forecast.width),
        help="Interval span over the last price, with parameter uncertainty included.",
    )
    cols[2].metric(
        "Wider than the point estimate",
        _pct(forecast.widening),
        help="Near zero on a long window is correct: the parameters are pinned down.",
    )
    cols[3].metric(
        "Acceptance",
        _pct(forecast.posterior.acceptance),
        help="Proposals the sampler took. Very low or very high both mean poor mixing.",
    )

    left, right = st.columns(2)
    with left:
        _title("The Two Intervals", "Posterior against the one every other forecast reports.")
        st.dataframe(
            posterior_interval_frame(forecast).style.format(
                {"lower": "{:,.4f}", "upper": "{:,.4f}", "width": "{:.2%}"}
            ),
            width="stretch",
            hide_index=True,
        )
    with right:
        _title("Per Parameter", "R-hat near 1 and a large ESS are what make this readable.")
        st.dataframe(
            posterior_diagnostics_frame(forecast).style.format(
                {"mean": "{:,.6f}", "p5": "{:,.6f}", "p95": "{:,.6f}", "r_hat": "{:.4f}", "ess": "{:,.0f}"}
            ),
            width="stretch",
            hide_index=True,
        )
    st.caption(
        f"Random walk with drift, {forecast.posterior.draws.shape[0]} chains x "
        f"{forecast.posterior.draws.shape[1]} draws, seed {forecast.posterior.seed}."
    )


# --- SIMULATION ----------------------------------------------------------------


def _monte_carlo_panel(db: DatabaseManager, ctx: dict[str, Any], symbol: str, name: str) -> None:
    """Is this edge luck? A distribution instead of a sample of size one."""
    controls = st.columns(4)
    method = _pick(
        controls[0],
        "Path generator",
        list(GENERATOR_NAMES),
        "sim_method",
        0,
        help="Choosing one is choosing what the answer can mean; the table below says how.",
    )
    paths = int(controls[1].number_input("Paths", min_value=20, max_value=5000, value=300, step=50))
    seed = int(controls[2].number_input("Seed", min_value=0, max_value=10_000, value=0, step=1))
    controls[3].metric("Generator keeps", GENERATORS[method].preserves.split(",")[0])

    st.dataframe(generator_catalogue(), width="stretch", hide_index=True)

    if not st.button("▶ Run the simulation", type="primary", help="Pure computation; writes nothing."):
        st.caption("Press to backtest this strategy over synthetic markets.")
        return

    with st.spinner(f"Backtesting over {paths} synthetic markets…"):
        view = build_simulation_view(
            db, symbol, name, ctx["days"], paths, method, seed, ctx["backtest"], _strategy_config(name)
        )

    head = view.headline
    cols = st.columns(5)
    cols[0].metric("Actual excess", _pct(head["actual_excess"]))
    cols[1].metric("Median simulated", _pct(head["median_excess"]))
    cols[2].metric(
        "p-value",
        f"{head['p_value']:.3f}",
        help="Share of synthetic paths that did at least as well. Large means the real "
        "result is easy to reproduce by chance.",
    )
    cols[3].metric("Paths", f"{head['paths']:,}")
    cols[4].metric("From real bars", f"{head['source_bars']:,}", help=SAMPLE_CAVEAT)

    verdict = view.summary.verdict
    (st.info if head["p_value"] > 0.10 else st.success)(verdict)
    st.caption(
        f"Generator `{head['method']}`, seed {head['seed']} — the same seed gives the same "
        f"answer. {SAMPLE_CAVEAT}"
    )

    left, right = st.columns(2)
    with left:
        _title(
            "Where the Real Result Sits",
            "Excess return of each synthetic path. The line is what actually happened: if it "
            "is inside the crowd, the rule did nothing the market's own shape would not.",
        )
        histogram = go.Figure(go.Histogram(x=view.distribution.to_numpy(), nbinsx=40, marker_color="#607d8b"))
        histogram.add_vline(
            x=head["actual_excess"],
            line_color="#ffab00",
            line_width=2,
            annotation_text="actual",
            annotation_position="top",
        )
        histogram.update_layout(xaxis_tickformat=".0%", xaxis_title="excess vs buy & hold")
        _plot(histogram, height=300)
    with right:
        _title(
            "Path Envelope — percentile bands across the simulated markets",
            "A thousand lines are noise; the envelope is the statement. All paths start at "
            "the same price, so the fan opens from a point.",
        )
        fan = go.Figure()
        for column, width in (("p5", 1), ("p25", 1), ("p50", 2), ("p75", 1), ("p95", 1)):
            fan.add_trace(
                go.Scatter(
                    x=view.fan.index,
                    y=view.fan[column],
                    mode="lines",
                    name=column,
                    line={"width": width, "color": "#2962ff" if column == "p50" else "#90a4ae"},
                )
            )
        _plot(fan, height=300)

    _title("Every Metric, Across Paths", "`actual` is the real backtest; the rest is the crowd.")
    st.dataframe(
        view.percentile_frame.style.format(
            dict.fromkeys(("actual", "p5", "p25", "p50", "p75", "p95"), "{:,.4g}")
        ),
        width="stretch",
        hide_index=True,
    )


def _execution_panel(db: DatabaseManager, ctx: dict[str, Any], symbol: str, name: str) -> None:
    """Does the vectorised backtester lie about execution?

    Written to be checkable *on screen*. The reconciliation proves the
    engine is right; the walkthrough shows what it does, so nobody has to
    read the source to match the implementation against the theory.
    """
    st.markdown(
        """
##### The lifecycle

Every bar runs through four events, in this order:

| | Event | What happens |
|---|---|---|
| 1 | **decide** | at bar *t*'s **close**, the strategy reads the bar and picks a target position |
| 2 | **delay** | the order waits `latency_bars` further bars before it reaches the market |
| 3 | **fill** | it closes `fill_ratio` of the remaining gap, capped by what the bar's volume can absorb |
| 4 | **hold** | the position reached at *t*'s close is held **during bar _t+1_**, earning that bar's return |

Step 4 is the one that matters, and it is why:

> `ExecutionModel(latency_bars=k)` is the same thing as `BacktestConfig(execution_lag=k + 1)`

The `+ 1` is **not** latency. A decision taken at a close cannot be acted on before the next
bar exists — true for an infinitely fast trader, so it is structural. `latency_bars` is
whatever delay comes on top of it. Above `execution_lag = 1` you are filling **stale
decisions**: the order carries the target it was placed with, and there is no cancel-replace.

Costs are charged on the bar the new position is *first held*, sized by how far it moved —
so a long-to-short flip is two units and costs twice an opening trade.
"""
    )

    _title(
        "Worked Example — the engine run on a flat price and a trivial signal",
        "Real output from the same `run_execution` the reconciliation uses, not an "
        "illustration that could drift from it. The price is held constant so every number "
        "here is about execution and none of it is about the market.",
    )
    walkthrough = st.columns([3, 1])
    extra_latency = int(
        walkthrough[1].number_input(
            "latency_bars", min_value=0, max_value=3, value=0, step=1, key="walk_latency"
        )
    )
    with walkthrough[0]:
        st.dataframe(
            execution_walkthrough(latency_bars=extra_latency).style.format(
                {
                    "decided (signal)": "{:+.0f}",
                    "held during bar": "{:+.0f}",
                    "position changed": "{:.2f}",
                    "cost paid": "{:.4f}",
                }
            ),
            width="stretch",
            hide_index=True,
        )
    st.caption(
        f"Read down the first two columns: a decision at bar *t* becomes a position at bar "
        f"*t+{1 + extra_latency}*. Raise `latency_bars` and the whole held column slides "
        "further right, which is the off-by-one made visible."
    )

    _title(
        "Reconciliation — is the fast backtester telling the truth?",
        "With no extra latency and full fills the two engines must agree **exactly**. That "
        "equality is what licenses using the fast one everywhere else in this app.",
    )
    if not st.button("▶ Reconcile and price the assumptions", type="primary"):
        st.caption("Press to run the event engine beside the vectorised one.")
        return

    with st.spinner("Running both engines…"):
        check, table = build_execution_view(
            db, symbol, name, ctx["days"], ctx["backtest"], _strategy_config(name)
        )

    if check.agrees:
        st.success(
            f"**The engines agree.** Position paths differ by "
            f"{check.max_position_difference:.1e} and equity by "
            f"{check.max_equity_difference:.1e}, against an `execution_lag` of "
            f"{check.execution_lag}. The fast path is telling the truth."
        )
    else:
        st.error(
            f"**The engines disagree**: positions by {check.max_position_difference:.2e}, "
            f"equity by {check.max_equity_difference:.2e}. One of the two is wrong, and "
            "every backtest on this screen depends on which."
        )

    _title(
        "What the Assumptions Are Worth",
        "Each row relaxes one of them. `vs_baseline` is the distance from the fast path's "
        "answer — the price of assuming instant, complete fills. `incomplete_fills` counts "
        "orders that needed more than one bar to go through.",
    )
    st.dataframe(
        table.style.format(
            {
                "total_return": "{:+.2%}",
                "vs_baseline": "{:+.2%}",
                "costs": "{:.2%}",
                "fill_ratio": "{:.2f}",
            }
        ),
        width="stretch",
        hide_index=True,
    )


def _simulation_page(db: DatabaseManager, settings: Settings, ctx: dict[str, Any]) -> None:
    st.subheader("Simulation — is the edge luck, and would it have filled?")
    st.caption(
        "A backtest is a sample of size one. These two answer different doubts: Monte Carlo "
        "asks whether the result survives a market reshuffled from the same moves; the "
        "execution model asks whether the fast backtester's assumptions are costing you "
        "anything. Both are read-only."
    )

    left, right = st.columns(2)
    symbol = _pick(left, "Token", ctx["symbols"], "sim_symbol", default_index(ctx["symbols"]))
    name = _pick(right, "Strategy", list(available()), "sim_strategy", 1)
    if name == "arima":
        st.warning(
            "ARIMA refits per bar, so a few hundred simulated paths is minutes of work rather "
            "than seconds. Start with a small path count."
        )

    monte_carlo, execution = st.tabs(["Monte Carlo — is the edge luck?", "Execution — would it have filled?"])
    with monte_carlo:
        _monte_carlo_panel(db, ctx, symbol, name)
    with execution:
        _execution_panel(db, ctx, symbol, name)


# --- AGENTS ------------------------------------------------------------------


def _observer_button(db: DatabaseManager, settings: Settings) -> None:
    """The one button on this page that does work, and its live stage stream."""
    if st.button("▶ Run observer now", type="primary", help="One observer run, written to the agent ledger."):
        # Each stage is written as it finishes, so a slow collect_metrics
        # over many tokens is visibly running rather than apparently hung.
        with st.status("Observing…", expanded=True) as progress:
            report = run_observer_now(
                settings, db, on_stage=lambda record: progress.write(format_stage(record))
            )
            progress.update(
                label=f"Observer finished {report.status_label.upper()} in {report.duration_seconds:.1f}s",
                state="error" if report.error else "complete",
                expanded=False,
            )
        color = SEVERITY_COLORS.get(report.status_label, "#90a4ae")
        st.markdown(
            f"**Observer finished :{'red' if color == DOWN else 'blue'}"
            f"[{report.status_label.upper()}]** — {report.summary}"
        )
        _symbols.clear()


def _coverage_chart(view: Any, settings: Settings, height: int = 260) -> None:
    """Stored against expected hours per token. Drawn on two pages, defined once."""
    _title("Coverage — stored vs expected hours", "100% is not the target: an hour with no swaps has no row.")
    if view.coverage.empty:
        st.info("No stored history yet.")
        return
    fig = go.Figure(
        go.Bar(
            x=view.coverage["coverage"],
            y=list(view.coverage.index),
            orientation="h",
            marker_color=[
                "#ffab00" if v < settings.min_expected_coverage else UP for v in view.coverage["coverage"]
            ],
        )
    )
    fig.update_layout(xaxis_tickformat=".0%")
    _plot(fig, height=height)


def _agents_metrics(view: Any) -> None:
    """The observer verdict, the two run counts, and the thinnest coverage."""

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


def _agents_news_strip(view: Any) -> None:
    """News health in four numbers; the News dashboard has the ledger and budget."""
    counts = view.news_counts
    failed = counts.get("error", 0) + counts.get("rate_limited", 0)
    news_cols = st.columns(4)
    news_cols[0].metric(
        "News source (Tavily)",
        view.news_status.upper(),
        help="The newest news call's outcome. The News dashboard has the full ledger and the budget.",
    )
    news_cols[1].metric("News runs ok", f"{counts.get('ok', 0)}", help="Of the recent ledgered calls.")
    news_cols[2].metric(
        "News runs failed",
        f"{failed}",
        delta=f"-{failed}" if failed else None,
        delta_color="inverse",
        help="Errors plus rate limits. Budget refusals are expected behaviour and counted separately.",
    )
    news_cols[3].metric("Budget refusals", f"{counts.get('budget_refused', 0)}")


def _stage_strip(frame: pd.DataFrame, title: str, caption: str, empty: str) -> None:
    """One metric per stage of a run, in order. Used for both ledgers."""
    _title(title, caption)
    if frame.empty:
        st.caption(empty)
        return
    stage_cols = st.columns(len(frame))
    for col, (_, row) in zip(stage_cols, frame.iterrows(), strict=True):
        seconds = f"{row['seconds']:.2f}s" if pd.notna(row["seconds"]) else "—"
        col.metric(str(row["stage"]), str(row["status"]).upper(), seconds, delta_color="off")
        col.caption(str(row["detail"]) or "—")


def _agents_findings(view: Any) -> None:
    """The latest observer report's findings, coloured by severity."""
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


def _agents_charts(view: Any, settings: Settings) -> None:
    """Ingest cadence beside per-token coverage."""
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
        _coverage_chart(view, settings)


def _agents_history(view: Any) -> None:
    """Every agent report, newest first."""
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


def _agents_page(db: DatabaseManager, settings: Settings) -> None:
    st.subheader("Watchtower — is the data fresh, complete, and still the same market?")
    _observer_button(db, settings)

    view = build_agents_view(db)
    _agents_metrics(view)
    _agents_news_strip(view)
    _stage_strip(
        view.stages,
        "Observer Stages — what the latest run did, in order",
        "A skipped stage means the database-down short-circuit fired; a slow one says where the time went.",
        "No stage records yet — runs recorded before this feature, or no run at all.",
    )
    _stage_strip(
        view.ingest_stages,
        "Ingest Stages — what the latest ingest run did, in order",
        "The same four steps `candlegraph ingest --progress` prints: metadata, token hours, "
        "pool discovery, pool hours.",
        "No stage records yet — the newest ingest run predates them.",
    )
    _agents_findings(view)
    _agents_charts(view, settings)
    _agents_history(view)


# --- NEWS (Tavily) --------------------------------------------------------------


# --- pipelines (read-only) -----------------------------------------------------
#
# Rollout "the harness, visible", step 2. Nothing on this page writes, enqueues
# or refreshes: there is deliberately no button. Step 3 adds "Run now", and it
# will ask the queue for a run rather than perform one (invariant 28).


def _coloured(frame: pd.DataFrame, column: str) -> Any:
    """A frame with one column coloured by run outcome or queue state."""
    return frame.style.map(
        lambda v: f"color: {OUTCOME_COLORS.get(str(v), '#90a4ae')}; font-weight: bold",
        subset=[column],
    )


def _pipelines_metrics(view: Any) -> None:
    """The newest run, how long ago, and what the queue is holding."""
    latest = view.latest
    cols = st.columns(5)
    cols[0].metric(
        "Latest pipeline run",
        f"{latest.pipeline} · {latest.outcome.upper()}" if latest else "—",
        help="The newest run of any pipeline. Partial: it finished, but not every step did everything.",
    )
    cols[1].metric("Started", age_text(view.latest_age_seconds))
    cols[2].metric(
        "Waiting in the queue",
        f"{view.waiting}",
        help="Queued or claimed: asked for by a trigger and not yet finished.",
    )
    cols[3].metric(
        "Dead rows",
        f"{view.dead}",
        delta=f"-{view.dead}" if view.dead else None,
        delta_color="inverse",
        help="Ran out of attempts. Each one needs a person to read its last_error.",
    )
    cols[4].metric(
        "Dispatcher",
        f"every {view.dispatcher_interval}s" if view.dispatcher_enabled else "OFF",
        help="EVENT_POLL_INTERVAL. The dispatcher is what turns a queued row into a run.",
    )


def _pipelines_warnings(view: Any) -> None:
    """The things that stop pipelines quietly, said out loud."""
    if not view.dispatcher_enabled:
        st.warning(
            "The dispatcher is off (`EVENT_POLL_INTERVAL=0`). No trigger will start a pipeline, "
            "and anything queued stays queued. `make run` still runs one by hand."
        )
    for problem in view.cross_spec_problems:
        st.error(f"Across the specs: {problem}")
    for pipeline, key, waited in view.unclaimed:
        st.warning(
            f"`{pipeline}` ({key}) has been ready to run for {age_text(waited).removesuffix(' ago')} "
            "and nothing has claimed it. The dispatcher runs in the `agents` service — check it is "
            "up (`make ps`, `make logs-agents`). This page can see what the dispatcher did, not "
            "whether it is alive."
        )
    broken = [card for card in view.specs if not card.loaded]
    if broken:
        st.error(
            f"{len(broken)} spec(s) refused by the loader, so they never run: "
            f"{', '.join(card.path for card in broken)}. The reasons are under Specs below."
        )
    last = view.last_dispatch
    if last is not None:
        st.caption(
            f"Dispatcher's newest recorded tick: {last['started_at']:%Y-%m-%d %H:%M} UTC, "
            f"{last['outcome']}. It records a tick only when it did something, so a quiet "
            "dispatcher is not a stopped one."
        )


def _pipeline_family(family: Any) -> None:
    """One run: where it came from, its strip, and every step's record."""
    started = f"{family.started_at:%Y-%m-%d %H:%M}" if family.started_at else "—"
    seconds = f"{family.duration_seconds:.1f}s" if family.duration_seconds is not None else "running"
    label = f"{started} · {family.pipeline} · {family.outcome} · {family.trigger or '-'} · {seconds}"
    with st.expander(label):
        why = [f"run `{family.run_id[:8]}`"]
        if family.caused_by:
            why.append(f"started by run `{family.caused_by[:8]}` finishing")
        if family.started_next:
            why.append(f"its finishing started: {', '.join(family.started_next)}")
        st.caption(" · ".join(why))
        _stage_strip(
            family.stages,
            "Steps, in order",
            "One stage per step, as the pipeline recorded it when it closed.",
            "Still running — the strip is written when the run closes. The table below is live.",
        )
        if family.live:
            st.caption("From the step runs so far: a step that has not started has no row yet.")
        if family.steps.empty:
            st.info("No step records.")
        else:
            st.dataframe(_coloured(family.steps, "status"), width="stretch", hide_index=True)


def _pipelines_runs(view: Any) -> None:
    """Recent runs of each pipeline, newest first, one expander per run."""
    _title(
        "Runs — newest first, per pipeline",
        "A run's steps are its child runs. Partial means it finished, but some step did not do everything.",
    )
    if not view.families:
        st.info(
            "No pipeline has run yet. A successful ingest starts `hourly`; "
            "`make run ARGS='hourly'` runs it now."
        )
        return
    for card in view.specs:
        runs = view.runs_of(card.name) if card.loaded else ()
        if runs:
            st.markdown(f"**{card.name}**")
            for family in runs:
                _pipeline_family(family)


def _pipelines_queue(view: Any) -> None:
    """What triggers asked for, with every dead row the queue still holds."""
    _title(
        "Queue — what a trigger asked for, and what became of it",
        "One row per requested run. The dedup key is why twelve ingests in an hour start one pipeline.",
    )
    if view.queue.empty:
        st.info("Nothing has been queued yet.")
    else:
        st.dataframe(_coloured(view.queue, "state"), width="stretch", hide_index=True)


def _ask_for_run(db: DatabaseManager, settings: Settings, pipeline: str) -> None:
    """The *Run now* callback: one queue row, never a run.

    A callback, not ``if st.button(...)``: Streamlit runs it *before* the
    rerun the click causes, so the queue table further up the page already
    shows the new row. Done inline, the table would be drawn first and the
    click would look as if it had done nothing until the next rerun.
    """
    st.session_state.pop("pipelines_pending", None)
    try:
        request = request_pipeline_run(db, settings, pipeline)
    except ViewError as exc:
        st.session_state["pipelines_notice"] = ("error", str(exc))
        return
    st.session_state["pipelines_awaiting"] = (request.pipeline, request.dedup_key)
    if request.entry is not None:
        st.session_state["pipelines_notice"] = (
            "success",
            f"Asked for one {pipeline} run: queue row #{request.entry.id}, dedup key "
            f"`{request.dedup_key}`. The dispatcher claims it within about "
            f"{settings.event_poll_interval}s; the run then appears under Runs. This page "
            "re-reads itself until that run has finished.",
        )
    else:
        st.session_state["pipelines_notice"] = (
            "info",
            f"{pipeline} was already asked for this minute (`{request.dedup_key}`), so this click "
            "changed nothing — a double-click is one run, not two.",
        )


def _forget_pending() -> None:
    st.session_state.pop("pipelines_pending", None)


def _ask_to_confirm(pipeline: str) -> None:
    st.session_state["pipelines_pending"] = pipeline


def _spend_confirmation(db: DatabaseManager, settings: Settings, card: Any) -> None:
    """The cost of a credit-spending pipeline, in numbers, before anything is queued."""
    estimate = estimate_pipeline_spend(db, settings)
    st.warning(
        f"**Queue a {card.name} run?** Its news step searches {len(estimate.tokens)} active token(s); "
        f"**up to {estimate.credits_at_most} credit(s)** — the ones not yet searched today "
        f"({', '.join(estimate.uncached) or 'none'}). Tokens already searched today are served "
        f"from the cache and cost nothing, and the budget guard may still refuse. Spent so far: "
        f"{estimate.spent_today}/{estimate.daily_cap} today, "
        f"{estimate.spent_month}/{estimate.monthly_cap} this month."
    )
    confirm, cancel = st.columns(2)
    confirm.button(
        "Confirm and queue",
        type="primary",
        key=f"pipeline_confirm_{card.name}",
        on_click=_ask_for_run,
        args=(db, settings, card.name),
    )
    cancel.button("Cancel", key=f"pipeline_cancel_{card.name}", on_click=_forget_pending)


def _run_now_control(db: DatabaseManager, settings: Settings, view: Any, card: Any) -> None:
    """*Run now* for one spec: disabled with the reason, confirmed if it spends, else immediate."""
    refusal = view.request_refusal
    if refusal:
        st.button("Run now", key=f"pipeline_run_{card.name}", disabled=True)
        st.caption(refusal)
        return
    if card.spends:
        st.button(
            "Run now…",
            key=f"pipeline_run_{card.name}",
            help="This pipeline spends search credits; the next step states the cost.",
            on_click=_ask_to_confirm,
            args=(card.name,),
        )
        if st.session_state.get("pipelines_pending") == card.name:
            _spend_confirmation(db, settings, card)
        return
    st.button(
        "Run now",
        key=f"pipeline_run_{card.name}",
        help="Queues one run with trigger `ui`; the dispatcher performs it. Nothing runs in this page.",
        on_click=_ask_for_run,
        args=(db, settings, card.name),
    )


def _pipelines_specs(db: DatabaseManager, settings: Settings, view: Any) -> None:
    """Each committed spec as the loader read it, with *Run now*; or the loader's refusal."""
    _title(
        "Specs — the committed pipelines, as the loader read them",
        "Run now asks the queue for a run, the same way an ingest or a schedule does.",
    )
    for card in view.specs:
        if not card.loaded:
            reasons = "\n".join(f"- {problem}" for problem in card.problems)
            st.error(f"**{card.path}** — refused by the loader, so it never runs:\n\n{reasons}")
            continue
        with st.container(border=True):
            st.markdown(f"**{card.name}** v{card.version} · `{card.path}` · deadline {card.deadline}")
            if card.description:
                st.caption(card.description)
            if card.starts_itself:
                st.markdown("Starts itself " + "; ".join(card.triggers) + ".")
            else:
                st.markdown("No trigger: it runs only when a person asks.")
            st.dataframe(card.steps, width="stretch", hide_index=True)
            _run_now_control(db, settings, view, card)


def _pipelines_live(view: Any) -> None:
    """The parts that change as the dispatcher works: metrics, warnings, runs, queue."""
    _pipelines_metrics(view)
    _pipelines_warnings(view)
    _pipelines_runs(view)
    _pipelines_queue(view)


def _pipelines_watch(db: DatabaseManager, settings: Settings) -> None:
    """The live parts, re-read every few seconds while a requested run is unfinished.

    Runs as a fragment only while ``pipelines_awaiting`` is set, so an idle
    page costs nothing. When the run it is watching has finished (or left the
    page), it forgets it and reruns the whole app once — which redraws the
    page without the timer, so the polling stops by itself.
    """
    view = build_pipelines_view(db, settings)
    _pipelines_live(view)
    awaiting = st.session_state.get("pipelines_awaiting")
    if awaiting is None:
        return
    state = view.state_of(*awaiting)
    if request_finished(state):
        st.session_state.pop("pipelines_awaiting", None)
        # The rerun below discards everything this run drew, including the
        # notice the click left. So the notice is replaced with how the request
        # ended — or carried over when its row has left the page — and the
        # rerun shows something true instead of nothing.
        st.session_state["pipelines_notice"] = _finished_notice(awaiting, state) or st.session_state.get(
            "pipelines_last_notice"
        )
        st.rerun()
    st.caption(f"Watching `{awaiting[1]}` ({state}) — re-reading every {PIPELINES_REFRESH_SECONDS}s.")


def _finished_notice(awaiting: tuple[str, str], state: str | None) -> tuple[str, str] | None:
    """How a requested run ended, in the queue's words. ``None`` if its row is not on the page."""
    pipeline, key = awaiting
    if state == "done":
        return (
            "info",
            f"The {pipeline} run you asked for (`{key}`) has finished. Its outcome and each step are "
            "under Runs — `done` means the dispatcher ran it, not that every step succeeded.",
        )
    if state == "dead":
        return (
            "error",
            f"The {pipeline} run you asked for (`{key}`) ran out of attempts and is dead. Its "
            "`last_error` is in the queue table; it needs a person.",
        )
    return None


def _pipelines_notice() -> None:
    """What the last click did, shown once."""
    notice = st.session_state.pop("pipelines_notice", None)
    if notice is not None:
        st.session_state["pipelines_last_notice"] = notice
        kind, text = notice
        {"success": st.success, "info": st.info, "error": st.error}[kind](text)


def _pipelines_page(db: DatabaseManager, settings: Settings) -> None:
    st.subheader("Pipelines — what ran, what is queued, and what each step did")
    st.caption("The one thing this page can do is ask: Run now queues a run, and the dispatcher performs it.")
    _pipelines_notice()
    view = build_pipelines_view(db, settings)
    if st.session_state.get("pipelines_awaiting"):
        st.fragment(_pipelines_watch, run_every=PIPELINES_REFRESH_SECONDS)(db, settings)
    else:
        _pipelines_live(view)
    _pipelines_specs(db, settings, view)


def _news_metrics(view: Any) -> None:
    """Budget, corpus size, and the newest call's outcome."""
    last = view.last_call
    last = view.last_call
    cols = st.columns(6)
    cols[0].metric(
        "Month",
        f"{view.spent_month} / {view.monthly_cap}",
        delta=f"{view.monthly_usage:.0%} used · {view.level.upper()}",
        delta_color="off",
        help="Credits this UTC month against TAVILY_MONTHLY_CREDITS. "
        "WARNING at 80%, CRITICAL at 100% — the observer's own grading.",
    )
    cols[1].metric(
        "Today",
        f"{view.spent_today} / {view.daily_cap}",
        help="The daily cap smooths the month; a runaway loop hits this first.",
    )
    cols[2].metric(
        "Reserve",
        f"{view.reserve}",
        help="Meant for manual searches. Known gap: no search may spend it today, manual or "
        "not — see docs/knowledge/budget-manager/DESIGN.md.",
    )
    cols[3].metric(
        "Documents",
        f"{int(view.corpus['documents']):,}",
        help=f"{int(view.corpus['chunks']):,} chunks, {int(view.corpus['embedded']):,} embedded.",
    )
    cols[4].metric(
        "Undated",
        f"{int(view.corpus['undated']):,}",
        help="Documents without a publication date: stored, shown, "
        "but never fed to a strategy (no look-ahead).",
    )
    cols[5].metric(
        "Last call",
        str(last["status"]).upper() if last else "—",
        help="Every call is a ledger row, refusals included.",
    )


def _news_runway(view: Any) -> None:
    """How much of the month and the day is gone, and what that means."""
    st.progress(min(view.monthly_usage, 1.0), text=f"Credit Runway — month ({view.monthly_usage:.0%})")
    st.progress(min(view.daily_usage, 1.0), text=f"Credit Runway — today ({view.daily_usage:.0%})")
    if view.level != "ok":
        st.warning(
            f"Budget at {view.monthly_usage:.0%} of the month — "
            f"the observer grades this {view.level.upper()}. "
            "New searches will be refused at the reserve; explanations fall back to stored documents."
        )
    if view.embedding_pending:
        st.info(
            f"{view.embedding_pending} chunks await embedding — run `make embed` so retrieval can see them."
        )


def _news_fetch_panel(db: DatabaseManager, settings: Settings, view: Any, ctx: dict[str, Any]) -> None:
    """The one control that spends credits: pick tokens, confirm, fetch."""
    _title(
        "Fetch news now — one budgeted search per token per UTC day",
        "Cached days cost nothing; the guard refuses before the provider would.",
    )
    left, right = st.columns([1, 2])
    chosen = left.multiselect(
        "Tokens (empty = the active tokens)", ctx["symbols"], default=[], key="news_symbols"
    )
    if left.button("Fetch news…", key="news_ask"):
        st.session_state["news_pending"] = chosen or None
    if "news_pending" in st.session_state:
        pending = st.session_state["news_pending"]
        with right:
            count = len(pending) if pending else len(view.default_symbols)
            st.warning(
                f"**Search Tavily for {count} token(s)?** Costs up to {count} credit(s) "
                f"({view.spent_month}/{view.monthly_cap} already spent this month); tokens already "
                "searched today are served from cache and cost nothing."
            )
            confirm, cancel = st.columns(2)
            if confirm.button("Confirm fetch", type="primary", key="news_confirm"):
                st.session_state.pop("news_pending", None)
                try:
                    with st.spinner("Searching…"):
                        summary = run_news_fetch_now(settings, db, pending)
                except ViewError as exc:
                    st.error(str(exc))
                else:
                    tone = st.success if summary.ok else st.error
                    tone(
                        f"Spent {summary.credits_spent} credit(s), stored {summary.documents_stored} "
                        f"document(s); {len(summary.failed)} failure(s)."
                    )
                    outcome_frame = pd.DataFrame([vars(o) for o in summary.outcomes]).set_index("symbol")
                    st.dataframe(
                        outcome_frame.style.map(
                            lambda v: f"color: {_news_color(str(v))}; font-weight: bold", subset=["status"]
                        ),
                        width="stretch",
                    )
            if cancel.button("Cancel", key="news_cancel"):
                st.session_state.pop("news_pending", None)
                st.rerun()


def _news_charts(view: Any) -> None:
    """Spend per day against the cap, call outcomes, and what the credits bought."""
    left, right = st.columns(2)
    with left:
        _title("Credit Burn — spend per UTC day vs the daily cap")
        if view.daily.empty:
            st.info("No calls ledgered yet.")
        else:
            burn = go.Figure(
                go.Bar(x=view.daily["day"], y=view.daily["credits"], marker_color="#2962ff", name="credits")
            )
            burn.add_hline(
                y=view.daily_cap, line_color="#ffab00", line_dash="dash", annotation_text="daily cap"
            )
            _plot(burn, height=260)
    with right:
        _title(
            "Call Outcomes — did the news runs succeed?",
            "Refusals are the guard working; errors and rate limits are the provider or us.",
        )
        if view.daily.empty:
            st.info("No calls ledgered yet.")
        else:
            outcomes = go.Figure(
                [
                    go.Bar(name="ok", x=view.daily["day"], y=view.daily["ok"], marker_color=UP),
                    go.Bar(
                        name="refused", x=view.daily["day"], y=view.daily["refused"], marker_color="#ffab00"
                    ),
                    go.Bar(
                        name="rate limited",
                        x=view.daily["day"],
                        y=view.daily["rate_limited"],
                        marker_color=DOWN,
                    ),
                    go.Bar(name="error", x=view.daily["day"], y=view.daily["errors"], marker_color="#b71c1c"),
                ]
            )
            outcomes.update_layout(barmode="stack")
            _plot(outcomes, height=260)

    if not view.daily.empty:
        _title("Harvest — raw results per day", "Zero results on a liquid token is a finding in itself.")
        _plot(
            go.Figure(go.Bar(x=view.daily["day"], y=view.daily["results"], marker_color="#607d8b")),
            height=180,
        )


def _news_tables(view: Any) -> None:
    """Every ledgered call, and the newest stored documents."""
    _title("The Ledger — every call, refusals included, newest first")
    if view.calls.empty:
        st.info("No source calls yet. Press “Fetch news…” above (needs TAVILY_API_KEY in .env).")
    else:
        st.dataframe(
            view.calls.style.map(
                lambda v: f"color: {_news_color(str(v))}; font-weight: bold", subset=["status"]
            ),
            width="stretch",
        )

    _title("Fresh Copy — newest stored news", "An unknown publication date stays blank, never “now”.")
    if view.documents.empty:
        st.info("No news documents stored yet.")
    else:
        st.dataframe(view.documents, width="stretch")


def _news_page(db: DatabaseManager, settings: Settings, ctx: dict[str, Any]) -> None:
    st.subheader("News — the metered Tavily pipeline: budget, ledger, harvest")
    view = build_news_view(db, settings)

    _news_metrics(view)
    _news_runway(view)
    _news_fetch_panel(db, settings, view, ctx)
    _news_charts(view)
    _news_tables(view)


def _news_color(status: str) -> str:
    return {
        "ok": UP,
        "cached": "#2962ff",
        "budget_refused": "#ffab00",
        "rate_limited": DOWN,
        "error": "#b71c1c",
    }.get(status, "#90a4ae")


# --- DATA ---------------------------------------------------------------------


def _data_metrics(view: Any) -> None:
    """How much history is stored, and how the last run went."""
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


def _backfill_panel(db: DatabaseManager, settings: Settings, view: Any) -> None:
    """The window picker and the two-step confirmation a backfill needs."""
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


def _data_tables(view: Any, settings: Settings) -> None:
    """Coverage per token, and the recent ingest runs."""
    left, right = st.columns(2)
    with left:
        _coverage_chart(view, settings, height=280)
    with right:
        _title("Recent Ingest Runs — newest first")
        if view.ingest_runs.empty:
            st.info("No ingest runs recorded yet.")
        else:
            st.dataframe(view.ingest_runs, width="stretch")


def _data_page(db: DatabaseManager, settings: Settings) -> None:
    st.subheader("Data — what is stored, and loading more of it")
    st.caption(
        f"A first-time token is fetched {settings.lookback_days} days back (LOOKBACK_DAYS). "
        "A backfill reaches further; it rewinds past what is stored and re-writes nothing twice."
    )
    view = build_agents_view(db)

    _data_metrics(view)
    _backfill_panel(db, settings, view)
    _data_tables(view, settings)


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
            case "Quality":
                _quality_page(db, settings, ctx)
            case "Risk":
                _risk_page(db, settings, ctx)
            case "Compare":
                _compare_page(db, settings, ctx)
            case "Portfolio":
                _portfolio_page(db, settings, ctx)
            case "Strategy":
                _strategy_page(db, settings, ctx)
            case "Simulation":
                _simulation_page(db, settings, ctx)
            case "Forecast":
                _forecast_page(db, ctx)
            case "Agents":
                _agents_page(db, settings)
            case "Pipelines":
                _pipelines_page(db, settings)
            case "News":
                _news_page(db, settings, ctx)
            case "Data":
                _data_page(db, settings)
    except ViewError as exc:
        st.info(str(exc))


# Streamlit executes this script with ``__name__ == "__main__"``, so the guard
# both runs the app and keeps the module importable by the tests.
if __name__ == "__main__":
    main()
