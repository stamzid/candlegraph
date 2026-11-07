"""Everything the UI computes, with no Streamlit in sight.

Streamlit scripts are hard to test: they are top-to-bottom modules whose
output is a side effect on a global session. So all the work lives here as
plain functions returning plain data, and ``app.py`` stays a thin layer of
widgets and charts over them. These functions carry the test coverage; the
rendering does not need any.

One view class per dashboard: market, token, risk, pair, portfolio,
strategy, forecast, agents. Each builder loads exactly what its screen
needs and raises :class:`ViewError` with a sentence a trader can act on.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from candlegraph.agents.base import AgentReport
from candlegraph.agents.observer import run_observer
from candlegraph.agents.reporting import build_sinks
from candlegraph.agents.stages import StageHook, StageRecord
from candlegraph.analytics.arima import ArimaConfig, ArimaForecast, fit_forecast
from candlegraph.analytics.pairwise import PairwiseComparison, compare_pair
from candlegraph.analytics.portfolio import (
    FrontierPoint,
    PortfolioConfig,
    PortfolioError,
    PortfolioResult,
    annualised_moments,
    efficient_frontier,
    optimise_portfolio,
)
from candlegraph.analytics.prices import (
    DEFAULT_MAX_FILL,
    load_price_frame,
    load_price_history,
    to_wide,
    window_bounds,
)
from candlegraph.analytics.risk import (
    METHODS,
    RiskConfig,
    RiskError,
    RiskReport,
    conditional_value_at_risk,
    portfolio_returns,
    risk_report,
    value_at_risk,
)
from candlegraph.analytics.rolling import (
    bollinger_bands,
    drawdown_curve,
    equity_curve,
    log_returns,
    periods_per_year,
    rolling_summary,
    simple_returns,
)
from candlegraph.ingestion.fills import (
    LINEAR,
    FillSummary,
    fill_cache_summary,
    load_fills,
    method_is_causal,
    refresh_fill_cache,
)
from candlegraph.ingestion.quality import (
    HOUR,
    Gap,
    QualityReport,
    build_report,
    group_missing_hours,
)
from candlegraph.ingestion.runner import IngestResult, run_ingest
from candlegraph.knowledge.budget import Level, usage_fraction, usage_level
from candlegraph.knowledge.explain import (
    ExplainError,
    Explanation,
    explain,
    load_explanation,
    request_for,
    services_for,
)
from candlegraph.knowledge.news import NewsError, NewsFetchSummary, budget_from, run_news_fetch, source_spend
from candlegraph.knowledge.retrieve import RetrievalError
from candlegraph.knowledge.sources.tavily import SOURCE_ID as TAVILY
from candlegraph.knowledge.statements import knowledge_statements_for
from candlegraph.settings import Settings
from candlegraph.storage import DatabaseManager
from candlegraph.strategies import backtest, run_strategy
from candlegraph.strategies.backtest import BacktestConfig, BacktestResult
from candlegraph.strategies.base import StrategyResult
from candlegraph.tokens import get_token_registry


class ViewError(RuntimeError):
    """Something the user needs to fix, phrased for the screen rather than a log."""


# --- sensible defaults ---------------------------------------------------------

# What a trader expects to see first. Alphabetical order put AAVE on every
# screen; the most liquid token is the right opening position.
DEFAULT_SYMBOL = "WBTC"
# A dollar: the natural reference for "how did this do", and a stable leg for
# the relative-value ratio.
DEFAULT_BENCHMARK = "USDT"
# Compare opens on two volatile majors instead. Pricing a token in USDT is a
# dollar chart, which the Token dashboard already draws; the question this
# screen answers — do these two move together, and which one leads — needs a
# second asset that actually moves.
DEFAULT_PAIR = ("WETH", "WBTC")
# USD-pegged assets have near-zero variance and swallow any minimum-risk
# objective, so a default book leaves them out. They are one click away.
STABLECOINS = frozenset({"USDC", "USDT", "DAI"})
# A quarter of the book per token: diversified by default, overridable.
DEFAULT_MAX_WEIGHT = 0.25
MAX_WEIGHT_STEP = 0.05
# The windows the Data dashboard offers for a backfill.
BACKFILL_CHOICES: dict[str, int] = {"7 days": 7, "30 days": 30, "90 days": 90, "180 days": 180, "1 year": 365}


def default_index(options: list[str], preferred: str = DEFAULT_SYMBOL, exclude: str | None = None) -> int:
    """Position of ``preferred`` in ``options``, or the first option that is not ``exclude``."""
    if preferred in options and preferred != exclude:
        return options.index(preferred)
    for position, option in enumerate(options):
        if option != exclude:
            return position
    return 0


# --- remembering what the user picked ----------------------------------------

# Streamlit drops a widget's state as soon as a rerun does not instantiate
# it, so switching dashboard and coming back resets every control on the page
# you left. Mirroring the choices into a plain namespace under this prefix is
# what survives the round trip; the widgets re-open on the mirror.
PREFIX = "pref:"


class Preferences:
    """The last choice made on each control, kept across dashboard switches.

    Wraps any mutable mapping — ``st.session_state`` in the app, a plain
    ``dict`` in the tests — so the remembering logic is ordinary Python and
    carries its own coverage. It stores only what the user picked, never a
    derived number: a stale *result* would be a bug, a stale *selection* is
    the point.
    """

    def __init__(self, store: Any) -> None:
        self._store = store

    def _key(self, name: str) -> str:
        return f"{PREFIX}{name}"

    def get(self, name: str, fallback: Any = None) -> Any:
        """What was chosen for ``name`` last time, or ``fallback``."""
        try:
            return self._store[self._key(name)]
        except KeyError:
            return fallback

    def remember(self, name: str, value: Any) -> Any:
        """Record ``value`` as the current choice and hand it straight back."""
        self._store[self._key(name)] = value
        return value

    def index(self, name: str, options: list[str], fallback: int = 0) -> int:
        """Where a remembered option sits in ``options`` today.

        Falls back when the remembered value is gone — a token can leave the
        list between visits, and a stale index would silently select the
        wrong one rather than failing.
        """
        remembered = self.get(name)
        if isinstance(remembered, str) and remembered in options:
            return options.index(remembered)
        return fallback if 0 <= fallback < len(options) else 0

    def multiselect(self, name: str, options: list[str], fallback: list[str]) -> list[str]:
        """Remembered multi-selection, narrowed to what is still available."""
        remembered = self.get(name)
        if isinstance(remembered, list):
            kept = [value for value in remembered if value in options]
            if kept:
                return kept
        return [value for value in fallback if value in options]

    def names(self) -> list[str]:
        return sorted(str(k)[len(PREFIX) :] for k in list(self._store) if str(k).startswith(PREFIX))

    def reset(self) -> int:
        """Forget every remembered choice. Returns how many were cleared.

        Only the ``pref:`` namespace goes: a pending backfill confirmation or
        a news fetch in flight is not a preference, and clearing those would
        turn a harmless button into a surprising one.
        """
        keys = [k for k in list(self._store) if str(k).startswith(PREFIX)]
        for key in keys:
            del self._store[key]
        return len(keys)


def default_portfolio_symbols(symbols: list[str], limit: int = 5) -> list[str]:
    """The opening book: majors first, stablecoins out, at most ``limit`` names.

    Falls back to whatever is stored when nothing but stablecoins is.
    """
    preferred = [s for s in (DEFAULT_SYMBOL, "WETH") if s in symbols]
    others = [s for s in symbols if s not in preferred and s not in STABLECOINS]
    chosen = (preferred + others)[:limit]
    return chosen if len(chosen) >= 2 else symbols[:limit]


def default_max_weight(
    n_assets: int, target: float = DEFAULT_MAX_WEIGHT, step: float = MAX_WEIGHT_STEP
) -> float:
    """``target`` unless fewer than ``1/target`` assets make it infeasible; then the feasible floor.

    Two tokens capped at 25% each cannot sum to one; the slider has to open on
    a value the optimiser can satisfy, rounded up to the slider's own step.
    """
    if n_assets <= 0:
        return 1.0
    floor = float(np.ceil((1.0 / n_assets) / step - 1e-9) * step)
    return float(min(1.0, max(target, floor)))


# --- shared loading ----------------------------------------------------------


def available_symbols(db: DatabaseManager) -> list[str]:
    """Symbols that actually have stored rows.

    Reads what is in the database rather than what is configured: a token
    added to the config an hour ago has nothing to plot yet.
    """
    rows = db.fetch_all(db.sql.coverage_report, {"start_unix": None})
    return sorted(row["symbol"] for row in rows if row.get("symbol"))


def load_prices(db: DatabaseManager, symbols: list[str], days: int | None) -> pd.DataFrame:
    """Wide price frame, raising a user-facing error when there is nothing."""
    if not symbols:
        raise ViewError("Pick at least one token.")
    frame = load_price_frame(db, symbols, days=days)
    if frame.empty:
        raise ViewError(f"No stored price history for {', '.join(symbols)} in that window.")
    return frame


def _one_symbol(db: DatabaseManager, symbol: str, days: int | None) -> pd.Series:
    frame = load_prices(db, [symbol], days)
    if symbol not in frame.columns or frame[symbol].dropna().empty:
        raise ViewError(f"No stored price history for {symbol} in that window.")
    prices = frame[symbol].dropna()
    prices.name = symbol
    return prices


def _trailing_change(prices: pd.Series, bars: int) -> float:
    """Return over the last ``bars`` bars, NaN when the history is shorter."""
    clean = prices.dropna()
    if len(clean) <= bars:
        return float("nan")
    past = float(clean.iloc[-(bars + 1)])
    return float(clean.iloc[-1]) / past - 1.0 if past > 0 else float("nan")


def observed_gaps(unix_hours: Any) -> list[Gap]:
    """Runs of missing hourly buckets inside an observed series' own range.

    Pure. The grouping is :func:`quality.group_missing_hours`, the same
    function the data-quality report uses, so the UI and ``candlegraph
    report`` can never disagree about what counts as one gap.

    Only the *interior* is examined: a token that started trading late has
    not got a gap before its first bar, it has a shorter history. That is
    the same rule ``coverage`` uses (``expected_hours`` spans the token's
    own stored range, not the window).
    """
    present = sorted({int(u) for u in unix_hours})
    if len(present) < 2:
        return []
    seen = set(present)
    missing = [unix for unix in range(present[0], present[-1] + HOUR, HOUR) if unix not in seen]
    return group_missing_hours(missing)


def fill_overlay(prices: pd.Series, fills: pd.DataFrame) -> pd.Series:
    """Interpolated hours plus the observed bar either side of each run. Pure.

    Anchoring on the neighbours is what makes the overlay readable: a bare
    scatter of filled points inside a gap says nothing about what it was
    drawn between, and a line that starts and ends on a real bar says
    exactly that.
    """
    if fills.empty or prices.empty:
        return pd.Series(dtype="float64")

    observed = prices.dropna().astype("float64")
    estimates = pd.Series(
        fills["price_usd"].to_numpy(dtype="float64"),
        index=pd.DatetimeIndex(fills["timestamp"]),
    )
    combined = pd.concat([observed, estimates]).sort_index()
    combined = combined[~combined.index.duplicated(keep="first")]

    keep = pd.Series(False, index=combined.index)
    keep.loc[estimates.index] = True
    positions = [combined.index.get_loc(moment) for moment in estimates.index]
    for position in positions:
        # The observation on each side of a filled hour anchors the segment.
        for neighbour in (position - 1, position + 1):
            if 0 <= neighbour < len(combined):
                keep.iloc[neighbour] = True

    overlay: pd.Series = combined.where(keep)
    return overlay


def longest_gaps(gaps: list[Gap], limit: int) -> list[Gap]:
    """The ``limit`` longest gaps, back in chronological order.

    A thin token can have hundreds of one-hour holes; shading every one of
    them turns a chart into a barcode and is slow to render. The longest are
    the ones a reader can act on.
    """
    if limit < 1 or len(gaps) <= limit:
        return list(gaps)
    chosen = sorted(gaps, key=lambda gap: gap.hours, reverse=True)[:limit]
    return sorted(chosen, key=lambda gap: gap.start_unix)


# --- market dashboard --------------------------------------------------------


@dataclass
class MarketView:
    """The whole tape on one screen: relative strength, movers, co-movement."""

    rebased: pd.DataFrame
    """Every token's price divided by its first bar, times 100."""
    snapshot: pd.DataFrame
    """One row per token: last price, trailing changes, volatility, volume."""
    correlation: pd.DataFrame
    """Pairwise hourly log-return correlation, aligned bars only."""
    bars: int


