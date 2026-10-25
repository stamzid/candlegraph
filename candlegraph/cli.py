"""Command-line entrypoint: ``candlegraph <command>``.

One binary, several roles. The API server and the ingest worker are separate
processes in the compose stack, and the one-shot commands (``backfill``,
``report``) are what CI and an operator use without starting either.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

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

    return parser


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
        case _:  # pragma: no cover - argparse rejects anything else
            raise SystemExit(f"unknown command {args.command!r}")
