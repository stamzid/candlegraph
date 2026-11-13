"""The tools the MCP server offers, as plain Python functions.

Every one is a thin wrapper around something the CLI already calls — the
server adds no capability of its own (``docs/knowledge/mcp/DESIGN.md``: *"If
a function exists only as an MCP tool and not as a CLI subcommand, that is a
smell"*). Keeping them here, free of any ``mcp`` import, buys three things:
the unit tier tests them directly against ``FakeDatabase`` with no protocol
in the way, the optional extra is only needed to *serve* them, and
:mod:`candlegraph.mcp.server` stays a registration list.

The signature and the docstring of each function **are** its MCP schema, so
both are written for a model reading them cold: no ``db`` or ``settings``
parameter (they are resolved from the process, as the Streamlit script does
it), explicit units, and a note where a number is easy to misread.

Three rules the whole module obeys:

- **No tool writes market data** (invariant 21's spirit). The only writes
  anywhere below are the ones the wrapped functions already make: the
  ``agent_run`` row ``observe`` produces, and the ``explanation`` /
  ``document`` / ``source_call`` rows ``explain_signal`` produces.
- **Only one tool can spend money**, ``explain_signal``, only when asked,
  and the budget guard is inside the function it calls — a client looping
  on it still spends at most the daily cap.
- **Everything is redacted** on the way out by :func:`boundary`, and so is
  the message of any error that escapes.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from candlegraph.agents.shell import redact
from candlegraph.analytics.pairwise import compare_pair
from candlegraph.analytics.prices import PriceError, load_price_frame, load_symbol_series
from candlegraph.analytics.rolling import rolling_summary
from candlegraph.api.formatting import format_candle_rows
from candlegraph.api.schema import fetch_chart_data, fetch_token_metadata
from candlegraph.api.status import status_body
from candlegraph.ingestion.ledger import recent_runs
from candlegraph.ingestion.quality import build_report, report_dict
from candlegraph.ingestion.runner import lookback_start
from candlegraph.mcp import payloads
from candlegraph.settings import Settings, get_settings
from candlegraph.storage import DatabaseManager, get_db
from candlegraph.tokens import TokenRegistry, get_token_registry

MAX_LIMIT = 500
# Buckets a `candles` call returns at most. Hourly data over a year is 8,760
# rows; a tool result that large is not something a client can reason over.
MAX_CANDLES = 2000


class ToolError(RuntimeError):
    """A tool could not do what was asked, phrased for whoever called it."""


def boundary[F: Callable[..., dict[str, Any]]](fn: F) -> F:
    """Redact a tool's result, and any error it raises, on the way out.

    The checks and the database both produce text that can quote a
    credential — a DSN in a psycopg2 message is the realistic case — and a
    tool result crosses a process boundary exactly as an agent report does.
    ``functools.wraps`` keeps ``__wrapped__`` pointing at the original, so
    :func:`inspect.signature` still sees the real parameters and the
    generated JSON schema is unaffected.
    """

    @functools.wraps(fn)
    def guarded(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            result = fn(*args, **kwargs)
        except (PriceError, LookupError, ValueError) as exc:
            # Known, explainable refusals: an unknown symbol, an unknown
            # strategy, a window with no bars. Re-raised as one type so the
            # server renders them the same way.
            raise ToolError(redact(str(exc).strip("'\""))) from exc
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(redact(f"{type(exc).__name__}: {exc}")) from exc
        return dict(payloads.redact_payload(result))

    return guarded  # type: ignore[return-value]


def _db() -> DatabaseManager:
    return get_db()


def _settings() -> Settings:
    return get_settings()


def _registry() -> TokenRegistry:
    return get_token_registry()


def _limit(value: int, cap: int = MAX_LIMIT) -> int:
    if value < 1 or value > cap:
        raise ValueError(f"limit must be between 1 and {cap} (got {value})")
    return value


def _moment(value: str | None, field: str) -> datetime | None:
    """Parse an ISO 8601 argument into an aware UTC datetime."""
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except ValueError as exc:
        raise ValueError(f"`{field}` must be ISO 8601, e.g. 2026-11-04T14:00:00Z (got {value!r})") from exc


# --- operating the stack -----------------------------------------------------


@boundary
def status() -> dict[str, Any]:
    """Is the service up, is the database reachable, and is the data fresh?

    The same body ``GET /status`` returns. ``data_fresh`` is false when the
    last successful ingest is older than three poll intervals; that is
    reported, never fatal — the API serving stale data is still the API
    working.

    One field means less here than it does over HTTP: ``ingest`` describes
    the process answering, and this server never ingests. Whether data is
    actually arriving is ``last_ingest_seconds_ago``, which comes from the
    ledger and is the same for every reader.
    """
    body, _up = status_body(_db(), _settings())
    return body


@boundary
def recent_ingest_runs(limit: int = 10) -> dict[str, Any]:
    """The newest rows of the ingest ledger, newest first.

    One row per ingest pass, with its trigger (``scheduled`` / ``manual`` /
    ``backfill``), its status (``succeeded`` / ``partial`` / ``failed``) and
    how many hourly rows it wrote. ``partial`` means some tokens succeeded
    and some did not. This is what answers "did last night's run fail?"
    without reading a log.

    Args:
        limit: How many runs to return, 1 to 500.
    """
    rows = recent_runs(_db(), _limit(limit))
    return {
        "count": len(rows),
        "runs": payloads.rows_to_dicts(
            rows,
            [
                "run_id",
                "trigger",
                "status",
                "started_at",
                "finished_at",
                "duration_seconds",
                "token_hours_written",
                "pool_hours_written",
                "errors",
            ],
        ),
    }


@boundary
def data_quality_report(days: int | None = None) -> dict[str, Any]:
    """Coverage, gaps and impossible rows per tracked token.

    The structure ``candlegraph report --json`` prints, field for field.

    **A gap is not automatically a fault.** Uniswap v3 emits no row for an
    hour with no swaps, so a thin token is legitimately sparse; that is why
    ``under_covered`` is judged against ``MIN_EXPECTED_COVERAGE`` rather
    than by the presence of a gap. ``violations`` counts rows that are
    impossible whatever the market did (``high < low``, OHLC outside the
    bar) and is the only unambiguous fault here.

    Args:
        days: Only consider the last N days. Default: all stored history.
    """
    settings = _settings()
    report = build_report(
        _db(),
        expected_symbols=_registry().symbols,
        start_unix=lookback_start(days) if days else None,
        min_expected_coverage=settings.min_expected_coverage,
    )
    return report_dict(report)


# --- market data -------------------------------------------------------------


@boundary
def candles(symbol: str, hours: int = 1, start: str | None = None, end: str | None = None) -> dict[str, Any]:
    """OHLC candles of any width for one tracked token.

    Buckets are aligned to the Unix epoch, so ``hours=24`` buckets on
    midnight UTC regardless of which hours are present, and **a bucket with
    no stored rows is absent rather than zero-filled**.

    Args:
        symbol: A tracked token symbol, case-insensitive.
        hours: Bucket width in hours. 1 is the stored grain.
        start: Earliest bar to include, ISO 8601 UTC. Default: all history.
        end: Latest bar to include, ISO 8601 UTC. Default: now.
    """
    if hours < 1:
        raise ValueError(f"hours must be at least 1 (got {hours})")
    registry = _registry()
    token = registry.by_symbol(symbol)
    if token is None:
        raise ValueError(f"unknown token {symbol!r}; tracked: {', '.join(registry.symbols)}")

    from_unix = _moment(start, "start")
    to_unix = _moment(end, "end")
    db = _db()
    rows = fetch_chart_data(
        token.symbol,
        hours,
        db=db,
        registry=registry,
        start_unix=int(from_unix.timestamp()) if from_unix else None,
        end_unix=int(to_unix.timestamp()) if to_unix else None,
    )
    bars = format_candle_rows(rows)
    truncated = len(bars) > MAX_CANDLES
    return {
        "symbol": token.symbol,
        "interval_hours": hours,
        "count": min(len(bars), MAX_CANDLES),
        "truncated": truncated,
        "metadata": payloads.rows_to_dicts([fetch_token_metadata(token.symbol, db=db, registry=registry)])[0],
        # The newest bars are the ones worth keeping when there are too many.
        "candles": bars[-MAX_CANDLES:] if truncated else bars,
    }


@boundary
def stats(symbol: str, window: int | None = None, days: int | None = None) -> dict[str, Any]:
    """Rolling mean, per-bar return and annualised volatility for one token.

    The window is complete or absent: with fewer than ``window`` bars there
    is no value, and the result says so instead of estimating one from
    fewer.

    Args:
        symbol: A tracked token symbol, case-insensitive.
        window: Rolling lookback in bars. Default: ``ROLLING_WINDOW`` (24).
        days: Trailing history to load. Default: ``ANALYTICS_LOOKBACK_DAYS``.
    """
    settings = _settings()
    bars = window or settings.rolling_window
    prices = load_symbol_series(
        _db(), _registry(), symbol, days=days if days is not None else settings.analytics_lookback_days
    )
    summary = rolling_summary(prices, window=bars, interval_hours=settings.analytics_interval_hours)
    return payloads.stats_payload(str(prices.name), bars, summary)


@boundary
def compare(
    symbol_a: str, symbol_b: str, window: int | None = None, days: int | None = None
) -> dict[str, Any]:
    """Correlation, beta and relative performance between two tracked tokens.

    Computed **only over hours both tokens actually traded**: nothing is
    forward-filled first, because correlating a real price against a carried
    one measures the fill policy rather than the market. ``aligned_bars``
    says how many hours survived.

    Args:
        symbol_a: The first token, case-insensitive.
        symbol_b: The second token; must differ from the first.
        window: Rolling-correlation lookback in bars. Default: ``ROLLING_WINDOW``.
        days: Trailing history to load. Default: ``ANALYTICS_LOOKBACK_DAYS``.
    """
    settings = _settings()
    registry = _registry()
    tokens = []
    for symbol in (symbol_a, symbol_b):
        token = registry.by_symbol(symbol)
        if token is None:
            raise ValueError(f"unknown token {symbol!r}; tracked: {', '.join(registry.symbols)}")
        tokens.append(token.symbol)
    if tokens[0] == tokens[1]:
        raise ValueError("compare needs two different tokens")

    period = days if days is not None else settings.analytics_lookback_days
    # fill="none": the comparison aligns on hours both tokens traded.
    frame = load_price_frame(_db(), tokens, days=period, fill="none")
    missing = [s for s in tokens if s not in frame.columns or frame[s].dropna().empty]
    if missing:
        raise ValueError(f"no stored price history for {', '.join(missing)} in the last {period} days")

    comparison = compare_pair(
        frame[tokens[0]].rename(tokens[0]),
        frame[tokens[1]].rename(tokens[1]),
        window=window or settings.rolling_window,
        interval_hours=settings.analytics_interval_hours,
        risk_free_rate=settings.risk_free_rate,
    )
    return payloads.comparison_payload(comparison)


# --- strategies ---------------------------------------------------------------


@boundary
def signals(
    symbol: str, strategy: str = "bollinger", days: int | None = None, tail: int = 20
) -> dict[str, Any]:
    """Run a strategy over stored history and return its target positions.

    Reads only: nothing is written to the ``signal`` table (``candlegraph
    signals --store`` is what does that). ``latest_position`` is -1 short,
    0 flat, 1 long. Registered strategies: ``bollinger``, ``crossover``,
    ``arima``.

    Args:
        symbol: A tracked token symbol, case-insensitive.
        strategy: A registered strategy name; its default configuration is used.
        days: Trailing history to run over. Default: ``ANALYTICS_LOOKBACK_DAYS``.
        tail: How many of the most recent bars to include, 1 to 500.
    """
    from candlegraph.strategies import run_strategy

    settings = _settings()
    prices = load_symbol_series(
        _db(), _registry(), symbol, days=days if days is not None else settings.analytics_lookback_days
    )
    return payloads.strategy_payload(run_strategy(strategy, prices, None), tail=_limit(tail))


@boundary
def backtest(
    symbol: str,
    strategy: str = "bollinger",
    days: int | None = None,
    fee_bps: float | None = None,
    slippage_bps: float | None = None,
) -> dict[str, Any]:
    """Backtest a strategy over stored history, net of fees and slippage.

    A position decided on bar *t* earns bar *t+1*'s return, and every
    position change pays the costs. **Read ``excess_return`` before
    ``total_return``**: on hourly data a naive strategy at these costs often
    pays more than its edge, and a losing backtest is usually the honest
    answer rather than something to tune away.

    Args:
        symbol: A tracked token symbol, case-insensitive.
        strategy: A registered strategy name, or ``all`` for every one of them.
        days: Trailing history to run over. Default: ``ANALYTICS_LOOKBACK_DAYS``.
        fee_bps: Per-side fee in basis points. Default: ``BACKTEST_FEE_BPS`` (30).
        slippage_bps: Per-side slippage in basis points. Default: ``BACKTEST_SLIPPAGE_BPS`` (5).
    """
    from candlegraph.strategies import available, run_strategy
    from candlegraph.strategies import backtest as run_backtest
    from candlegraph.strategies.backtest import BacktestConfig

    settings = _settings()
    prices = load_symbol_series(
        _db(), _registry(), symbol, days=days if days is not None else settings.analytics_lookback_days
    )
    config = BacktestConfig(
        fee_bps=fee_bps if fee_bps is not None else settings.backtest_fee_bps,
        slippage_bps=slippage_bps if slippage_bps is not None else settings.backtest_slippage_bps,
        risk_free_rate=settings.risk_free_rate,
        interval_hours=settings.analytics_interval_hours,
    )
    names = list(available()) if strategy == "all" else [strategy]
    results = [
        payloads.backtest_payload(run_backtest(run_strategy(name, prices, None), config=config))
        for name in names
    ]
    return {"symbol": str(prices.name), "bars": len(prices), "backtests": results}


# --- the agents ----------------------------------------------------------------


@boundary
def agent_runs(agent: str | None = None, limit: int = 10) -> dict[str, Any]:
    """The newest rows of the agent ledger: what the graphs reported, and when.

    Summary rows only; ``latest_agent_report`` returns one in full.

    Args:
        agent: Only this agent (``observer`` or ``janitor``). Default: both.
        limit: How many runs to return, 1 to 500.
    """
    from candlegraph.agents.ledger import recent_agent_runs

    rows = recent_agent_runs(_db(), _limit(limit), agent)
    return {
        "count": len(rows),
        "runs": payloads.rows_to_dicts(
            rows,
            [
                "run_id",
                "agent",
                "trigger",
                "outcome",
                "status",
                "started_at",
                "finished_at",
                "duration_seconds",
                "findings_total",
                "summary",
            ],
        ),
    }


@boundary
def latest_agent_report(agent: str = "observer") -> dict[str, Any]:
    """The newest report from one agent, with its findings and Markdown.

    Reads the ledger; it does not run anything. ``observe`` is the tool that
    runs the observer.

    Args:
        agent: ``observer`` or ``janitor``.
    """
    from candlegraph.agents.ledger import recent_agent_runs

    rows = recent_agent_runs(_db(), 1, agent)
    if not rows:
        return {"agent": agent, "found": False, "note": f"no {agent} run has been recorded yet"}
    row = rows[0]
    return {
        "agent": agent,
        "found": True,
        "run": payloads.rows_to_dicts([row])[0],
        "findings": payloads.redact_payload(row.get("findings") or []),
        "markdown": str(row.get("report") or ""),
    }


@boundary
def observe() -> dict[str, Any]:
    """Run the run-observer agent once and return its report.

    **Writes one ``agent_run`` ledger row** — that is what an agent run is —
    and posts the report to the configured sinks. It writes no market data
    and changes nothing else.

    It answers three questions: is the data fresh, is it complete, and does
    it still look like the same market. Remember its own rules when reading
    the findings: a gap is a finding only on a liquid token, drift is a
    WARNING for a human rather than an alarm, and "not enough bars" is INFO
    and never a guess.
    """
    from candlegraph.agents.reporting import build_sinks
    from candlegraph.harness import JOBS, run_job
    from candlegraph.harness.runs import MCP

    db = _db()
    settings = _settings()
    run = run_job(
        JOBS["observer"], settings, trigger=MCP, db=db, params={"sinks": build_sinks(settings, db=db)}
    )
    if run.payload is None:
        raise RuntimeError(f"the observer run failed: {dict(run.result.errors) if run.result else {}}")
    return payloads.report_payload(run.payload, markdown=True)


# --- the knowledge layer ---------------------------------------------------------


@boundary
def search_knowledge(
    query: str,
    symbol: str | None = None,
    before: str | None = None,
    top_k: int | None = None,
    mode: str = "display",
) -> dict[str, Any]:
    """Hybrid retrieval over the stored news corpus, as of a moment in time.

    A dense (pgvector) leg and a lexical (``tsvector``) leg fused by
    reciprocal rank. **Never calls the web**: this searches what has already
    been fetched, so it costs nothing. ``explain_signal`` is the tool that
    can fetch.

    Nothing published after ``before`` is returned, ever. In ``strategy``
    mode nothing *retrieved* after it is returned either, and undated
    documents are excluded — that is what a backtest may read.

    The ``symbol`` filter joins alias mentions, so a document that was never
    linked is invisible to it (``make link-mentions`` backfills the links).

    Args:
        query: What to look for, in words.
        symbol: Restrict to chunks mentioning this tracked token.
        before: The bar, ISO 8601 UTC. Default: now.
        top_k: Results to return. Default: ``RETRIEVAL_TOP_K`` (8).
        mode: ``display`` or ``strategy``.
    """
    from candlegraph.knowledge.embed import EmbeddingError, build_embedder
    from candlegraph.knowledge.retrieve import RetrievalConfig, check_backend, retrieve

    if mode not in ("display", "strategy"):
        raise ValueError(f"mode must be display or strategy (got {mode!r})")
    settings = _settings()
    check_backend(settings.retrieval_backend)
    wanted = top_k or settings.retrieval_top_k
    config = RetrievalConfig(
        top_k=_limit(wanted, 100),
        candidates=max(settings.retrieval_candidates, wanted),
        mode="strategy" if mode == "strategy" else "display",
    )

    asset_id: str | None = None
    terms: list[str] = []
    if symbol:
        registry = _registry()
        token = registry.by_symbol(symbol)
        if token is None:
            raise ValueError(f"unknown token {symbol!r}; tracked: {', '.join(registry.symbols)}")
        asset_id = token.address
        terms = list(token.aliases) or [token.symbol]

    try:
        embedder = build_embedder(settings)
    except EmbeddingError:
        # The lexical leg alone still answers, and the result says so.
        embedder = None

    result = retrieve(
        _db(),
        query,
        bar=_moment(before, "before") or datetime.now(tz=UTC),
        embedder=embedder,
        terms=terms,
        asset_id=asset_id,
        config=config,
    )
    return payloads.retrieval_payload(result, query)


@boundary
def explain_signal(
    symbol: str, strategy: str = "bollinger", at: str | None = None, search: bool = False
) -> dict[str, Any]:
    """Explain one bar of a strategy with the news that was already public.

    **Writes** an ``explanation`` row (an upsert on the signal's natural
    key). **With ``search=true`` it may perform one metered web search per
    token per UTC day**, which also writes ``document`` and ``source_call``
    rows; the budget guard is inside the pipeline, so repeated calls spend
    at most the daily cap and a refusal is recorded rather than retried.
    Default is false: a credit is spent only when a caller says so.

    The rationale may cite only documents published at or before the bar,
    and a citation the narrator invented is stripped before you see it.

    Args:
        symbol: A tracked token symbol, case-insensitive.
        strategy: A registered strategy name; its default configuration is used.
        at: The bar to explain, ISO 8601 UTC. Default: the latest bar.
        search: Whether the metered news source may be called. Default false.
    """
    from candlegraph.knowledge.explain import explain_symbol

    explanation = explain_symbol(_db(), _settings(), symbol, strategy, at=_moment(at, "at"), search=search)
    return payloads.explanation_payload(explanation)


@boundary
def knowledge_budget() -> dict[str, Any]:
    """How many news-search credits this month and today have been spent.

    Summed from the ``source_call`` ledger, which records refusals and
    rate-limited calls as well as successes. ``level`` is the observer's own
    grading: ok below 80% of the cap, warning from 80%, critical from 100%.
    The reserve is held back for an operator's manual search and the
    pipeline treats it as already spent.
    """
    from candlegraph.knowledge.budget import usage_fraction, usage_level
    from candlegraph.knowledge.news import budget_from, source_spend
    from candlegraph.knowledge.sources.tavily import SOURCE_ID

    settings = _settings()
    budget = budget_from(settings)
    month, today = source_spend(_db(), SOURCE_ID, datetime.now(tz=UTC))
    month_fraction = usage_fraction(month, budget.monthly_credits)
    return {
        "source": SOURCE_ID,
        "key_configured": settings.tavily_api_key is not None,
        "month_spent": month,
        "monthly_credits": budget.monthly_credits,
        "usable_monthly": budget.usable_monthly,
        "reserve": budget.reserve,
        "month_fraction": month_fraction,
        "today_spent": today,
        "daily_credits": budget.daily_credits,
        "day_fraction": usage_fraction(today, budget.daily_credits),
        "level": usage_level(month_fraction),
        "note": "a search costs one credit and is cached per token per UTC day",
    }


# --- the registry the server reads ------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    """One tool, and the honest answer to "does it write?".

    ``read_only`` becomes the MCP ``readOnlyHint`` annotation, and ``writes``
    names the tables a non-read-only tool touches. The design's rule is that
    no tool writes market data; the test that enforces it reads this table.
    """

    name: str
    run: Callable[..., dict[str, Any]]
    read_only: bool = True
    writes: str = ""


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec("status", status),
    ToolSpec("recent_ingest_runs", recent_ingest_runs),
    ToolSpec("data_quality_report", data_quality_report),
    ToolSpec("candles", candles),
    ToolSpec("stats", stats),
    ToolSpec("compare", compare),
    ToolSpec("signals", signals),
    ToolSpec("backtest", backtest),
    ToolSpec("agent_runs", agent_runs),
    ToolSpec("latest_agent_report", latest_agent_report),
    ToolSpec("observe", observe, read_only=False, writes="agent_run"),
    ToolSpec("search_knowledge", search_knowledge),
    ToolSpec(
        "explain_signal",
        explain_signal,
        read_only=False,
        writes="explanation, and document/source_call when search is true",
    ),
    ToolSpec("knowledge_budget", knowledge_budget),
)