def build_market_view(
    db: DatabaseManager, symbols: list[str], days: int | None, interval_hours: int = 1
) -> MarketView:
    """Everything the market dashboard shows, from one read."""
    if not symbols:
        raise ViewError("Pick at least one token.")
    start_unix, end_unix = window_bounds(days)
    history = load_price_history(db, symbols, start_unix, end_unix)
    if history.empty:
        raise ViewError(f"No stored price history for {', '.join(symbols)} in that window.")

    display = to_wide(history, fill="ffill")
    honest = to_wide(history, fill="none")
    factor = float(np.sqrt(periods_per_year(interval_hours)))

    first = display.apply(lambda col: col.dropna().iloc[0] if col.notna().any() else np.nan)
    rebased = display.divide(first, axis=1) * 100.0

    long = history.frame
    day_ago = long["period_start_unix"].max() - 24 * 3600
    volume_24h = long[long["period_start_unix"] > day_ago].groupby("symbol")["volume_usd"].sum()

    rows = {}
    for symbol in display.columns:
        prices = display[symbol]
        returns = log_returns(honest[symbol]).dropna()
        rows[symbol] = {
            "last": float(prices.dropna().iloc[-1]) if prices.notna().any() else float("nan"),
            "change_24h": _trailing_change(prices, 24),
            "change_7d": _trailing_change(prices, 24 * 7),
            "change_30d": _trailing_change(prices, 24 * 30),
            "volatility": float(returns.std(ddof=1)) * factor if len(returns) > 1 else float("nan"),
            "max_drawdown": float(drawdown_curve(equity_curve(simple_returns(prices).fillna(0.0))).min()),
            "volume_24h": float(volume_24h.get(symbol, float("nan"))),
        }
    snapshot = pd.DataFrame.from_dict(rows, orient="index").sort_values("change_24h", ascending=False)

    # Aligned bars only: correlating a real price with a forward-filled one
    # measures the fill policy, not the market.
    correlation = honest.apply(log_returns).corr()

    return MarketView(rebased=rebased, snapshot=snapshot, correlation=correlation, bars=len(display))


