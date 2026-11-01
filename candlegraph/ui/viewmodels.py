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
from candlegraph.analytics.prices import load_price_frame, load_price_history, to_wide, window_bounds
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
from candlegraph.ingestion.runner import IngestResult, run_ingest
from candlegraph.knowledge.budget import Level, usage_fraction, usage_level
from candlegraph.knowledge.news import NewsError, NewsFetchSummary, budget_from, run_news_fetch, source_spend
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

    @property
    def bars(self) -> int:
        return len(self.prices)

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


def run_observer_now(settings: Settings, db: DatabaseManager) -> AgentReport:
    """One on-demand observer run, written to the agent ledger.

    The ledger sink is used regardless of ``AGENT_REPORT_SINKS`` so the run
    always lands in the history this dashboard reads; the report is also
    returned for immediate rendering. Never raises for a failing graph —
    a crashed run comes back with ``error`` set, exactly like a scheduled one.
    """
    sinks = build_sinks(settings, db=db, names=("ledger",))
    return run_observer(settings, db=db, sinks=sinks)


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
