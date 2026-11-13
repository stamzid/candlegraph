"""Command-line entrypoint: ``candlegraph <command>``.

One binary, several roles. The API server, the ingest worker, the UI and the
agent scheduler are separate processes in the compose stack; the one-shot
commands (``backfill``, ``report``, ``signals``, ``backtest``, ``portfolio``,
``risk``, ``observe``, ``janitor``) are what CI and an operator use without starting
any of them.

Every analytics command is deterministic: the same stored data and the same
flags produce the same numbers, which is what makes a reported backtest
something you can check.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import Any

from candlegraph.settings import Settings, get_settings
from candlegraph.utils.logging_utils import configure_logging, service_logger

# The three gap estimators, named here so the parser can offer them without
# importing the ingestion package at start-up. `candlegraph.ingestion.fills`
# owns the real list and a test holds the two together.
FILL_METHODS = ("linear", "causal", "bridge")

# Monte Carlo path generators, named here for the same reason: the parser
# should not import the analytics package at start-up. `analytics.simulation`
# owns the real list and a test holds the two together.
SIMULATION_GENERATORS = ("block", "iid", "gbm", "regime")
DEFAULT_GENERATOR = "block"


# `serve --port auto` tries these in order. 8000 is the compose API's host
# port (APP_HOST_PORT), which is the usual collision; the rest are the
# conventional development alternatives, none of which the stack binds
# (8501 is the UI, 5432 Postgres, 11434 Ollama).
SUGGESTED_PORTS = (8000, 8001, 8080, 8081, 8888, 9000)


def _add_jobs(parser: argparse.ArgumentParser, role: str) -> None:
    parser.add_argument(
        "--jobs",
        default=None,
        help=f"comma-separated job names to host (default: every {role} job; see `candlegraph jobs`)",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="candlegraph", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the GraphQL API server")
    serve.add_argument("--host", default=None, help="interface to bind (default: HOST, 0.0.0.0)")
    serve.add_argument(
        "--port",
        default=None,
        help="port to bind (default: PORT, 8000). 'auto' takes the first free one of "
        f"{', '.join(str(p) for p in SUGGESTED_PORTS)}. A busy port is refused with free suggestions "
        "rather than left to uvicorn's bare 'address already in use'",
    )
    worker = sub.add_parser("worker", help="run the ingest worker loop")
    _add_jobs(worker, "worker")
    ingest = sub.add_parser("ingest", help="run one ingest pass and exit")
    backfill = sub.add_parser("backfill", help="run one ingest pass over a longer window")
    backfill.add_argument("--days", type=int, default=90, help="how far back to reach (default: 90)")
    for parser_ in (ingest, backfill):
        parser_.add_argument(
            "--progress",
            action="store_true",
            help="print each stage (token_metadata, token_hours, pool_discovery, pool_hours) to stderr "
            "as it finishes, in the same format as `observe --progress`",
        )

    report = sub.add_parser("report", help="print the data-quality report")
    report.add_argument("--days", type=int, default=None, help="only consider the last N days")
    report.add_argument("--strict", action="store_true", help="exit non-zero when the report is not clean")
    report.add_argument(
        "--json",
        action="store_true",
        help="print the report as JSON instead of the table; the same structure the "
        "data_quality_report MCP tool returns",
    )

    runs = sub.add_parser("runs", help="show recent ingest runs from the ledger")
    runs.add_argument("--limit", type=int, default=10)
    which = runs.add_mutually_exclusive_group()
    which.add_argument(
        "--job",
        default=None,
        help="runs of one job from run_log (ingest, observer, janitor), in progress included",
    )
    which.add_argument("--all", action="store_true", help="runs of every job from run_log, newest first")

    fill = sub.add_parser(
        "fill",
        help="recompute the display-only fill cache for hours with no stored row",
    )
    fill.add_argument(
        "symbols",
        nargs="*",
        help="tokens to refresh; default: every token with stored history",
    )
    _add_window(fill)
    fill.add_argument(
        "--max-fill",
        type=int,
        default=None,
        help="longest run of missing hours that may be filled (default: 24). "
        "Beyond it the hole stays, whole.",
    )
    # One estimator per run. They are alternatives for the same missing hour,
    # not layers, so there is no way to ask for two at once; `--all-methods`
    # computes each in turn so they can be compared.
    fill.add_argument(
        "--method",
        choices=FILL_METHODS,
        default=None,
        help="which estimator to compute (default: FILL_METHOD, normally linear). "
        "linear = straight line, no error bar, look-ahead; causal = Kalman "
        "local-level, past data only, safe for a strategy; bridge = Brownian "
        "bridge pinned to both ends, most accurate, look-ahead",
    )
    fill.add_argument(
        "--all-methods",
        action="store_true",
        help="compute every estimator in turn, so the same gap holds all three and they can be compared",
    )
    fill.add_argument(
        "--clear",
        action="store_true",
        help="delete the cached fills instead of recomputing them (the chosen method, "
        "or every method with --all-methods)",
    )
    fill.add_argument("--status", action="store_true", help="print what is cached and exit")
    fill.add_argument(
        "--evaluate",
        action="store_true",
        help="hold out hours whose real price is known, re-estimate them with every "
        "method, and report the error of each",
    )
    fill.add_argument(
        "--gap-hours",
        type=int,
        default=4,
        help="width of the synthetic holes --evaluate punches (default: 4)",
    )

    sim = sub.add_parser(
        "simulate",
        help="run a strategy over synthetic markets, or measure the execution assumptions",
    )
    sim.add_argument("symbol")
    sim.add_argument("--strategy", default="bollinger", help="strategy name")
    _add_window(sim)
    sim.add_argument("--paths", type=int, default=500, help="synthetic paths to draw (default: 500)")
    sim.add_argument(
        "--method",
        choices=SIMULATION_GENERATORS,
        default=None,
        help="path generator. block = stationary block bootstrap, keeps volatility "
        "clustering (the default); iid = shuffle returns, keeps the distribution and "
        "destroys the structure; gbm = normal returns at the sample's moments; "
        "regime = two-state Markov switching",
    )
    sim.add_argument("--seed", type=int, default=0, help="recorded with the result; same seed, same paths")
    sim.add_argument(
        "--execution",
        action="store_true",
        help="instead of path simulation, reconcile the discrete-event engine against the "
        "vectorised backtester and report what latency and partial fills cost",
    )
    sim.add_argument("--fee-bps", type=float, default=None)
    sim.add_argument("--slippage-bps", type=float, default=None)
    _add_strategy_flags(sim)

    sub.add_parser("strategies", help="list the available strategies")

    ui = sub.add_parser("ui", help="run the Streamlit UI")
    ui.add_argument("--host", default=None)
    ui.add_argument("--port", type=int, default=None)

    stats = sub.add_parser("stats", help="rolling mean and volatility for a token")
    stats.add_argument("symbol")
    _add_window(stats)
    stats.add_argument("--window", type=int, default=None, help="rolling lookback in bars")

    compare = sub.add_parser("compare", help="pairwise comparison of two tokens' stored history")
    compare.add_argument("symbol_a")
    compare.add_argument("symbol_b")
    _add_window(compare)
    compare.add_argument("--window", type=int, default=None, help="rolling-correlation lookback in bars")
    compare.add_argument(
        "--risk-free-rate",
        type=float,
        default=None,
        help="annual decimal fraction: 0.04 means 4%/yr (the UI takes percent)",
    )
    compare.add_argument("--tail", type=int, default=10, help="rolling-correlation rows to print")

    signals = sub.add_parser("signals", help="generate signals for a token")
    signals.add_argument("symbol")
    signals.add_argument("--strategy", default="bollinger", help="strategy name")
    _add_window(signals)
    signals.add_argument("--store", action="store_true", help="write the signals to the database")
    signals.add_argument("--tail", type=int, default=10, help="rows to print")
    signals.add_argument(
        "--explain",
        action="store_true",
        help="explain one bar (default: the latest) with the numeric context and cited news published "
        "before it; stores an explanation row. One Tavily search per token per UTC day at most",
    )
    signals.add_argument("--at", default=None, help="with --explain: the bar to explain, ISO 8601 UTC")
    signals.add_argument(
        "--reference",
        default=None,
        help="with --explain: the symbol the move is compared against (default: KNOWLEDGE_REFERENCE_SYMBOL)",
    )
    signals.add_argument(
        "--no-search",
        action="store_true",
        help="with --explain: never call the news source; stored documents only",
    )
    _add_strategy_flags(signals)

    bt = sub.add_parser("backtest", help="backtest a strategy over stored history")
    bt.add_argument("symbol")
    bt.add_argument("--strategy", default="bollinger", help="strategy name, or 'all'")
    _add_window(bt)
    bt.add_argument("--fee-bps", type=float, default=None)
    bt.add_argument("--slippage-bps", type=float, default=None)
    bt.add_argument(
        "--risk-free-rate",
        type=float,
        default=None,
        help="annual decimal fraction: 0.04 means 4%/yr (the UI takes percent)",
    )
    _add_strategy_flags(bt)

    portfolio = sub.add_parser("portfolio", help="mean-variance optimise across tokens")
    portfolio.add_argument(
        "--symbols", nargs="*", default=None, help="tokens to allocate across (default: all tracked)"
    )
    _add_window(portfolio)
    portfolio.add_argument(
        "--risk-free-rate",
        type=float,
        default=None,
        help="annual decimal fraction: 0.04 means 4%/yr (the UI takes percent)",
    )
    portfolio.add_argument(
        "--objective",
        default="max_sharpe",
        choices=["max_sharpe", "min_volatility", "target_return", "risk_parity", "equal_weight"],
    )
    portfolio.add_argument("--target-return", type=float, default=None, help="annual, for target_return")
    portfolio.add_argument("--allow-short", action="store_true")
    portfolio.add_argument("--max-weight", type=float, default=1.0)
    portfolio.add_argument("--shrinkage", type=float, default=0.0, help="covariance shrinkage, 0-1")
    portfolio.add_argument("--frontier", type=int, default=0, help="also print N frontier points")

    risk = sub.add_parser("risk", help="tail risk and systematic/idiosyncratic split for a token")
    risk.add_argument("symbol")
    _add_window(risk)
    risk.add_argument("--benchmark", default=None, help="token to regress against (default: RISK_BENCHMARK)")
    risk.add_argument("--no-benchmark", action="store_true", help="skip the systematic/idiosyncratic split")
    risk.add_argument(
        "--confidence", type=float, default=None, help="VaR confidence, e.g. 0.95 (default: RISK_CONFIDENCE)"
    )
    risk.add_argument(
        "--horizon", type=int, default=None, help="VaR horizon in bars (default: RISK_HORIZON_BARS)"
    )
    risk.add_argument(
        "--method",
        default=None,
        choices=["historical", "parametric", "cornish_fisher"],
        help="VaR estimator (default: RISK_VAR_METHOD)",
    )
    risk.add_argument(
        "--risk-free-rate",
        type=float,
        default=None,
        help="annual decimal fraction: 0.04 means 4%/yr (the UI takes percent)",
    )

    forecast = sub.add_parser("forecast", help="ARIMA forecast for a token")
    forecast.add_argument("symbol")
    _add_window(forecast)
    forecast.add_argument("--order", default="1,1,1", help="p,d,q")
    forecast.add_argument("--auto-order", action="store_true", help="pick p and q by criterion")
    forecast.add_argument("--steps", type=int, default=1, help="periods ahead")
    forecast.add_argument("--confidence", type=float, default=0.95)
    forecast.add_argument(
        "--posterior",
        action="store_true",
        help="sample the model's parameters by MCMC instead of fitting a point estimate, "
        "so the interval includes parameter uncertainty and not just innovation noise. "
        "Reports R-hat and effective sample size, and refuses if the chains did not mix",
    )
    forecast.add_argument("--draws", type=int, default=2000, help="posterior draws per chain")
    forecast.add_argument("--chains", type=int, default=4, help="chains; R-hat needs at least 2")
    forecast.add_argument("--seed", type=int, default=0, help="recorded with the result")

    # --- agents ---
    observe = sub.add_parser("observe", help="run the run-observer agent once and post its report")
    observe.add_argument("--strict", action="store_true", help="exit non-zero at WARNING or worse")
    observe.add_argument("--json", action="store_true", help="print the report as JSON instead of Markdown")
    observe.add_argument(
        "--progress",
        action="store_true",
        help="print each stage (collect_ledger, collect_quality, collect_metrics, assess, publish) to stderr "
        "as it finishes, with its duration and what it read",
    )
    _add_sinks(observe)

    janitor = sub.add_parser("janitor", help="run the CI janitor agent once and post its report")
    janitor.add_argument("--strict", action="store_true", help="exit non-zero at WARNING or worse")
    janitor.add_argument("--json", action="store_true", help="print the report as JSON instead of Markdown")
    janitor.add_argument(
        "--open-pr",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="push a branch and draft a PR when fixes changed files (default: JANITOR_OPEN_PR)",
    )
    janitor.add_argument(
        "--checks", default=None, help="comma-separated make targets (default: JANITOR_CHECKS)"
    )
    _add_sinks(janitor)

    agents = sub.add_parser("agents", help="run the agents on their schedule until stopped")
    _add_jobs(agents, "agents")

    sub.add_parser("jobs", help="list the registered jobs: schedule, process role and write surface")

    agent_runs = sub.add_parser("agent-runs", help="show recent agent runs from the agent ledger")
    agent_runs.add_argument("--limit", type=int, default=10)
    agent_runs.add_argument("--agent", default=None, help="only this agent")
    agent_runs.add_argument("--show", action="store_true", help="print the newest matching report in full")

    # --- tools over MCP ---
    mcp = sub.add_parser(
        "mcp",
        help="serve candlegraph's read-only tools over the Model Context Protocol",
        description="Serve the read-only tool surface. A client (Claude Code, an IDE) launches "
        "this over stdio and calls the same functions the other subcommands do. No tool writes "
        "market data; `explain_signal` is the only one that can spend a search credit, and only "
        "when it is asked to.",
    )
    mcp.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="stdio (default) is a client launching this process; http serves the same tools "
        "over streamable HTTP for a client in another container",
    )
    mcp.add_argument("--host", default="127.0.0.1", help="with --transport http: bind address")
    mcp.add_argument("--port", type=int, default=8900, help="with --transport http: port")

    # --- knowledge ---
    knowledge = sub.add_parser("knowledge", help="operate the knowledge layer (stage 1)")
    knowledge_sub = knowledge.add_subparsers(dest="knowledge_command", required=True)
    knowledge_sub.add_parser(
        "sync-assets", help="upsert asset rows (addresses, symbols, aliases) from config/tokens.yaml"
    )
    fetch = knowledge_sub.add_parser(
        "fetch-news", help="one budgeted Tavily news search per token per UTC day; every call is ledgered"
    )
    fetch.add_argument(
        "--symbol", action="append", default=None, help="only these tokens (repeatable; default: all tracked)"
    )
    search = knowledge_sub.add_parser(
        "search", help="hybrid retrieval over stored news as of a bar: dense + lexical, fused, no look-ahead"
    )
    search.add_argument("query", help="what to look for, in words")
    search.add_argument("--symbol", default=None, help="restrict to chunks mentioning this token")
    search.add_argument("--at", default=None, help="the bar, ISO 8601 UTC (default: now)")
    search.add_argument(
        "--top-k", type=int, default=None, help="results to return (default: RETRIEVAL_TOP_K)"
    )
    search.add_argument("--lexical-only", action="store_true", help="skip the embedder; tsvector leg alone")
    search.add_argument(
        "--mode",
        choices=["display", "strategy"],
        default="display",
        help="strategy also requires retrieved_at <= bar and never returns undated text",
    )
    search.add_argument(
        "--include-undated", action="store_true", help="display mode: show undated chunks, flagged"
    )
    knowledge_sub.add_parser(
        "link-mentions", help="link stored chunks to the assets their aliases name (backfill; idempotent)"
    )
    for name, text in (
        ("embed", "embed chunks that have no vector yet (or one from another model)"),
        ("reembed", "same as embed; the name to reach for after changing EMBEDDING_MODEL"),
    ):
        embed = knowledge_sub.add_parser(name, help=text)
        embed.add_argument(
            "--batch-size",
            type=int,
            default=None,
            help="texts per model call (default: EMBEDDING_BATCH_SIZE)",
        )
        embed.add_argument("--limit", type=int, default=None, help="stop after about this many chunks")

    return parser


def _add_sinks(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--sinks", default=None, help="comma-separated report sinks (default: AGENT_REPORT_SINKS)"
    )


def _add_window(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--days", type=int, default=None, help="trailing window (default: ANALYTICS_LOOKBACK_DAYS)"
    )


def _add_strategy_flags(parser: argparse.ArgumentParser) -> None:
    """Flags shared by the commands that build a strategy config."""
    parser.add_argument("--window", type=int, default=None, help="bollinger: rolling window")
    parser.add_argument("--entry-z", type=float, default=None, help="bollinger: entry z-score")
    parser.add_argument("--exit-z", type=float, default=None, help="bollinger: exit z-score")
    parser.add_argument("--fast", type=int, default=None, help="crossover: fast window")
    parser.add_argument("--slow", type=int, default=None, help="crossover: slow window")
    parser.add_argument("--order", default=None, help="arima: p,d,q")
    parser.add_argument("--threshold", type=float, default=None, help="arima: forecast return cutoff")
    parser.add_argument("--refit-every", type=int, default=None, help="arima: bars between refits")
    # Tri-state on purpose: `store_true` would make "not specified" look like
    # False and silently disable shorting for strategies that default to on.
    parser.add_argument(
        "--allow-short",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="override the strategy's own shorting default",
    )


def _serve(settings: Settings, host: str | None = None, port: str | None = None) -> int:
    import uvicorn

    from candlegraph.utils.ports import first_free, port_is_free

    bind_host = host or settings.host
    if port is None:
        bind_port = settings.port
    elif port == "auto":
        found = first_free(bind_host, SUGGESTED_PORTS)
        if found is None:
            service_logger.error(
                "None of the suggested ports is free (%s); pass --port N",
                ", ".join(map(str, SUGGESTED_PORTS)),
            )
            return 1
        bind_port = found
    else:
        try:
            bind_port = int(port)
        except ValueError:
            service_logger.error("--port takes a number or 'auto', not %r", port)
            return 2
    if not port_is_free(bind_host, bind_port):
        # The usual cause is the compose stack's API on APP_HOST_PORT=8000.
        free = [p for p in SUGGESTED_PORTS if p != bind_port and port_is_free(bind_host, p)]
        service_logger.error(
            "Port %s is already in use on %s (often the compose API on APP_HOST_PORT). "
            "Free now: %s. Run `candlegraph serve --port %s` or `--port auto`.",
            bind_port,
            bind_host,
            ", ".join(map(str, free)) or "none of the suggestions",
            free[0] if free else "N",
        )
        return 1
    service_logger.info("Starting candlegraph API on %s:%s", bind_host, bind_port)
    uvicorn.run(
        "candlegraph.api.app:app",
        host=bind_host,
        port=bind_port,
        log_level=settings.log_level.lower(),
    )
    return 0


def _selected_jobs(role: str, names: str | None) -> Any:
    from candlegraph.harness.jobs import select

    if names is None:
        return select(role=role)  # type: ignore[arg-type]
    return select(names=_sink_names(names) or ())


def _worker(settings: Settings, names: str | None = None) -> int:
    from candlegraph.harness.process import run_process, worker_banner

    try:
        jobs = _selected_jobs("worker", names)
    except ValueError as exc:
        service_logger.error("%s", exc)
        return 2
    run_process(settings, jobs, name="Ingest worker", banner=worker_banner(settings))
    return 0


def _jobs(settings: Settings) -> int:
    from candlegraph.harness.jobs import JOBS

    rows = [
        (
            job.name,
            job.role,
            f"{job.interval_setting.upper()}={job.schedule.seconds(settings)}"
            + ("" if job.schedule.seconds(settings) > 0 else " (disabled)"),
            ",".join(sorted(job.writes)),
            job.description,
        )
        for job in JOBS.values()
    ]
    header = ("job", "role", "schedule", "writes", "description")
    widths = [max(len(str(r[i])) for r in [header, *rows]) for i in range(len(header))]
    for row in [header, *rows]:
        print("  ".join(str(cell).ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip())
    return 0


def _stage_printer(label: str) -> Any:
    """An ``on_stage`` callback printing each stage to stderr, so stdout stays clean."""
    from candlegraph.harness.stages import StageRecord, format_stage

    print(f"{label} stages:", file=sys.stderr, flush=True)

    def on_stage(record: StageRecord) -> None:
        print(format_stage(record), file=sys.stderr, flush=True)

    return on_stage


def _ingest(settings: Settings, days: int | None, trigger: str, progress: bool = False) -> int:
    from candlegraph.ingestion.runner import run_ingest
    from candlegraph.storage import get_db
    from candlegraph.tokens import get_token_registry

    on_stage = _stage_printer("ingest") if progress else None
    try:
        result = run_ingest(
            get_db(), get_token_registry(), settings, trigger=trigger, lookback_days=days, on_stage=on_stage
        )
    except Exception:
        # The run could not start; the harness logged it and closed the row as failed.
        return 1
    service_logger.info(
        "%s: %d token hours, %d pool hours, %d errors",
        trigger,
        result.hours_written,
        result.pool_hours_written,
        len(result.errors),
    )
    for target, message in result.errors.items():
        service_logger.error("  %s: %s", target, message)
    return 0 if result.ok else 1


def _report(settings: Settings, days: int | None, strict: bool, as_json: bool = False) -> int:
    import json

    from candlegraph.ingestion.quality import build_report, format_report, report_dict
    from candlegraph.ingestion.runner import lookback_start
    from candlegraph.storage import get_db
    from candlegraph.tokens import get_token_registry

    start_unix = lookback_start(days) if days else None
    report = build_report(
        get_db(),
        expected_symbols=get_token_registry().symbols,
        start_unix=start_unix,
        min_expected_coverage=settings.min_expected_coverage,
    )
    print(json.dumps(report_dict(report), indent=2, default=str) if as_json else format_report(report))
    return 0 if report.ok or not strict else 1


def _fill(settings: Settings, args: argparse.Namespace) -> int:
    """Recompute, clear, evaluate, or report on the fill cache.

    Three estimators share the table, keyed apart by `method`, so the same
    missing hour can hold all three and they can be compared. Only one is
    ever *selected* at a time — they are alternatives, not layers.

    Two of the three look ahead and are display only. `causal` does not, but
    nothing reads this cache on a strategy's behalf regardless: `backtest`
    and `signals` load observations through `load_symbol_series`, which has
    never touched this table.
    """
    from candlegraph.analytics.prices import DEFAULT_MAX_FILL, fill_rule
    from candlegraph.ingestion.fills import (
        METHOD_SUMMARY,
        METHODS,
        fill_cache_summary,
        refresh_fill_cache,
    )
    from candlegraph.storage import get_db

    db = get_db()
    chosen = args.method or settings.fill_method
    methods = list(METHODS) if args.all_methods else [chosen]

    def stored_symbols() -> list[str]:
        """Symbols with rows, read straight from the coverage query."""
        rows = db.fetch_all(db.sql.coverage_report, {"start_unix": None})
        return sorted(str(row["symbol"]) for row in rows if row.get("symbol"))

    if args.status:
        cached = fill_cache_summary(db)
        if cached.empty:
            print("the fill cache is empty; run `candlegraph fill` to populate it")
            return 0
        print(f"{'symbol':<10}{'method':<9}{'hours':>7}{'banded':>8}{'longest':>9}  computed")
        for record in cached.to_dict("records"):
            longest = int(record["longest_gap_hours"] or 0)
            print(
                f"{record['symbol']:<10}{record['method']:<9}"
                f"{int(record['filled_hours']):>7}{int(record['hours_with_band'] or 0):>8}"
                f"{longest:>8}h  {record['computed_at']:%Y-%m-%d %H:%M}"
            )
        print()
        for method in METHODS:
            rule = fill_rule(_policy_for(method))
            flag = "causal" if rule.causal else "display only"
            print(f"  {method:<8} {flag:<13} {METHOD_SUMMARY[method]}")
        return 0

    symbols = list(args.symbols) or stored_symbols()
    if not symbols:
        print("no stored history to fill; run `make backfill days=90` first")
        return 0

    if args.evaluate:
        return _fill_evaluate(settings, db, symbols, args)

    if args.clear:
        total = 0
        for method in methods:
            total += db.execute(db.sql.delete_fills_for_method, {"method": method, "symbols": symbols})
        print(f"cleared {total} cached fill(s) for {len(symbols)} symbol(s) across {', '.join(methods)}")
        return 0

    cap = args.max_fill if args.max_fill is not None else DEFAULT_MAX_FILL
    for method in methods:
        summary = refresh_fill_cache(db, symbols, method=method, days=args.days, max_fill=cap)
        rule = fill_rule(_policy_for(method))
        banner = "causal — safe for a strategy" if rule.causal else "display only — looks ahead"
        print(
            f"fill cache ({summary.method}, {banner}): {summary.rows_written} hour(s) "
            f"written across {len(summary.runs)} symbol(s)"
        )
        for run in summary.runs:
            if run.filled_hours or run.skipped_hours:
                print(
                    f"  {run.symbol:<10}{run.filled_hours:>6} filled"
                    f"{run.skipped_hours:>7} left (gap over the cap)"
                    f"   {run.gaps} gap(s), longest {run.longest_gap_hours}h"
                )
    print("Nothing here is read by `backtest` or `signals`; the cache is for display.")
    return 0


def _policy_for(method: str) -> str:
    from candlegraph.ingestion.fills import METHOD_POLICY

    return METHOD_POLICY[method]


def _fill_evaluate(settings: Settings, db: Any, symbols: list[str], args: argparse.Namespace) -> int:
    """Score every estimator against hours whose real price we already have.

    "Which one is best" is otherwise a matter of taste. Holding out observed
    hours and re-estimating them is the falsifiable version.
    """
    from candlegraph.analytics.prices import load_price_frame
    from candlegraph.ingestion.fills import score_methods

    window = args.days if args.days is not None else settings.analytics_lookback_days
    frame = load_price_frame(db, symbols, days=window, fill="none")
    if frame.empty:
        print("no stored history to evaluate against")
        return 0

    print(
        f"Holding out {args.gap_hours}-hour stretches of real prices and re-estimating "
        f"them. Error is relative, so tokens at different prices compare."
    )
    any_scored = False
    for symbol in symbols:
        if symbol not in frame.columns:
            continue
        scores = score_methods(frame[symbol], gap_hours=args.gap_hours)
        if not scores:
            print(f"\n{symbol}: not enough history to evaluate")
            continue
        any_scored = True
        print(f"\n{symbol}  ({scores[0].hours} held-out hours)")
        print(f"  {'method':<9}{'MAE':>10}{'RMSE':>10}{'worst':>9}  consumable by")
        for score in scores:
            who = "a strategy" if score.causal else "display only"
            print(
                f"  {score.method:<9}{score.mae_bps:>9.1f}b{score.rmse * 10_000:>9.1f}b"
                f"{score.worst * 100:>8.2f}%  {who}"
            )
    if any_scored:
        print(
            "\nA lower error is not a licence to trade on it: `bridge` wins because it "
            "reads the bar *after* the gap. Among estimates that could have been made at "
            "the time, `causal` is the only entry."
        )
    return 0


def _runs(limit: int) -> int:
    from candlegraph.ingestion.ledger import recent_runs
    from candlegraph.storage import get_db

    rows = recent_runs(get_db(), limit)
    if not rows:
        print("no ingest runs recorded yet")
        return 0

    print(f"{'started':<21}{'trigger':<11}{'status':<11}{'tokens':>8}{'pools':>8}{'secs':>8}")
    for row in rows:
        duration = row["duration_seconds"]
        elapsed = f"{float(duration):>8.1f}" if duration is not None else f"{'-':>8}"
        print(
            f"{row['started_at']:%Y-%m-%d %H:%M:%S}  {row['trigger']:<11}{row['status']:<11}"
            f"{row['token_hours_written']:>8}{row['pool_hours_written']:>8}{elapsed}"
        )
    return 0


def _run_log(limit: int, job: str | None) -> int:
    """Rows of the ``run_log`` view: every job in one vocabulary."""
    from candlegraph.storage import get_db

    db = get_db()
    rows = db.fetch_all(db.sql.get_run_log, {"limit": limit, "job": job})
    if not rows:
        print(f"no {job + ' ' if job else ''}runs recorded yet")
        return 0

    print(f"{'started':<21}{'job':<10}{'trigger':<11}{'outcome':<11}{'verdict':<10}{'secs':>8}  run_id")
    for row in rows:
        duration = row["duration_seconds"]
        elapsed = f"{float(duration):>8.1f}" if duration is not None else f"{'-':>8}"
        # NULL trigger = a row written before the column existed; shown as "-", never guessed.
        print(
            f"{row['started_at']:%Y-%m-%d %H:%M:%S}  {row['job']:<10}{row['trigger'] or '-':<11}"
            f"{row['outcome']:<11}{row['verdict'] or '-':<10}{elapsed}  {str(row['run_id'])[:8]}"
        )
    return 0


# --- analytics -------------------------------------------------------------


def parse_order(text: str) -> tuple[int, int, int]:
    """Parse a ``p,d,q`` string into an ARIMA order."""
    parts = [piece.strip() for piece in text.split(",")]
    if len(parts) != 3:
        raise SystemExit(f"--order must be p,d,q (got {text!r})")
    try:
        p, d, q = (int(piece) for piece in parts)
    except ValueError:
        raise SystemExit(f"--order must be three integers (got {text!r})") from None
    return p, d, q


def build_strategy_config(name: str, args: argparse.Namespace) -> object | None:
    """Turn parsed flags into the config object the named strategy expects.

    Only flags the user actually passed are applied, so each strategy keeps
    its own defaults rather than inheriting another's.
    """
    from candlegraph.analytics.arima import ArimaConfig
    from candlegraph.strategies.arima_signal import ArimaSignalConfig
    from candlegraph.strategies.bollinger import BollingerConfig
    from candlegraph.strategies.crossover import CrossoverConfig

    given = {k: v for k, v in vars(args).items() if v is not None}

    match name:
        case "bollinger":
            return BollingerConfig(
                **{k: given[k] for k in ("window", "entry_z", "exit_z", "allow_short") if k in given}
            )
        case "crossover":
            return CrossoverConfig(**{k: given[k] for k in ("fast", "slow", "allow_short") if k in given})
        case "arima":
            arima = ArimaConfig(order=parse_order(given["order"])) if "order" in given else ArimaConfig()
            return ArimaSignalConfig(
                arima=arima,
                **{k: given[k] for k in ("threshold", "refit_every", "allow_short") if k in given},
            )
        case _:
            return None


def _prices_for(settings: Settings, symbol: str, days: int | None) -> Any:
    """Load one token's USD price series, or exit with a useful message."""
    from candlegraph.analytics.prices import PriceError, load_symbol_series
    from candlegraph.storage import get_db
    from candlegraph.tokens import get_token_registry

    window = days if days is not None else settings.analytics_lookback_days
    try:
        return load_symbol_series(get_db(), get_token_registry(), symbol, days=window)
    except PriceError as exc:
        raise SystemExit(str(exc)) from exc