# --- token dashboard ---------------------------------------------------------


@dataclass
class TokenView:
    """One token's screen: candles, bands, volatility, stretch, drawdown."""

    symbol: str
    ohlc: pd.DataFrame
    """Hourly open/high/low/close/volume_usd, indexed by UTC timestamp."""
    prices: pd.Series
    summary: pd.DataFrame
    bands: pd.DataFrame
    drawdown: pd.Series
    window: int
    var_1d: float
    """One-day 95% historical VaR as a positive loss fraction; NaN when short."""
    gaps: list[Gap] = field(default_factory=list)
    """Runs of hours with no stored row, inside this window's own range.

    Descriptive only: nothing here fills them. A gap is not a fault
    (invariant 19) — Uniswap emits no row for an hour with no swaps — but a
    reader comparing two charts deserves to know which one has holes in it.
    """
    fills: pd.DataFrame = field(default_factory=pd.DataFrame)
    """Cached estimates for those hours, read rather than recomputed.

    Empty when the cache has not been refreshed, which is a normal state and
    not an error. These are **display only**: the stored method is linear
    interpolation, which reads the gap's right endpoint, so nothing that
    trades may consume them.
    """

    @property
    def filled_hours(self) -> int:
        return len(self.fills)

    @property
    def fill_method(self) -> str | None:
        return str(self.fills.iloc[0]["method"]) if self.filled_hours else None

    @property
    def fill_overlay(self) -> pd.Series:
        """The interpolated path, anchored to the real bar on either side.

        Each run of filled hours is drawn as one connected segment that
        starts and ends on an observation, so the line visibly bridges the
        hole rather than floating inside it. Everything outside a run is
        ``NaN``, which is what keeps the segments separate.
        """
        return fill_overlay(self.prices, self.fills)

    @property
    def bars(self) -> int:
        return len(self.prices)

    @property
    def missing_hours(self) -> int:
        """Hours with no row, inside the plotted range."""
        return sum(gap.hours for gap in self.gaps)

    @property
    def span_hours(self) -> int:
        """Hours between the first and last stored bar, inclusive."""
        return self.bars + self.missing_hours

    @property
    def coverage(self) -> float:
        """Observed fraction of the hours in the plotted range."""
        span = self.span_hours
        return self.bars / span if span else 0.0

    @property
    def longest_gap(self) -> Gap | None:
        return max(self.gaps, key=lambda gap: gap.hours, default=None)

    @property
    def latest(self) -> dict[str, float]:
        """Headline numbers for the metric strip, skipping incomplete windows."""
        out: dict[str, float] = {
            "price": float(self.prices.iloc[-1]) if self.bars else 0.0,
            "change_24h": _trailing_change(self.prices, 24),
            "max_drawdown": float(self.drawdown.min()) if len(self.drawdown) else 0.0,
            "var_1d": self.var_1d,
        }
        complete = self.summary.dropna()
        if not complete.empty:
            row = complete.iloc[-1]
            out["rolling_mean"] = float(row["rolling_mean"])
            out["rolling_volatility"] = float(row["rolling_volatility"])
        return out


def build_token_view(
    db: DatabaseManager,
    symbol: str,
    days: int | None,
    window: int,
    num_std: float = 2.0,
    interval_hours: int = 1,
    fill_method: str = LINEAR,
) -> TokenView:
    """Candles plus every per-token statistic the token dashboard plots."""
    start_unix, end_unix = window_bounds(days)
    history = load_price_history(db, [symbol], start_unix, end_unix)
    if history.empty:
        raise ViewError(f"No stored price history for {symbol} in that window.")

    ohlc = history.for_symbol(symbol)[["open", "high", "low", "close", "volume_usd"]]
    prices = history.close(symbol)
    if prices.empty:
        raise ViewError(f"No stored price history for {symbol} in that window.")
    if len(prices) < window:
        raise ViewError(
            f"{symbol} has {len(prices)} bars, fewer than the {window}-bar window. "
            "Widen the date range or shorten the window."
        )

    returns = simple_returns(prices).dropna()
    try:
        var_1d = value_at_risk(returns, RiskConfig(confidence=0.95, horizon_bars=24))
    except RiskError:
        var_1d = float("nan")

    return TokenView(
        symbol=symbol,
        ohlc=ohlc,
        prices=prices,
        summary=rolling_summary(prices, window=window, interval_hours=interval_hours),
        bands=bollinger_bands(prices, window=window, num_std=num_std),
        drawdown=drawdown_curve(equity_curve(returns)),
        window=window,
        var_1d=var_1d,
        # The rows that were actually read, not a reindexed grid: this is the
        # one place that knows which hours the subgraph really emitted.
        gaps=observed_gaps(history.for_symbol(symbol)["period_start_unix"]),
        # Read, never recomputed: `candlegraph fill` (or the Quality page's
        # button) is what populates this, so a widget change costs one
        # indexed select rather than an interpolation over the whole window.
        fills=load_fills(db, [symbol], method=fill_method, start_unix=start_unix, end_unix=end_unix),
    )


def candle_frame(ohlc: pd.DataFrame, target_bars: int = 320) -> pd.DataFrame:
    """Resample hourly OHLC to at most ~``target_bars`` candles for display.

    Thousands of hourly candles render as a smear; a trader reads a few
    hundred. Open takes the first bar, close the last, high/low the extremes,
    volume the sum — and an interval with no bars at all stays absent rather
    than zero-filled (invariant 6's shape, applied to display).
    """
    if ohlc.empty or len(ohlc) <= target_bars:
        return ohlc
    hours = int(np.ceil(len(ohlc) / target_bars))
    sampled = ohlc.resample(f"{hours}h").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume_usd": "sum"}
    )
    return sampled.dropna(subset=["open", "close"])


# --- quality dashboard -------------------------------------------------------

