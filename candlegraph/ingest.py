"""Orchestration of a single ingest run: cursors in, rows written out.

A run is idempotent. Cursors come from what is already stored, writes upsert on
``(token_id, period_start_unix)``, and a token that fails is reported rather
than aborting the other tokens.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from candlegraph.dba import DatabaseManager
from candlegraph.helpers import prepare_hour_rows
from candlegraph.settings import Settings
from candlegraph.subgraph_client import SubgraphClient, SubgraphError
from candlegraph.tokens import TokenRegistry
from candlegraph.utils.logging_utils import service_logger


@dataclass
class IngestResult:
    """What one run of :func:`ingest_once` did."""

    tokens_written: int = 0
    hours_written: int = 0
    rows_pruned: int = 0
    hours_by_symbol: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True when no token failed."""
        return not self.errors


def lookback_start(lookback_days: int, now: datetime | None = None) -> int:
    """The unix second ``lookback_days`` before ``now`` (default: current UTC)."""
    moment = now or datetime.now(tz=UTC)
    return int((moment - timedelta(days=lookback_days)).timestamp())


def resolve_cursors(
    db: DatabaseManager,
    registry: TokenRegistry,
    lookback_days: int,
    now: datetime | None = None,
) -> dict[str, int]:
    """Per-token resume points: the newest stored hour, else the lookback start.

    Tokens with stored rows resume from the latest hour already written, so a
    restart re-fetches nothing. Tokens with no rows backfill the full window.
    """
    start = lookback_start(lookback_days, now)
    stored = {row["token_id"]: int(row["latest_unix"]) for row in db.fetch_all(db.sql.get_latest_timestamps)}
    return {address: stored.get(address, start) for address in registry.addresses}


def ingest_token_metadata(db: DatabaseManager, client: SubgraphClient, registry: TokenRegistry) -> int:
    """Refresh the ``token`` table. Returns the number of rows written."""
    rows = client.fetch_tokens(list(registry.addresses))
    if not rows:
        service_logger.warning("Subgraph returned no token metadata for %d tokens", len(registry))
        return 0
    return db.upsert_many(db.sql.insert_token, db.sql.insert_token_template, rows)


def ingest_token_hours(
    db: DatabaseManager,
    client: SubgraphClient,
    registry: TokenRegistry,
    token_id: str,
    since: int,
) -> int:
    """Page through one token's hourly rows from ``since``, writing each page."""
    symbol_by_address = registry.symbol_by_address
    written = 0
    for page in client.iter_token_hour_datas(token_id, since):
        records = prepare_hour_rows(page.rows, symbol_by_address)
        if records:
            written += db.upsert_many(db.sql.insert_token_hours, db.sql.insert_token_hours_template, records)
    return written


def ingest_once(
    db: DatabaseManager,
    client: SubgraphClient,
    registry: TokenRegistry,
    settings: Settings,
    now: datetime | None = None,
) -> IngestResult:
    """Run one full ingest pass over every configured token."""
    result = IngestResult()

    try:
        result.tokens_written = ingest_token_metadata(db, client, registry)
    except SubgraphError as exc:
        service_logger.error("Token metadata refresh failed: %s", exc)
        result.errors["__metadata__"] = str(exc)

    cursors = resolve_cursors(db, registry, settings.lookback_days, now)
    for token in registry:
        try:
            written = ingest_token_hours(db, client, registry, token.address, cursors[token.address])
        except SubgraphError as exc:
            service_logger.error("Ingest failed for %s: %s", token.symbol, exc)
            result.errors[token.symbol] = str(exc)
            continue
        result.hours_by_symbol[token.symbol] = written
        result.hours_written += written
        service_logger.info("Ingested %d hourly rows for %s", written, token.symbol)

    if not settings.retain_history:
        cutoff = lookback_start(settings.lookback_days, now)
        result.rows_pruned = db.execute(db.sql.delete_older_data, {"interval_start": cutoff})
        service_logger.info(
            "RETAIN_HISTORY is off: pruned %d rows older than %d days",
            result.rows_pruned,
            settings.lookback_days,
        )

    service_logger.info(
        "Ingest run finished: %d token rows, %d hourly rows, %d errors",
        result.tokens_written,
        result.hours_written,
        len(result.errors),
    )
    return result


def run_ingest(
    db: DatabaseManager,
    registry: TokenRegistry,
    settings: Settings,
    now: datetime | None = None,
) -> IngestResult:
    """Build a subgraph client for this run and ingest with it."""
    with SubgraphClient(settings) as client:
        return ingest_once(db, client, registry, settings, now)
