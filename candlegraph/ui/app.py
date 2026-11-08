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
from candlegraph.ingestion.fills import METHOD_POLICY, METHOD_SUMMARY
from candlegraph.ingestion.fills import METHODS as FILL_METHODS
from candlegraph.settings import Settings, get_settings
from candlegraph.storage import DatabaseManager, get_db
from candlegraph.strategies import available
from candlegraph.strategies.arima_signal import ArimaSignalConfig
from candlegraph.strategies.backtest import BacktestConfig
from candlegraph.strategies.bollinger import BollingerConfig
from candlegraph.strategies.crossover import CrossoverConfig
from candlegraph.ui.viewmodels import (
    BACKFILL_CHOICES,
    DEFAULT_PAIR,
    MAX_SHADED_GAPS,
    MAX_WEIGHT_STEP,
    Preferences,
    ViewError,
    available_symbols,
    build_agents_view,
    build_forecast,
    build_market_view,
    build_method_scores,
    build_news_view,
    build_pair_view,
    build_portfolio_view,
    build_quality_view,
    build_risk_view,
    build_strategy_view,
    build_token_view,
    candle_frame,
    default_index,
    default_max_weight,
    default_portfolio_symbols,
    forecast_frame,
    gap_lane_segments,
    load_stored_explanation,
    longest_gaps,
    method_catalogue,
    range_lane_segments,
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
    "Forecast",
    "Agents",
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


# --- RISK --------------------------------------------------------------------


def _risk_page(db: DatabaseManager, settings: Settings, ctx: dict[str, Any]) -> None:
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
    first, second = DEFAULT_PAIR
    a, b, c = st.columns(3)
    symbol_a = _pick(a, "Token A", ctx["symbols"], "pair_a", default_index(ctx["symbols"], first))
    others = [s for s in ctx["symbols"] if s != symbol_a] or ctx["symbols"]
    symbol_b = _pick(b, "Token B", others, "pair_b", default_index(others, second, exclude=symbol_a))
    window = int(c.number_input("Rolling window (bars)", 2, 2000, settings.rolling_window * 7, 1))

    view = build_pair_view(
        db, symbol_a, symbol_b, ctx["days"], window, settings.analytics_interval_hours, ctx["risk_free"]
    )
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


def _strategy_page(db: DatabaseManager, settings: Settings, ctx: dict[str, Any]) -> None:
    left, right = st.columns(2)
    symbol = _pick(left, "Token", ctx["symbols"], "strategy_symbol", default_index(ctx["symbols"]))
    name = _pick(right, "Strategy", list(available()), "strategy_name", 1)

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

    _explanation_panel(db, settings, view, symbol, name)


# --- FORECAST ----------------------------------------------------------------


def _forecast_page(db: DatabaseManager, ctx: dict[str, Any]) -> None:
    a, b, c, d = st.columns(4)
    symbol = _pick(a, "Token", ctx["symbols"], "forecast_symbol", default_index(ctx["symbols"]))
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

    _title(
        "Stages — what the latest run did, in order",
        "A skipped stage means the database-down short-circuit fired; a slow one says where the time went.",
    )
    if view.stages.empty:
        st.caption("No stage records yet — runs recorded before this feature, or no run at all.")
    else:
        stage_cols = st.columns(len(view.stages))
        for col, (_, row) in zip(stage_cols, view.stages.iterrows(), strict=True):
            seconds = f"{row['seconds']:.2f}s" if pd.notna(row["seconds"]) else "—"
            col.metric(str(row["stage"]), str(row["status"]).upper(), seconds, delta_color="off")
            col.caption(str(row["detail"]) or "—")

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


# --- NEWS (Tavily) --------------------------------------------------------------


def _news_page(db: DatabaseManager, settings: Settings, ctx: dict[str, Any]) -> None:
    st.subheader("News — the metered Tavily pipeline: budget, ledger, harvest")
    view = build_news_view(db, settings)

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
        help="Held back for manual searches; the pipeline treats it as already spent.",
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

    _title(
        "Fetch news now — one budgeted search per token per UTC day",
        "Cached days cost nothing; the guard refuses before the provider would.",
    )
    left, right = st.columns([1, 2])
    chosen = left.multiselect("Tokens (empty = all tracked)", ctx["symbols"], default=[], key="news_symbols")
    if left.button("Fetch news…", key="news_ask"):
        st.session_state["news_pending"] = chosen or None
    if "news_pending" in st.session_state:
        pending = st.session_state["news_pending"]
        with right:
            count = len(pending) if pending else len(ctx["symbols"])
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


def _news_color(status: str) -> str:
    return {
        "ok": UP,
        "cached": "#2962ff",
        "budget_refused": "#ffab00",
        "rate_limited": DOWN,
        "error": "#b71c1c",
    }.get(status, "#90a4ae")


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
            case "Forecast":
                _forecast_page(db, ctx)
            case "Agents":
                _agents_page(db, settings)
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