# Beyond this many gap lanes the map stops being readable; the table below it
# still lists every token.
MAX_GAP_LANES = 25
# Gaps shaded on one token's price chart. See :func:`longest_gaps`.
MAX_SHADED_GAPS = 40


def quality_tokens_frame(report: QualityReport, now: datetime) -> pd.DataFrame:
    """One row per token: coverage, gaps, and how stale the last bar is.

    Pure. The same numbers ``candlegraph report`` prints, plus ``stale_hours``,
    which the fixed-width table has no room for and a screen does.
    """
    columns = [
        "symbol",
        "stored_hours",
        "expected_hours",
        "missing_hours",
        "coverage",
        "gaps",
        "longest_gap_hours",
        "stale_hours",
        "first",
        "last",
        "under_covered",
    ]
    rows = []
    for token in sorted(report.tokens, key=lambda t: t.coverage):
        longest = token.longest_gap
        rows.append(
            {
                "symbol": token.symbol,
                "stored_hours": token.stored_hours,
                "expected_hours": token.expected_hours,
                "missing_hours": token.missing_hours,
                "coverage": token.coverage,
                "gaps": len(token.gaps),
                "longest_gap_hours": longest.hours if longest else 0,
                "stale_hours": (now - token.last).total_seconds() / HOUR if token.last else float("nan"),
                "first": token.first,
                "last": token.last,
                "under_covered": token.coverage < report.min_expected_coverage,
            }
        )
    return pd.DataFrame(rows, columns=columns)


def quality_gaps_frame(report: QualityReport) -> pd.DataFrame:
    """Every gap in the report, longest first: which token, when, how long."""
    columns = ["symbol", "start", "end", "hours"]
    rows = [
        {"symbol": token.symbol, "start": gap.start, "end": gap.end, "hours": gap.hours}
        for token in report.tokens
        for gap in token.gaps
    ]
    frame = pd.DataFrame(rows, columns=columns)
    return frame.sort_values("hours", ascending=False).reset_index(drop=True) if not frame.empty else frame


def completeness_frame(completeness: dict[str, Any]) -> pd.DataFrame:
    """Field-level fill rates as fractions of ``rows_total``.

    Every ``*_present`` count the query returns is picked up by name, so a
    column added to the completeness query appears here without an edit.
    A low number is not automatically a fault: ``NULL`` means *not
    collected* (invariant 11), which is exactly what rows written before the
    enrichment migration are.
    """
    columns = ["field", "present", "fraction"]
    total = int(completeness.get("rows_total") or 0)
    if not total:
        return pd.DataFrame(columns=columns)
    rows = [
        {
            "field": name.removesuffix("_present"),
            "present": int(value or 0),
            "fraction": int(value or 0) / total,
        }
        for name, value in completeness.items()
        if name.endswith("_present")
    ]
    return pd.DataFrame(rows, columns=columns).sort_values("fraction").reset_index(drop=True)


def _absent(value: Any) -> bool:
    """``None`` or ``NaT``/``NaN``, as one question. Keeps pandas' sentinels local."""
    return value is None or bool(pd.isna(value))


def gap_lane_segments(gaps: pd.DataFrame, symbols: list[str]) -> tuple[list[Any], list[Any]]:
    """Gap runs as one broken line: ``(x, y)`` with ``None`` between segments.

    Plotly draws a single trace with breaks where a coordinate is ``None``,
    so every gap across every token becomes one trace rather than hundreds —
    which is the difference between a chart that renders and one that hangs.
    Only ``symbols`` are included, in the order given, so the lanes line up
    with the background ranges drawn beside them.
    """
    wanted = set(symbols)
    xs: list[Any] = []
    ys: list[Any] = []
    if gaps.empty:
        return xs, ys
    for symbol, start, end in zip(list(gaps["symbol"]), list(gaps["start"]), list(gaps["end"]), strict=True):
        if symbol not in wanted:
            continue
        # The run covers whole buckets: it starts at `start` and ends when the
        # last missing hour ends, which is one hour after that bucket opens.
        xs.extend([start, end + timedelta(hours=1), None])
        ys.extend([symbol, symbol, None])
    return xs, ys


def range_lane_segments(tokens: pd.DataFrame, symbols: list[str]) -> tuple[list[Any], list[Any]]:
    """Each token's stored range as one broken line: the gap map's background.

    Drawn under the gap segments so a short history reads as a short lane
    rather than as a lane full of holes — the two are different statements
    and the map has to keep them apart.
    """
    xs: list[Any] = []
    ys: list[Any] = []
    if tokens.empty:
        return xs, ys
    spans = {
        symbol: (first, last)
        for symbol, first, last in zip(
            list(tokens["symbol"]), list(tokens["first"]), list(tokens["last"]), strict=True
        )
    }
    for symbol in symbols:
        first, last = spans.get(symbol, (None, None))
        # A token with no rows has no range to draw; it is named in
        # `missing_tokens` instead, where it means something different.
        if _absent(first) or _absent(last):
            continue
        xs.extend([first, last, None])
        ys.extend([symbol, symbol, None])
    return xs, ys


@dataclass
class QualityView:
    """Is this history trustworthy, and where is it thin?

    The descriptive view of the stored data itself: coverage, where the holes
    are, which rows are impossible, and which fields were never collected.
    Read-only — nothing here fills a gap or writes a row.
    """

    report: QualityReport
    tokens: pd.DataFrame
    gaps: pd.DataFrame
    completeness: pd.DataFrame
    violations: pd.DataFrame
    generated_at: datetime
    fill_cache: pd.DataFrame = field(default_factory=pd.DataFrame)
    """One row per (symbol, method): how many hours are cached and how old."""

    @property
    def cached_hours(self) -> int:
        if self.fill_cache.empty:
            return 0
        return int(self.fill_cache["filled_hours"].astype("int64").sum())

    @property
    def fillable_hours(self) -> int:
        """Missing hours short enough to be worth estimating, per the cap."""
        if self.gaps.empty:
            return 0
        within = self.gaps[self.gaps["hours"] <= DEFAULT_MAX_FILL]
        return int(within["hours"].sum())

    @property
    def cache_is_stale(self) -> bool:
        """Whether a refresh would change anything a reader can see."""
        return self.cached_hours != self.fillable_hours

    @property
    def min_expected_coverage(self) -> float:
        return self.report.min_expected_coverage

    @property
    def under_covered(self) -> list[str]:
        """Tokens below the configured threshold — a prompt to look, not a fault."""
        return [token.symbol for token in self.report.under_covered]

    @property
    def missing_tokens(self) -> list[str]:
        """Configured tokens with no stored rows at all. Always worth a look."""
        return list(self.report.missing_tokens)

    @property
    def lane_symbols(self) -> list[str]:
        """Tokens for the gap map: thinnest coverage first, capped for legibility."""
        return list(self.tokens["symbol"].head(MAX_GAP_LANES)) if not self.tokens.empty else []

    @property
    def headline(self) -> dict[str, Any]:
        """The metric strip."""
        gaps = self.gaps
        worst = gaps.iloc[0] if not gaps.empty else None
        return {
            "tokens": len(self.tokens),
            "rows": self.report.total_rows,
            "under_covered": len(self.under_covered),
            "violations": len(self.violations),
            "gaps": len(gaps),
            "worst_gap_hours": int(worst["hours"]) if worst is not None else 0,
            "worst_gap_symbol": str(worst["symbol"]) if worst is not None else None,
            "ok": self.report.ok,
        }


