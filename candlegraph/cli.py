"""Command-line entrypoint: ``candlegraph <command>``.

One binary, several roles. The API server, the ingest worker and the UI are
separate processes in the compose stack; the one-shot commands (``backfill``,
``report``, ``signals``, ``backtest``, ``portfolio``) are what CI and an
operator use without starting any of them.

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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="candlegraph", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="run the GraphQL API server")
    sub.add_parser("worker", help="run the ingest worker loop")
    sub.add_parser("ingest", help="run one ingest pass and exit")

    backfill = sub.add_parser("backfill", help="run one ingest pass over a longer window")
    backfill.add_argument("--days", type=int, default=90, help="how far back to reach (default: 90)")

    report = sub.add_parser("report", help="print the data-quality report")
    report.add_argument("--days", type=int, default=None, help="only consider the last N days")
    report.add_argument("--strict", action="store_true", help="exit non-zero when the report is not clean")

    runs = sub.add_parser("runs", help="show recent ingest runs from the ledger")
    runs.add_argument("--limit", type=int, default=10)

    sub.add_parser("strategies", help="list the available strategies")

    ui = sub.add_parser("ui", help="run the Streamlit UI")
    ui.add_argument("--host", default=None)
    ui.add_argument("--port", type=int, default=None)

    stats = sub.add_parser("stats", help="rolling mean and volatility for a token")
    stats.add_argument("symbol")
    _add_window(stats)
    stats.add_argument("--window", type=int, default=None, help="rolling lookback in bars")

    signals = sub.add_parser("signals", help="generate signals for a token")
    signals.add_argument("symbol")
    signals.add_argument("--strategy", default="bollinger", help="strategy name")
    _add_window(signals)
    signals.add_argument("--store", action="store_true", help="write the signals to the database")
    signals.add_argument("--tail", type=int, default=10, help="rows to print")
    _add_strategy_flags(signals)

    bt = sub.add_parser("backtest", help="backtest a strategy over stored history")
    bt.add_argument("symbol")
    bt.add_argument("--strategy", default="bollinger", help="strategy name, or 'all'")
    _add_window(bt)
    bt.add_argument("--fee-bps", type=float, default=None)
    bt.add_argument("--slippage-bps", type=float, default=None)
    bt.add_argument("--risk-free-rate", type=float, default=None)
    _add_strategy_flags(bt)

    portfolio = sub.add_parser("portfolio", help="mean-variance optimise across tokens")
    portfolio.add_argument(
        "--symbols", nargs="*", default=None, help="tokens to allocate across (default: all tracked)"
    )
    _add_window(portfolio)
    portfolio.add_argument("--risk-free-rate", type=float, default=None, help="annual, e.g. 0.04")
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

    forecast = sub.add_parser("forecast", help="ARIMA forecast for a token")
    forecast.add_argument("symbol")
    _add_window(forecast)
    forecast.add_argument("--order", default="1,1,1", help="p,d,q")
    forecast.add_argument("--auto-order", action="store_true", help="pick p and q by criterion")
    forecast.add_argument("--steps", type=int, default=1, help="periods ahead")
    forecast.add_argument("--confidence", type=float, default=0.95)

    return parser


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


def _serve(settings: Settings) -> int:
    import uvicorn

    service_logger.info("Starting candlegraph API on %s:%s", settings.host, settings.port)
    uvicorn.run(
        "candlegraph.api.app:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )
    return 0


def _worker(settings: Settings) -> int:
    from candlegraph.ingestion.worker import run_worker

    run_worker(settings)
    return 0


def _ingest(settings: Settings, days: int | None, trigger: str) -> int:
    from candlegraph.ingestion.runner import run_ingest
    from candlegraph.storage import get_db
    from candlegraph.tokens import get_token_registry

    result = run_ingest(get_db(), get_token_registry(), settings, trigger=trigger, lookback_days=days)
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


def _report(settings: Settings, days: int | None, strict: bool) -> int:
    from candlegraph.ingestion.quality import build_report, format_report
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
    print(format_report(report))
    return 0 if report.ok or not strict else 1


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
    from candlegraph.analytics.prices import load_price_frame
    from candlegraph.storage import get_db
    from candlegraph.tokens import get_token_registry

    registry = get_token_registry()
    if registry.by_symbol(symbol) is None:
        raise SystemExit(f"unknown token {symbol!r}; tracked: {', '.join(registry.symbols)}")

    window = days if days is not None else settings.analytics_lookback_days
    frame = load_price_frame(get_db(), [symbol], days=window)
    if frame.empty or symbol not in frame.columns:
        raise SystemExit(f"no stored price history for {symbol} in the last {window} days")

    series = frame[symbol].dropna()
    series.name = symbol
    return series


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

    if args.frontier:
        print()
        print(f"{'return':>10}{'volatility':>12}{'sharpe':>10}")
        for point in efficient_frontier(returns, config, points=args.frontier):
            print(f"{point.expected_return:>9.2%}{point.volatility:>11.2%}{point.sharpe:>10.3f}")
    return 0 if result.converged else 1


def _forecast(settings: Settings, args: argparse.Namespace) -> int:
    from candlegraph.analytics.arima import ArimaConfig, fit_forecast

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


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and dispatch. Returns the process exit code."""
    args = _parser().parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)

    match args.command:
        case "serve":
            return _serve(settings)
        case "worker":
            return _worker(settings)
        case "ingest":
            return _ingest(settings, None, "manual")
        case "backfill":
            return _ingest(settings, args.days, "backfill")
        case "report":
            return _report(settings, args.days, args.strict)
        case "runs":
            return _runs(args.limit)
        case "strategies":
            return _strategies_cmd()
        case "ui":
            return _ui(settings, args)
        case "stats":
            return _stats(settings, args)
        case "signals":
            return _signals(settings, args)
        case "backtest":
            return _backtest(settings, args)
        case "portfolio":
            return _portfolio(settings, args)
        case "forecast":
            return _forecast(settings, args)
        case _:  # pragma: no cover - argparse rejects anything else
            raise SystemExit(f"unknown command {args.command!r}")