def _ui(settings: Settings, args: argparse.Namespace) -> int:
    """Hand off to Streamlit's own CLI, which owns the server loop."""
    from pathlib import Path

    try:
        from streamlit.web import cli as streamlit_cli
    except ImportError:
        raise SystemExit("Streamlit is not installed. Install the UI extra: `uv sync --extra ui`.") from None

    script = Path(__file__).resolve().parent / "ui" / "app.py"
    host = args.host or settings.ui_host
    port = args.port or settings.ui_port
    service_logger.info("Starting candlegraph UI on %s:%s", host, port)

    sys.argv = [
        "streamlit",
        "run",
        str(script),
        "--server.address",
        host,
        "--server.port",
        str(port),
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]
    streamlit_cli.main()
    return 0


def _strategies_cmd() -> int:
    from candlegraph.strategies import available, get

    for name in available():
        print(f"{name:<12}{get(name).description}")
    return 0


def _stats(settings: Settings, args: argparse.Namespace) -> int:
    from candlegraph.analytics.rolling import rolling_summary

    prices = _prices_for(settings, args.symbol, args.days)
    window = args.window or settings.rolling_window
    summary = rolling_summary(prices, window=window, interval_hours=settings.analytics_interval_hours)
    latest = summary.dropna().tail(1)

    print(f"{args.symbol}: {len(prices)} bars, rolling window {window}")
    if latest.empty:
        print(f"not enough history for a {window}-bar window")
        return 1

    row = latest.iloc[0]
    print(f"{'last price':<22}{row['price']:>14,.4f}")
    print(f"{'rolling mean':<22}{row['rolling_mean']:>14,.4f}")
    print(f"{'rolling return/bar':<22}{row['rolling_return']:>14.4%}")
    print(f"{'volatility (annual)':<22}{row['rolling_volatility']:>14.2%}")
    return 0