def build_quality_view(
    db: DatabaseManager, settings: Settings, days: int | None = None, now: datetime | None = None
) -> QualityView:
    """The data-quality report, shaped for a screen.

    Calls the same :func:`quality.build_report` that ``candlegraph report``
    and the ``data_quality_report`` MCP tool call, so three surfaces cannot
    drift apart. It issues one query per token with missing hours, exactly as
    the CLI does, which is why this lives on its own dashboard rather than
    loading on every page.
    """
    moment = (now or datetime.now(tz=UTC)).astimezone(UTC)
    start_unix, _end_unix = window_bounds(days, now=moment)
    report = build_report(
        db,
        expected_symbols=get_token_registry().symbols,
        start_unix=start_unix,
        min_expected_coverage=settings.min_expected_coverage,
        now=moment,
    )
    return QualityView(
        report=report,
        tokens=quality_tokens_frame(report, moment),
        gaps=quality_gaps_frame(report),
        completeness=completeness_frame(report.completeness),
        violations=pd.DataFrame(report.violations),
        generated_at=report.generated_at,
        fill_cache=fill_cache_summary(db),
    )


def run_fill_refresh_now(db: DatabaseManager, symbols: list[str], method: str = LINEAR) -> FillSummary:
    """Recompute and store the display-only fills. The one write this page makes.

    Behind an explicit button, because Streamlit re-runs the whole script on
    every widget change and this writes rows. It touches no market data —
    only ``token_hours_fill`` — so coverage and every observer finding are
    unaffected by pressing it.
    """
    if not symbols:
        raise ViewError("No tokens with stored history to fill.")
    if method_is_causal(method):  # pragma: no cover - no causal method exists yet
        raise ViewError(f"{method!r} is causal and belongs in the strategy path, not this cache.")
    return refresh_fill_cache(db, symbols, method=method)


# --- risk dashboard ----------------------------------------------------------


@dataclass
class RiskView:
    """The risk screen: the full report plus what the charts need around it."""

    report: RiskReport
    returns: pd.Series
    """Per-bar simple returns, the histogram's raw material."""
    drawdown: pd.Series
    estimators: pd.DataFrame
    """VaR and CVaR under every estimator, same confidence and horizon."""


def build_risk_view(
    db: DatabaseManager,
    symbol: str,
    benchmark: str | None,
    days: int | None,
    config: RiskConfig,
) -> RiskView:
    """The risk report for one token, optionally split against a benchmark."""
    if benchmark == symbol:
        benchmark = None
    symbols = [symbol] + ([benchmark] if benchmark else [])
    # fill="none": the benchmark split regresses on hours both actually traded.
    frame = load_price_frame(db, symbols, days=days, fill="none")
    if frame.empty or symbol not in frame.columns or frame[symbol].dropna().empty:
        raise ViewError(f"No stored price history for {symbol} in that window.")

    prices = frame[symbol].dropna()
    prices.name = symbol
    benchmark_prices = None
    if benchmark and benchmark in frame.columns and not frame[benchmark].dropna().empty:
        benchmark_prices = frame[benchmark].dropna()
        benchmark_prices.name = benchmark

    try:
        report = risk_report(prices, config, benchmark_prices)
        returns = simple_returns(prices).dropna()
        estimators = pd.DataFrame(
            {
                method: {
                    "VaR": value_at_risk(returns, replace(config, method=method)),
                    "CVaR": conditional_value_at_risk(returns, replace(config, method=method)),
                }
                for method in METHODS
            }
        ).T
    except RiskError as exc:
        raise ViewError(str(exc)) from exc

    return RiskView(
        report=report,
        returns=returns,
        drawdown=drawdown_curve(equity_curve(returns)),
        estimators=estimators,
    )


# --- pair dashboard ----------------------------------------------------------


@dataclass
class PairView:
    """Two tokens side by side: ratio, correlation regime, beta cloud."""

    comparison: PairwiseComparison

    @property
    def legs_frame(self) -> pd.DataFrame:
        """Each token's own statistics, side by side over the aligned bars.

        ``candlegraph compare`` prints these; the screen showed only the
        joint numbers (correlation, beta, tracking error), which left a
        reader unable to see *which* leg carried the return or the risk.
        Both columns are computed over the same aligned bars, so they are
        comparable with each other — not with a full-history figure
        elsewhere in the app.
        """
        c = self.comparison
        # One row per token, so each statistic is a column and can carry its
        # own unit: a percentage and a ratio must not share a format string.
        return pd.DataFrame(
            [
                {
                    "token": c.symbol_a,
                    "total_return": c.total_return_a,
                    "volatility": c.volatility_a,
                    "sharpe": c.sharpe_a,
                },
                {
                    "token": c.symbol_b,
                    "total_return": c.total_return_b,
                    "volatility": c.volatility_b,
                    "sharpe": c.sharpe_b,
                },
            ],
            columns=["token", "total_return", "volatility", "sharpe"],
        )

    @property
    def scatter(self) -> pd.DataFrame:
        """Aligned hourly returns as (x, y) points for the beta cloud."""
        a, b = self.comparison.symbol_a, self.comparison.symbol_b
        frame = self.comparison.frame[[f"return_{b}", f"return_{a}"]].dropna()
        return frame.rename(columns={f"return_{b}": b, f"return_{a}": a})

    @property
    def beta_line(self) -> pd.DataFrame:
        """Two points of the fitted line ``y = beta * x`` across the cloud."""
        points = self.scatter
        if points.empty or not np.isfinite(self.comparison.beta):
            return pd.DataFrame(columns=["x", "y"])
        span = float(points.iloc[:, 0].abs().max())
        xs = [-span, span]
        return pd.DataFrame({"x": xs, "y": [self.comparison.beta * x for x in xs]})


def build_pair_view(
    db: DatabaseManager,
    symbol_a: str,
    symbol_b: str,
    days: int | None,
    window: int,
    interval_hours: int = 1,
    risk_free_rate: float = 0.0,
) -> PairView:
    """Compare two tokens over the hours both traded."""
    if symbol_a == symbol_b:
        raise ViewError("Pick two different tokens.")
    frame = load_price_frame(db, [symbol_a, symbol_b], days=days, fill="none")
    missing = [s for s in (symbol_a, symbol_b) if s not in frame.columns or frame[s].dropna().empty]
    if missing:
        raise ViewError(f"No stored price history for {', '.join(missing)} in that window.")

    try:
        comparison = compare_pair(
            frame[symbol_a].rename(symbol_a),
            frame[symbol_b].rename(symbol_b),
            window=window,
            interval_hours=interval_hours,
            risk_free_rate=risk_free_rate,
        )
    except ValueError as exc:
        raise ViewError(str(exc)) from exc
    return PairView(comparison=comparison)


# --- portfolio dashboard -----------------------------------------------------


@dataclass
class PortfolioView:
    """A solved allocation, its frontier, its risk budget, and its tail."""

    result: PortfolioResult
    frontier: list[FrontierPoint] = field(default_factory=list)
    bars: int = 0
    symbols: tuple[str, ...] = ()
    risk_contributions: pd.DataFrame = field(default_factory=pd.DataFrame)
    """Each held asset's share of portfolio volatility; sums to one."""
    var: float = float("nan")
    cvar: float = float("nan")

    @property
    def weights_frame(self) -> pd.DataFrame:
        """Non-zero weights, largest first, ready for a bar chart."""
        held = {k: v for k, v in self.result.weights.items() if abs(v) > 1e-6}
        frame = pd.DataFrame({"weight": pd.Series(held, dtype="float64")})
        return frame.sort_values("weight", ascending=False)

    @property
    def frontier_frame(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {"volatility": p.volatility, "expected_return": p.expected_return, "sharpe": p.sharpe}
                for p in self.frontier
            ]
        )


def _risk_budget(
    returns: pd.DataFrame, weights: dict[str, float], interval_hours: int, shrinkage: float
) -> pd.DataFrame:
    """Each held asset's share of portfolio volatility (Euler decomposition)."""
    held = {k: v for k, v in weights.items() if abs(v) > 1e-6 and k in returns.columns}
    if len(held) < 1:
        return pd.DataFrame()
    _, sigma = annualised_moments(returns[list(held)], interval_hours, shrinkage)
    w = np.array([held[name] for name in sigma.columns], dtype="float64")
    marginal = sigma.to_numpy() @ w
    total = float(np.sqrt(max(w @ marginal, 0.0)))
    if total <= 0:
        return pd.DataFrame()
    contributions = w * marginal / total
    frame = pd.DataFrame({"risk_share": contributions / total}, index=list(sigma.columns))
    return frame.sort_values("risk_share", ascending=False)


def build_portfolio_view(
    db: DatabaseManager,
    symbols: list[str],
    days: int | None,
    config: PortfolioConfig,
    frontier_points: int = 0,
    risk: RiskConfig | None = None,
) -> PortfolioView:
    """Optimise across the chosen tokens; trace the frontier and the tail."""
    if len(symbols) < 2:
        raise ViewError("A portfolio needs at least two tokens.")

    prices = load_prices(db, symbols, days)
    returns = prices.apply(log_returns).dropna(how="all")

    try:
        result = optimise_portfolio(returns, config)
        frontier = efficient_frontier(returns, config, points=frontier_points) if frontier_points >= 2 else []
        budget = _risk_budget(returns, result.weights, config.interval_hours, config.shrinkage)
    except PortfolioError as exc:
        raise ViewError(str(exc)) from exc

    var = cvar = float("nan")
    if risk is not None:
        held = {k: v for k, v in result.weights.items() if abs(v) > 1e-6}
        try:
            series = portfolio_returns(returns, held)
            var = value_at_risk(series, risk)
            cvar = conditional_value_at_risk(series, risk)
        except RiskError:
            pass  # too little history for a tail; the metrics show NaN

    return PortfolioView(
        result=result,
        frontier=frontier,
        bars=len(returns.dropna(how="any")),
        symbols=tuple(prices.columns),
        risk_contributions=budget,
        var=var,
        cvar=cvar,
    )


# --- strategy dashboard ------------------------------------------------------


@dataclass
class StrategyView:
    """A strategy run and its backtest, kept together for one screen."""

    strategy: StrategyResult
    report: BacktestResult

    @property
    def equity_frame(self) -> pd.DataFrame:
        """Strategy equity against buy-and-hold, both rebased to the same start."""
        frame = self.report.frame
        capital = self.report.config.initial_capital
        hold = capital * (1.0 + frame["returns"].fillna(0.0)).cumprod()
        return pd.DataFrame({"strategy": frame["equity"], "buy and hold": hold})

    @property
    def trades_frame(self) -> pd.DataFrame:
        """One row per position change: when, from what, to what, at what price."""
        frame = self.report.frame
        signals = frame["signal"].astype("float64")
        changed = signals.diff().fillna(signals.abs()) != 0
        trades = frame.loc[changed, ["price", "signal"]].copy()
        trades["from"] = signals.shift(1).fillna(0.0).astype("int64").loc[changed]
        trades = trades.rename(columns={"signal": "to"})
        trades["to"] = trades["to"].astype("int64")
        return trades[["from", "to", "price"]]


def build_strategy_view(
    db: DatabaseManager,
    symbol: str,
    strategy: str,
    days: int | None,
    strategy_config: Any = None,
    backtest_config: BacktestConfig | None = None,
) -> StrategyView:
    """Run a strategy over stored prices and backtest it in one step."""
    prices = _one_symbol(db, symbol, days)
    result = run_strategy(strategy, prices, strategy_config)
    return StrategyView(strategy=result, report=backtest(result, config=backtest_config))


# --- explanations (signals --explain in the UI) --------------------------------


@dataclass
class ExplanationView:
    """One explained signal, shaped for the Strategy dashboard."""

    explanation: Explanation

    @property
    def citations_frame(self) -> pd.DataFrame:
        """One row per citation in the order the prose numbers them."""
        columns = ["n", "published", "title", "url", "legs", "quote"]
        rows = [
            {
                "n": c.n,
                "published": c.published_at.strftime("%Y-%m-%d %H:%M UTC")
                if c.published_at
                else "date unknown",
                "title": c.title or "",
                "url": c.url,
                "legs": c.legs,
                "quote": c.quote,
            }
            for c in self.explanation.citations
        ]
        return pd.DataFrame(rows, columns=columns)

    @property
    def headline(self) -> dict[str, Any]:
        """The metric strip: what the signal said and how far the price moved."""
        context = self.explanation.numeric_context
        reference = context.get("reference") or {}
        return {
            "bar": self.explanation.bar,
            "position": self.explanation.position_label,
            "score": self.explanation.score,
            "move_24h": context.get("move_24h"),
            "reference_symbol": reference.get("symbol"),
            "reference_move_24h": reference.get("move_24h"),
            "citations": len(self.explanation.citations),
        }

    @property
    def status_line(self) -> str:
        """Where the documents came from and which retrieval legs ran — always shown."""
        e = self.explanation
        legs = "hybrid retrieval" if e.dense_used else "lexical retrieval only"
        line = f"news: {e.news_status} — {e.news_message}; {legs}"
        if e.retrieval_note:
            line += f" ({e.retrieval_note})"
        if e.chunks_embedded:
            line += f"; embedded {e.chunks_embedded} new chunks"
        return line

    @property
    def validator_note(self) -> str | None:
        """What the validator removed, or None when the prose was clean."""
        e = self.explanation
        if not e.rejected_citations:
            return None
        return (
            f"The narrator cited {list(e.rejected_citations)}, which retrieval never returned; "
            f"{len(e.stripped_sentences)} sentence(s) were removed."
        )