def _compare(settings: Settings, args: argparse.Namespace) -> int:
    from candlegraph.analytics.pairwise import compare_pair, format_comparison
    from candlegraph.analytics.prices import load_price_frame
    from candlegraph.storage import get_db
    from candlegraph.tokens import get_token_registry

    registry = get_token_registry()
    for symbol in (args.symbol_a, args.symbol_b):
        if registry.by_symbol(symbol) is None:
            raise SystemExit(f"unknown token {symbol!r}; tracked: {', '.join(registry.symbols)}")
    if args.symbol_a == args.symbol_b:
        raise SystemExit("compare needs two different tokens")

    window = args.days if args.days is not None else settings.analytics_lookback_days
    # fill="none": the comparison aligns on hours both tokens actually traded;
    # forward-filling first would correlate a real price with a stale one.
    frame = load_price_frame(get_db(), [args.symbol_a, args.symbol_b], days=window, fill="none")
    missing = [s for s in (args.symbol_a, args.symbol_b) if s not in frame.columns or frame[s].dropna().empty]
    if missing:
        raise SystemExit(f"no stored price history for {', '.join(missing)} in the last {window} days")

    try:
        result = compare_pair(
            frame[args.symbol_a].rename(args.symbol_a),
            frame[args.symbol_b].rename(args.symbol_b),
            window=args.window or settings.rolling_window,
            interval_hours=settings.analytics_interval_hours,
            risk_free_rate=(
                args.risk_free_rate if args.risk_free_rate is not None else settings.risk_free_rate
            ),
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    print(format_comparison(result))
    tail = result.frame[["ratio", "rolling_correlation"]].dropna().tail(args.tail)
    if not tail.empty:
        print()
        print(tail.to_string(float_format=lambda v: f"{v:,.4f}"))
    return 0


def _signals(settings: Settings, args: argparse.Namespace) -> int:
    from candlegraph.storage import get_db
    from candlegraph.strategies import run_strategy
    from candlegraph.strategies.store import store_signals

    prices = _prices_for(settings, args.symbol, args.days)
    config = build_strategy_config(args.strategy, args)
    result = run_strategy(args.strategy, prices, config)

    print(
        f"{args.strategy} on {args.symbol}: {len(result.frame)} bars, "
        f"{result.trades()} trades, {result.exposure():.1%} exposure, latest {result.latest.name}"
    )
    tail = result.frame[["price", "score", "signal"]].tail(args.tail)
    print(tail.to_string(float_format=lambda v: f"{v:,.4f}"))

    if args.store:
        print(f"stored {store_signals(get_db(), result, symbol=args.symbol)} rows")
    if args.explain:
        return _explain(settings, args, result)
    return 0


def _explain(settings: Settings, args: argparse.Namespace, result: Any) -> int:
    """The `--explain` half of `signals`: one bar, cited, validated, stored."""
    from datetime import UTC, datetime

    from candlegraph.knowledge.explain import (
        ExplainError,
        explain,
        format_explanation,
        request_for,
        services_for,
    )
    from candlegraph.knowledge.retrieve import RetrievalError
    from candlegraph.storage import get_db
    from candlegraph.tokens import get_token_registry

    registry = get_token_registry()
    token = registry.by_symbol(args.symbol)
    assert token is not None  # _prices_for already refused an unknown symbol
    reference_symbol = args.reference or settings.knowledge_reference_symbol
    reference = None
    if reference_symbol.casefold() != args.symbol.casefold():
        if registry.by_symbol(reference_symbol) is None:
            print(f"reference {reference_symbol!r} is not tracked; no market comparison")
        else:
            try:
                reference = _prices_for(settings, reference_symbol, args.days)
            except SystemExit as exc:
                print(f"no reference prices: {exc}; no market comparison")
    at = datetime.fromisoformat(args.at).astimezone(UTC) if args.at else None

    source, embedder, notes = services_for(settings)
    if args.no_search:
        source = None
    for note in notes:
        print(note)
    try:
        request = request_for(result, token, at=at, reference=reference)
        explanation = explain(get_db(), settings, request, source=source, embedder=embedder)
    except (ExplainError, RetrievalError) as exc:
        raise SystemExit(str(exc)) from exc
    print(format_explanation(explanation))
    return 0


def _backtest(settings: Settings, args: argparse.Namespace) -> int:
    from candlegraph.strategies import available, backtest, compare, format_backtest, run_strategy
    from candlegraph.strategies.backtest import BacktestConfig

    prices = _prices_for(settings, args.symbol, args.days)
    config = BacktestConfig(
        fee_bps=args.fee_bps if args.fee_bps is not None else settings.backtest_fee_bps,
        slippage_bps=(args.slippage_bps if args.slippage_bps is not None else settings.backtest_slippage_bps),
        risk_free_rate=(args.risk_free_rate if args.risk_free_rate is not None else settings.risk_free_rate),
        interval_hours=settings.analytics_interval_hours,
    )

    names = list(available()) if args.strategy == "all" else [args.strategy]
    results = [
        backtest(run_strategy(name, prices, build_strategy_config(name, args)), config=config)
        for name in names
    ]

    if len(results) == 1:
        print(format_backtest(results[0]))
    else:
        table = compare(results)
        print(table.to_string(index=False, float_format=lambda v: f"{v:,.4f}"))
    return 0


def _simulation_config(settings: Settings, args: argparse.Namespace) -> Any:
    """Execution assumptions shared by the real and the simulated runs.

    They have to be the same, or the distribution is not a distribution
    *about* the real backtest.
    """
    from candlegraph.strategies.backtest import BacktestConfig

    return BacktestConfig(
        fee_bps=args.fee_bps if args.fee_bps is not None else settings.backtest_fee_bps,
        slippage_bps=(args.slippage_bps if args.slippage_bps is not None else settings.backtest_slippage_bps),
        risk_free_rate=settings.risk_free_rate,
        interval_hours=settings.analytics_interval_hours,
    )


def _simulate_paths_report(settings: Settings, args: argparse.Namespace) -> int:
    """Monte Carlo: is this edge luck?"""
    from candlegraph.strategies.montecarlo import evaluate_strategy, format_simulation

    prices = _prices_for(settings, args.symbol, args.days)
    summary = evaluate_strategy(
        prices,
        args.strategy,
        n_paths=args.paths,
        method=args.method or DEFAULT_GENERATOR,
        seed=args.seed,
        config=_simulation_config(settings, args),
        strategy_config=build_strategy_config(args.strategy, args),
    )
    print(format_simulation(summary))
    return 0


def _simulate_execution_report(settings: Settings, args: argparse.Namespace) -> int:
    """Discrete event: does the vectorised backtester lie about execution?

    The reconciliation is printed first because everything after it is
    only meaningful if the two engines agree when the assumptions are put
    back.
    """
    from candlegraph.analytics.events import latency_cost, reconcile
    from candlegraph.strategies import run_strategy

    prices = _prices_for(settings, args.symbol, args.days)
    signals = run_strategy(args.strategy, prices, build_strategy_config(args.strategy, args)).signals
    cost_bps = (args.fee_bps if args.fee_bps is not None else settings.backtest_fee_bps) + (
        args.slippage_bps if args.slippage_bps is not None else settings.backtest_slippage_bps
    )

    check = reconcile(prices, signals, latency_bars=0, cost_bps=cost_bps)
    print(f"Execution model: {args.strategy} on {args.symbol}, {cost_bps:.0f} bps per unit")
    print("-" * 64)
    print("Reconciliation against the vectorised backtester (no extra latency):")
    print(f"  max position difference {check.max_position_difference:.2e}")
    print(f"  max equity difference   {check.max_equity_difference:.2e}")
    print(f"  {'AGREES' if check.agrees else 'DISAGREES — one of the two engines is wrong'}")
    print()
    print("What the convenient assumptions are worth:")
    table = latency_cost(prices, signals, latencies=(0, 1, 2, 4), cost_bps=cost_bps, fill_ratios=(1.0, 0.25))
    print(table.to_string(index=False, float_format=lambda v: f"{v:,.4f}"))
    print()
    print(
        "latency_bars is delay on top of the one bar you cannot trade inside; 0 with full "
        "fills is exactly what `candlegraph backtest` assumes."
    )
    return 0


def _simulate(settings: Settings, args: argparse.Namespace) -> int:
    """Two reports behind one verb, because they answer different doubts."""
    from candlegraph.analytics.simulation import SimulationError

    try:
        if args.execution:
            return _simulate_execution_report(settings, args)
        return _simulate_paths_report(settings, args)
    except SimulationError as exc:
        raise SystemExit(str(exc)) from exc


def _portfolio(settings: Settings, args: argparse.Namespace) -> int:
    from candlegraph.analytics.portfolio import (
        PortfolioConfig,
        PortfolioError,
        efficient_frontier,
        format_portfolio,
        optimise_portfolio,
    )
    from candlegraph.analytics.prices import load_price_frame
    from candlegraph.analytics.rolling import log_returns
    from candlegraph.storage import get_db
    from candlegraph.tokens import get_token_registry

    symbols = args.symbols or list(get_token_registry().symbols)
    window = args.days if args.days is not None else settings.analytics_lookback_days
    prices = load_price_frame(get_db(), symbols, days=window)
    if prices.empty:
        raise SystemExit(f"no stored price history for {', '.join(symbols)}")

    returns = prices.apply(log_returns).dropna(how="all")
    config = PortfolioConfig(
        risk_free_rate=(args.risk_free_rate if args.risk_free_rate is not None else settings.risk_free_rate),
        objective=args.objective,
        allow_short=args.allow_short,
        max_weight=args.max_weight,
        target_return=args.target_return,
        interval_hours=settings.analytics_interval_hours,
        shrinkage=args.shrinkage,
    )

    try:
        result = optimise_portfolio(returns, config)
    except PortfolioError as exc:
        raise SystemExit(str(exc)) from exc

    print(f"{len(returns)} aligned bars over {window} days")
    print(format_portfolio(result))
    print(_portfolio_tail_risk(settings, returns, result.weights))

    if args.frontier:
        print()
        print(f"{'return':>10}{'volatility':>12}{'sharpe':>10}")
        for point in efficient_frontier(returns, config, points=args.frontier):
            print(f"{point.expected_return:>9.2%}{point.volatility:>11.2%}{point.sharpe:>10.3f}")
    return 0 if result.converged else 1


def _risk_config(settings: Settings, args: argparse.Namespace) -> Any:
    """The risk knobs: flags when given, settings otherwise."""
    from candlegraph.analytics.risk import RiskConfig, RiskError

    given = {k: v for k, v in vars(args).items() if v is not None}
    try:
        return RiskConfig(
            confidence=given.get("confidence", settings.risk_confidence),
            horizon_bars=given.get("horizon", settings.risk_horizon_bars),
            method=given.get("method", settings.risk_var_method),
            interval_hours=settings.analytics_interval_hours,
            risk_free_rate=given.get("risk_free_rate", settings.risk_free_rate),
        )
    except RiskError as exc:
        raise SystemExit(str(exc)) from exc


def _portfolio_tail_risk(settings: Settings, returns: Any, weights: dict[str, float]) -> str:
    """VaR and CVaR of the solved allocation, in the same units as its volatility."""
    from candlegraph.analytics.risk import (
        RiskConfig,
        RiskError,
        conditional_value_at_risk,
        portfolio_returns,
        value_at_risk,
    )

    held = {k: v for k, v in weights.items() if v != 0.0}
    try:
        config = RiskConfig(
            confidence=settings.risk_confidence,
            horizon_bars=settings.risk_horizon_bars,
            method=settings.risk_var_method,  # type: ignore[arg-type]
            interval_hours=settings.analytics_interval_hours,
            risk_free_rate=settings.risk_free_rate,
        )
        series = portfolio_returns(returns, held)
        var = value_at_risk(series, config)
        cvar = conditional_value_at_risk(series, config)
    except RiskError as exc:
        return f"tail risk: not estimated ({exc})"
    hours = config.horizon_bars * config.interval_hours
    label = f"{hours}h" if hours % 24 else f"{hours // 24}d"
    return (
        f"{f'VaR {config.confidence:.0%} {label}':<10}{var:>9.2%}  ({config.method})\n"
        f"{f'CVaR {config.confidence:.0%} {label}':<10}{cvar:>9.2%}"
    )


def _risk(settings: Settings, args: argparse.Namespace) -> int:
    from candlegraph.analytics.prices import load_price_frame
    from candlegraph.analytics.rates import RateSourceError, rate_provider_for
    from candlegraph.analytics.risk import RiskError, format_risk_report, risk_report
    from candlegraph.storage import get_db
    from candlegraph.tokens import get_token_registry

    registry = get_token_registry()
    if registry.by_symbol(args.symbol) is None:
        raise SystemExit(f"unknown token {args.symbol!r}; tracked: {', '.join(registry.symbols)}")

    benchmark = None if args.no_benchmark else (args.benchmark or settings.risk_benchmark or None)
    if benchmark is not None and (benchmark == args.symbol or registry.by_symbol(benchmark) is None):
        # A token against itself is a tautology, and an untracked benchmark
        # has no history to regress on; the report simply omits the split.
        benchmark = None

    try:
        quote = rate_provider_for(settings).quote()
    except RateSourceError as exc:
        raise SystemExit(str(exc)) from exc
    config = _risk_config(settings, args)

    window = args.days if args.days is not None else settings.analytics_lookback_days
    symbols = [args.symbol] + ([benchmark] if benchmark else [])
    # fill="none": the decomposition aligns on hours both tokens traded.
    frame = load_price_frame(get_db(), symbols, days=window, fill="none")
    if frame.empty or args.symbol not in frame.columns or frame[args.symbol].dropna().empty:
        raise SystemExit(f"no stored price history for {args.symbol} in the last {window} days")
    prices = frame[args.symbol].dropna().rename(args.symbol)
    benchmark_prices = None
    if benchmark and benchmark in frame.columns and not frame[benchmark].dropna().empty:
        benchmark_prices = frame[benchmark].dropna().rename(benchmark)

    try:
        report = risk_report(prices, config, benchmark_prices)
    except RiskError as exc:
        raise SystemExit(str(exc)) from exc

    print(format_risk_report(report))
    print(
        f"risk-free rate: {quote.nominal:.2%} nominal, {quote.real:.2%} real "
        f"(inflation {quote.inflation:.2%}, source: {quote.source})"
    )
    if benchmark_prices is None and not args.no_benchmark:
        print("no benchmark split: set RISK_BENCHMARK or --benchmark to a different tracked token")
    return 0


def _posterior_forecast_report(settings: Settings, args: argparse.Namespace) -> int:
    """`forecast --posterior`: an interval that admits the parameters are estimated.

    Exits non-zero when the chains did not mix. An unconverged sampler
    produces confident nonsense and does not announce itself, so the
    refusal has to be the command's exit code and not only a line of
    prose someone might skim past.
    """
    from candlegraph.analytics.mcmc import MCMCError, format_posterior, posterior_forecast

    prices = _prices_for(settings, args.symbol, args.days)
    try:
        result = posterior_forecast(
            prices,
            steps=args.steps,
            confidence=args.confidence,
            draws=args.draws,
            chains=args.chains,
            seed=args.seed,
        )
    except MCMCError as exc:
        raise SystemExit(str(exc)) from exc

    print(f"{args.symbol}: random walk with drift, on {len(prices)} bars")
    print(format_posterior(result))
    return 0 if result.usable else 1


def _forecast(settings: Settings, args: argparse.Namespace) -> int:
    from candlegraph.analytics.arima import ArimaConfig, fit_forecast

    if args.posterior:
        return _posterior_forecast_report(settings, args)

    prices = _prices_for(settings, args.symbol, args.days)
    config = ArimaConfig(
        order=parse_order(args.order),
        auto_order=args.auto_order,
        forecast_steps=args.steps,
        confidence=args.confidence,
    )
    result = fit_forecast(prices, config)

    if not result.ok:
        print(f"{args.symbol}: no forecast ({result.message})")
        return 1

    print(f"{args.symbol}: ARIMA{result.order} on {len(prices)} bars")
    print(f"{'last price':<22}{result.last_price:>14,.4f}")
    print(f"{f'forecast (+{args.steps}h)':<22}{result.predicted:>14,.4f}")
    print(f"{'expected return':<22}{result.expected_return:>14.4%}")
    print(f"{f'{args.confidence:.0%} interval':<22}{result.lower:>14,.4f} .. {result.upper:,.4f}")
    print(f"{'interval width':<22}{result.interval_width:>14.2%}  of price")
    return 0


# --- agents ----------------------------------------------------------------


def _print_report(report: Any, as_json: bool) -> None:
    from candlegraph.agents.reporting import report_json

    print(report_json(report) if as_json else report.markdown)


def _agent_exit(report: Any, strict: bool) -> int:
    from candlegraph.agents.base import Severity

    if report.error:
        return 2
    return 1 if strict and report.at_least(Severity.WARNING) else 0


def _sink_names(text: str | None) -> list[str] | None:
    if text is None:
        return None
    return [name.strip() for name in text.split(",") if name.strip()]


def _observe(settings: Settings, args: argparse.Namespace) -> int:
    from candlegraph.agents.reporting import build_sinks
    from candlegraph.harness import JOBS, run_job
    from candlegraph.harness.runs import MANUAL
    from candlegraph.storage import get_db

    db = get_db()
    sinks = build_sinks(settings, db=db, names=_sink_names(args.sinks))
    # stderr so `--json` stays machine-readable on stdout.
    on_stage = _stage_printer("observer") if args.progress else None
    run = run_job(
        JOBS["observer"], settings, trigger=MANUAL, db=db, params={"sinks": sinks}, on_stage=on_stage
    )
    if run.payload is None:
        return 2
    report = run.payload
    _print_report(report, args.json)
    return _agent_exit(report, args.strict)


def _janitor(settings: Settings, args: argparse.Namespace) -> int:
    from candlegraph.agents.reporting import build_sinks
    from candlegraph.agents.shell import SubprocessShell
    from candlegraph.harness import JOBS, run_job
    from candlegraph.harness.runs import MANUAL
    from candlegraph.storage import get_db

    names = _sink_names(args.sinks)
    chosen = tuple(names) if names is not None else settings.report_sinks
    shell = SubprocessShell()
    db = get_db() if "ledger" in chosen else None
    sinks = build_sinks(settings, db=db, shell=shell, names=chosen)
    checks = tuple(_sink_names(args.checks) or ()) or None
    run = run_job(
        JOBS["janitor"],
        settings,
        trigger=MANUAL,
        params={"shell": shell, "checks": checks, "open_pr": args.open_pr, "sinks": sinks},
    )
    if run.payload is None:
        return 2
    report = run.payload
    _print_report(report, args.json)
    return _agent_exit(report, args.strict)


def _agents(settings: Settings, names: str | None = None) -> int:
    from candlegraph.harness.process import agents_banner, run_process

    try:
        jobs = _selected_jobs("agents", names)
    except ValueError as exc:
        service_logger.error("%s", exc)
        return 2
    run_process(settings, jobs, name="Agents", banner=agents_banner(settings, jobs))
    return 0


def _agent_runs(args: argparse.Namespace) -> int:
    from candlegraph.agents.ledger import recent_agent_runs
    from candlegraph.storage import get_db

    rows = recent_agent_runs(get_db(), args.limit, args.agent)
    if not rows:
        print("no agent runs recorded yet")
        return 0
    if args.show:
        print(rows[0]["report"])
        return 0
    print(f"{'finished':<21}{'agent':<10}{'status':<10}{'findings':>9}{'secs':>7}  summary")
    for row in rows:
        duration = row["duration_seconds"]
        elapsed = f"{float(duration):>7.1f}" if duration is not None else f"{'-':>7}"
        summary = str(row["summary"]).splitlines()[0] if row["summary"] else ""
        print(
            f"{row['finished_at']:%Y-%m-%d %H:%M:%S}  {row['agent']:<10}{row['status']:<10}"
            f"{row['findings_total']:>9}{elapsed}  {summary[:70]}"
        )
    return 0


def _mcp(args: argparse.Namespace) -> int:
    """Serve the tools. Blocks until the client disconnects or the process stops."""
    try:
        from candlegraph.mcp.server import run_server
    except ImportError:
        raise SystemExit(
            "the MCP server needs the `mcp` extra. Install it with `uv sync --extra mcp`, "
            "or run it with `make mcp`."
        ) from None

    # Nothing may write to stdout under the stdio transport: it is the
    # protocol channel. `configure_logging` attaches a stderr handler and no
    # tool prints, so there is nothing further to redirect here.
    run_server(transport=args.transport, host=args.host, port=args.port)
    return 0


def _knowledge(settings: Settings, args: argparse.Namespace) -> int:
    from candlegraph.knowledge.assets import sync_assets
    from candlegraph.knowledge.embed import EmbeddingError, build_embedder, embed_pending, embedding_backlog
    from candlegraph.storage import get_db
    from candlegraph.tokens import get_token_registry

    match args.knowledge_command:
        case "sync-assets":
            written = sync_assets(get_db(), get_token_registry())
            print(f"synced {written} assets from the token registry")
            return 0
        case "fetch-news":
            from candlegraph.knowledge.news import NewsError, run_news_fetch

            try:
                fetched = run_news_fetch(get_db(), settings, symbols=args.symbol)
            except NewsError as exc:
                raise SystemExit(str(exc)) from exc
            print(f"{'token':<8}{'status':<16}{'credits':>8}{'results':>8}{'docs':>6}{'chunks':>7}  note")
            for o in fetched.outcomes:
                print(
                    f"{o.symbol:<8}{o.status:<16}{o.credits:>8}{o.results:>8}"
                    f"{o.documents_stored:>6}{o.chunks_written:>7}  {o.message[:60]}"
                )
            print(
                f"spent {fetched.credits_spent} credits, stored {fetched.documents_stored} documents; "
                "run `candlegraph knowledge embed` to vectorise the new chunks"
            )
            return 0 if fetched.ok else 1
        case "search":
            from datetime import UTC, datetime

            from candlegraph.knowledge.embed import EmbeddingError, build_embedder
            from candlegraph.knowledge.retrieve import (
                RetrievalConfig,
                RetrievalError,
                check_backend,
                format_results,
                retrieve,
            )

            top_k = args.top_k or settings.retrieval_top_k
            try:
                check_backend(settings.retrieval_backend)
                config = RetrievalConfig(
                    top_k=top_k,
                    candidates=max(settings.retrieval_candidates, top_k),
                    mode=args.mode,
                    include_undated=args.include_undated,
                )
            except RetrievalError as exc:
                raise SystemExit(str(exc)) from exc
            bar = datetime.fromisoformat(args.at).astimezone(UTC) if args.at else datetime.now(tz=UTC)
            asset_id = None
            terms: list[str] = []
            if args.symbol:
                registry = get_token_registry()
                token = registry.by_symbol(args.symbol)
                if token is None:
                    raise SystemExit(f"unknown token {args.symbol!r}; tracked: {', '.join(registry.symbols)}")
                asset_id = token.address
                terms = list(token.aliases) or [token.symbol]
            embedder = None
            if not args.lexical_only:
                try:
                    embedder = build_embedder(settings)
                except EmbeddingError as exc:
                    print(f"no embedder: {exc}; lexical only")
            result = retrieve(
                get_db(),
                args.query,
                bar=bar,
                embedder=embedder,
                terms=terms,
                asset_id=asset_id,
                config=config,
            )
            print(format_results(result))
            return 0
        case "link-mentions":
            from candlegraph.knowledge.mentions import alias_dictionary, link_unlinked_documents

            registry = get_token_registry()
            db = get_db()
            sync_assets(db, registry)
            documents, mentions = link_unlinked_documents(db, alias_dictionary(registry))
            print(f"linked {documents} documents, {mentions} new mentions")
            return 0
        case "embed" | "reembed":
            db = get_db()
            try:
                embedder = build_embedder(settings)
                before = embedding_backlog(db, embedder.name)
                print(
                    f"{embedder.name} ({settings.embedding_provider}): "
                    f"{before.pending} of {before.total} chunks to embed "
                    f"({before.missing} missing, {before.stale} from another model)"
                )
                summary = embed_pending(
                    db,
                    embedder,
                    batch_size=args.batch_size or settings.embedding_batch_size,
                    limit=args.limit,
                    dimensions=settings.embedding_dimensions,
                )
            except EmbeddingError as exc:
                raise SystemExit(str(exc)) from exc
            print(
                f"embedded {summary.embedded} chunks in {summary.batches} batches; "
                f"{summary.remaining} remaining"
            )
            return 0
        case _:  # pragma: no cover - argparse rejects anything else
            raise SystemExit(f"unknown knowledge command {args.knowledge_command!r}")


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and dispatch. Returns the process exit code."""
    args = _parser().parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)

    match args.command:
        case "serve":
            return _serve(settings, args.host, args.port)
        case "worker":
            return _worker(settings, args.jobs)
        case "jobs":
            return _jobs(settings)
        case "ingest":
            return _ingest(settings, None, "manual", args.progress)
        case "backfill":
            return _ingest(settings, args.days, "backfill", args.progress)
        case "report":
            return _report(settings, args.days, args.strict, args.json)
        case "runs":
            if args.job or args.all:
                return _run_log(args.limit, args.job)
            return _runs(args.limit)
        case "fill":
            return _fill(settings, args)
        case "simulate":
            return _simulate(settings, args)
        case "strategies":
            return _strategies_cmd()
        case "ui":
            return _ui(settings, args)
        case "stats":
            return _stats(settings, args)
        case "compare":
            return _compare(settings, args)
        case "signals":
            return _signals(settings, args)
        case "backtest":
            return _backtest(settings, args)
        case "portfolio":
            return _portfolio(settings, args)
        case "forecast":
            return _forecast(settings, args)
        case "risk":
            return _risk(settings, args)
        case "observe":
            return _observe(settings, args)
        case "janitor":
            return _janitor(settings, args)
        case "agents":
            return _agents(settings, args.jobs)
        case "agent-runs":
            return _agent_runs(args)
        case "mcp":
            return _mcp(args)
        case "knowledge":
            return _knowledge(settings, args)
        case _:  # pragma: no cover - argparse rejects anything else
            raise SystemExit(f"unknown command {args.command!r}")