def load_stored_explanation(db: DatabaseManager, strategy: str, symbol: str) -> ExplanationView | None:
    """The latest stored explanation for the pair. Free: no network, no model."""
    found = load_explanation(db, strategy, symbol)
    return ExplanationView(found) if found else None


def run_explain_now(
    settings: Settings,
    db: DatabaseManager,
    result: StrategyResult,
    symbol: str,
    *,
    search: bool,
    at: datetime | None = None,
) -> ExplanationView:
    """Explain one bar of a strategy already run on this screen, and store it.

    ``search`` is the one way a credit gets spent from the UI, and it is a
    checkbox the user ticked; without it the stored corpus serves. The
    reference series is ``KNOWLEDGE_REFERENCE_SYMBOL`` when it is a different
    token with stored history.

    Raises:
        ViewError: an untracked symbol or no bar at ``at``.
    """
    registry = get_token_registry()
    token = registry.by_symbol(symbol)
    if token is None:
        raise ViewError(f"unknown token {symbol!r}")
    reference = None
    reference_symbol = settings.knowledge_reference_symbol
    if reference_symbol.casefold() != symbol.casefold() and registry.by_symbol(reference_symbol) is not None:
        try:
            reference = _one_symbol(db, reference_symbol, None)
        except ViewError:
            reference = None  # no stored history for the reference: explain without the comparison
    source, embedder, _notes = services_for(settings)
    if not search:
        source = None
    try:
        request = request_for(result, token, at=at, reference=reference)
        return ExplanationView(explain(db, settings, request, source=source, embedder=embedder))
    except (ExplainError, RetrievalError) as exc:
        raise ViewError(str(exc)) from exc


# --- forecast dashboard ------------------------------------------------------


def build_forecast(
    db: DatabaseManager, symbol: str, days: int | None, config: ArimaConfig
) -> tuple[pd.Series, ArimaForecast]:
    """Price history and an ARIMA forecast over it."""
    prices = _one_symbol(db, symbol, days)
    return prices, fit_forecast(prices, config)


def forecast_frame(prices: pd.Series, forecast: ArimaForecast) -> pd.DataFrame:
    """The forecast path on a future hourly index, for plotting past the data."""
    if not forecast.ok or not forecast.path:
        return pd.DataFrame(columns=["forecast"])

    last = pd.DatetimeIndex(prices.index)[-1]
    index = pd.date_range(last + pd.Timedelta(hours=1), periods=len(forecast.path), freq="h")
    return pd.DataFrame({"forecast": list(forecast.path)}, index=index)


# --- agents dashboard --------------------------------------------------------


@dataclass
class AgentsView:
    """The watchtower: agent reports, ingest cadence, and coverage."""

    agent_runs: pd.DataFrame
    """Newest first: finished_at, agent, status, findings_total, duration, summary."""
    latest: dict[str, Any] | None
    """The newest observer run's raw ledger row, or None before the first run."""
    findings: pd.DataFrame
    """The latest observer report's findings: severity, kind, subject, message."""
    ingest_runs: pd.DataFrame
    """Newest first: started_at, status, token and pool rows written, duration."""
    coverage: pd.DataFrame
    """Per token: stored hours, expected hours, and their ratio."""
    news_calls: pd.DataFrame = field(default_factory=pd.DataFrame)
    """Recent Tavily calls, newest first — the minimal news-health strip here;
    the News dashboard carries the full analysis."""
    stages: pd.DataFrame = field(default_factory=pd.DataFrame)
    """The latest observer run's stages in order: stage, status, seconds, detail.
    Empty before the first run, and for runs recorded before stages existed."""

    @property
    def status(self) -> str:
        """The latest observer verdict: ``ok``/``info``/``warning``/``critical``/``error``."""
        return str(self.latest["status"]) if self.latest else "none"

    @property
    def news_status(self) -> str:
        """The newest news call's outcome, or ``none`` before the first fetch."""
        return "none" if self.news_calls.empty else str(self.news_calls.iloc[0]["status"])

    @property
    def news_counts(self) -> dict[str, int]:
        """Recent call outcomes: the "were the news runs successful" numbers."""
        if self.news_calls.empty:
            return {}
        counts = self.news_calls["status"].value_counts()
        return {str(k): int(v) for k, v in counts.items()}


def stages_frame(detail: Any) -> pd.DataFrame:
    """The ``stages`` list of an ``agent_run.detail`` as a frame, order kept."""
    columns = ["stage", "status", "seconds", "detail"]
    raw = detail.get("stages") if isinstance(detail, dict) else None
    if not raw:
        return pd.DataFrame(columns=columns)
    records = [StageRecord.from_dict(item) for item in raw]
    return pd.DataFrame(
        {
            "stage": [r.name for r in records],
            "status": [r.status for r in records],
            "seconds": [r.duration_seconds for r in records],
            "detail": [", ".join(f"{k}={v}" for k, v in r.detail.items()) for r in records],
        }
    )


def _findings_frame(rows: Any) -> pd.DataFrame:
    columns = ["severity", "kind", "subject", "message"]
    if not rows:
        return pd.DataFrame(columns=columns)
    frame = pd.DataFrame(list(rows))
    present = [c for c in columns if c in frame.columns]
    return frame[present]


def build_agents_view(db: DatabaseManager, limit: int = 30) -> AgentsView:
    """Everything the agents dashboard shows, from the two ledgers."""
    agent_rows = db.fetch_all(db.sql.get_recent_agent_runs, {"limit": limit, "agent": None})
    observer_rows = [row for row in agent_rows if row.get("agent") == "observer"]
    latest = observer_rows[0] if observer_rows else None

    agent_columns = ["finished_at", "agent", "status", "findings_total", "duration_seconds", "summary"]
    agent_runs = pd.DataFrame(agent_rows)
    if not agent_runs.empty:
        agent_runs = agent_runs[[c for c in agent_columns if c in agent_runs.columns]]

    ingest_rows = db.fetch_all(db.sql.get_recent_runs, {"limit": limit})
    ingest_columns = [
        "started_at",
        "trigger",
        "status",
        "token_hours_written",
        "pool_hours_written",
        "duration_seconds",
    ]
    ingest_runs = pd.DataFrame(ingest_rows)
    if not ingest_runs.empty:
        ingest_runs = ingest_runs[[c for c in ingest_columns if c in ingest_runs.columns]]

    news_columns = ["called_at", "status", "credits", "results", "query"]
    news_calls = pd.DataFrame(
        db.fetch_all(
            knowledge_statements_for(db.schema).get_recent_source_calls,
            {"source_id": "tavily", "limit": limit},
        )
    )
    if not news_calls.empty:
        news_calls = news_calls[[c for c in news_columns if c in news_calls.columns]]

    coverage_rows = db.fetch_all(db.sql.coverage_report, {"start_unix": None})
    coverage = pd.DataFrame(coverage_rows)
    if not coverage.empty:
        coverage = coverage[["symbol", "stored_hours", "expected_hours"]].set_index("symbol")
        coverage["coverage"] = coverage["stored_hours"].astype("float64") / coverage["expected_hours"].astype(
            "float64"
        )
        coverage = coverage.sort_values("coverage")

    return AgentsView(
        agent_runs=agent_runs,
        latest=latest,
        findings=_findings_frame(latest.get("findings") if latest else None),
        ingest_runs=ingest_runs,
        coverage=coverage,
        news_calls=news_calls,
        stages=stages_frame(latest.get("detail") if latest else None),
    )


# --- news (Tavily) dashboard ---------------------------------------------------


@dataclass
class NewsView:
    """The metered news source: budget, ledger, and what the credits bought."""

    source_id: str
    monthly_cap: int
    daily_cap: int
    reserve: int
    spent_month: int
    spent_today: int
    calls: pd.DataFrame
    """Newest first: called_at, status, credits, results, query, error."""
    daily: pd.DataFrame
    """One row per UTC day: calls, credits, results, ok/errors/rate_limited/refused."""
    documents: pd.DataFrame
    """Newest stored news: retrieved_at, published_at, title, url."""
    corpus: dict[str, Any]
    """documents, chunks, embedded, undated, last_retrieved_at for this source."""
    embedding_pending: int
    """Chunks still waiting for the configured model's vector."""

    @property
    def monthly_usage(self) -> float:
        return usage_fraction(self.spent_month, self.monthly_cap)

    @property
    def daily_usage(self) -> float:
        return usage_fraction(self.spent_today, self.daily_cap)

    @property
    def level(self) -> Level:
        """The observer's grading of monthly usage: ok / warning at 80% / critical at 100%."""
        return usage_level(self.monthly_usage)

    @property
    def last_call(self) -> dict[str, Any] | None:
        if self.calls.empty:
            return None
        return {str(k): v for k, v in self.calls.iloc[0].to_dict().items()}

    @property
    def outcome_counts(self) -> dict[str, int]:
        """How the recent calls ended, for the status strip."""
        if self.calls.empty:
            return {}
        counts = self.calls["status"].value_counts()
        return {str(k): int(v) for k, v in counts.items()}


def _one(rows: list[dict[str, Any]], default: dict[str, Any]) -> dict[str, Any]:
    return rows[0] if rows else default


def build_news_view(
    db: DatabaseManager, settings: Settings, now: datetime | None = None, limit: int = 30
) -> NewsView:
    """Everything the News dashboard shows, from the ledger and the corpus."""
    statements = knowledge_statements_for(db.schema)
    moment = (now or datetime.now(tz=UTC)).astimezone(UTC)
    budget = budget_from(settings)
    month, today = source_spend(db, TAVILY, moment)

    call_columns = ["called_at", "status", "credits", "results", "query", "error"]
    calls = pd.DataFrame(
        db.fetch_all(statements.get_recent_source_calls, {"source_id": TAVILY, "limit": limit})
    )
    if not calls.empty:
        calls = calls[[c for c in call_columns if c in calls.columns]]

    daily = pd.DataFrame(
        db.fetch_all(
            statements.source_call_daily_stats,
            {"source_id": TAVILY, "since": moment - timedelta(days=30)},
        )
    )

    documents = pd.DataFrame(
        db.fetch_all(statements.get_recent_source_documents, {"source_id": TAVILY, "limit": limit})
    )
    if not documents.empty:
        documents = documents[["retrieved_at", "published_at", "title", "url"]]

    corpus = _one(
        db.fetch_all(statements.count_source_documents, {"source_id": TAVILY}),
        {"documents": 0, "chunks": 0, "embedded": 0, "undated": 0, "last_retrieved_at": None},
    )
    backlog = _one(
        db.fetch_all(statements.count_embedding_backlog, {"model": settings.embedding_model}),
        {"missing": 0, "stale": 0, "total": 0},
    )

    return NewsView(
        source_id=TAVILY,
        monthly_cap=budget.monthly_credits,
        daily_cap=budget.daily_credits,
        reserve=budget.reserve,
        spent_month=month,
        spent_today=today,
        calls=calls,
        daily=daily,
        documents=documents,
        corpus=dict(corpus),
        embedding_pending=int(backlog["missing"]) + int(backlog["stale"]),
    )


def run_news_fetch_now(
    settings: Settings, db: DatabaseManager, symbols: list[str] | None = None
) -> NewsFetchSummary:
    """One on-demand news fetch, phrased for the screen when it cannot run.

    Every call it makes — including budget refusals — is a ``source_call``
    row, so the dashboard above reflects it immediately.
    """
    try:
        return run_news_fetch(db, settings, symbols=symbols)
    except NewsError as exc:
        raise ViewError(str(exc)) from exc


def run_observer_now(
    settings: Settings, db: DatabaseManager, on_stage: StageHook | None = None
) -> AgentReport:
    """One on-demand observer run, written to the agent ledger.

    The ledger sink is used regardless of ``AGENT_REPORT_SINKS`` so the run
    always lands in the history this dashboard reads; the report is also
    returned for immediate rendering. Never raises for a failing graph —
    a crashed run comes back with ``error`` set, exactly like a scheduled one.
    ``on_stage`` lets the page show each stage as it finishes.
    """
    sinks = build_sinks(settings, db=db, names=("ledger",))
    return run_observer(settings, db=db, sinks=sinks, on_stage=on_stage)


# --- data dashboard ------------------------------------------------------------


def run_backfill(settings: Settings, db: DatabaseManager, days: int) -> IngestResult:
    """One backfill over ``days`` of history, recorded in the ingest ledger.

    The same code path as ``candlegraph backfill --days N``: it rewinds past
    the stored history (invariant 13), upserts on the natural key so nothing
    is duplicated, and never deletes. The caller is responsible for having
    asked the user first — a backfill is minutes of network and thousands of
    writes, which is why the dashboard confirms before calling this.
    """
    if days < 1:
        raise ViewError("A backfill needs at least one day.")
    if settings.graph_api_key is None:
        raise ViewError("GRAPH_API_KEY is not set in this process; the UI cannot reach the subgraph.")
    return run_ingest(db, get_token_registry(), settings, trigger="backfill", lookback_days=days)
