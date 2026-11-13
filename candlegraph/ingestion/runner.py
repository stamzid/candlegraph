"""Orchestration of a single ingest run: cursors in, rows written out.

A run is idempotent. Cursors come from what is already stored, writes upsert
on the natural key, and a token or pool that fails is reported rather than
aborting the rest. Nothing is ever deleted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from candlegraph.harness.stages import StageHook, StageRecord, timed_stage
from candlegraph.ingestion import ledger
from candlegraph.ingestion.subgraph import SubgraphClient, SubgraphError
from candlegraph.ingestion.transform import (
    RowFormatError,
    prepare_hour_rows,
    prepare_pool_hour_rows,
    prepare_pool_rows,
    prepare_token_rows,
)
from candlegraph.settings import Settings
from candlegraph.storage import DatabaseManager
from candlegraph.tokens import TokenRegistry
from candlegraph.utils.logging_utils import service_logger

if TYPE_CHECKING:
    from candlegraph.harness.jobs import JobContext
    from candlegraph.harness.runs import RunOutcome

# Errors a single target can fail with without taking the run down.
TARGET_ERRORS = (SubgraphError, RowFormatError)


@dataclass
class IngestResult:
    """What one run of :func:`ingest_once` did."""

    tokens_written: int = 0
    hours_written: int = 0
    pools_written: int = 0
    pool_hours_written: int = 0
    hours_by_symbol: dict[str, int] = field(default_factory=dict)
    pool_hours_by_pool: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    run_id: str | None = None
    stages: list[StageRecord] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when nothing failed."""
        return not self.errors

    @property
    def rows_written(self) -> int:
        """Every row this run wrote, across all tables."""
        return self.tokens_written + self.hours_written + self.pools_written + self.pool_hours_written

    def counts(self) -> dict[str, int]:
        """The shape the ledger stores."""
        return {
            "tokens_written": self.tokens_written,
            "token_hours_written": self.hours_written,
            "pools_written": self.pools_written,
            "pool_hours_written": self.pool_hours_written,
        }


def lookback_start(lookback_days: int, now: datetime | None = None) -> int:
    """The unix second ``lookback_days`` before ``now`` (default: current UTC)."""
    moment = now or datetime.now(tz=UTC)
    return int((moment - timedelta(days=lookback_days)).timestamp())


def _cursor_for(latest: int | None, earliest: int | None, start: int, reach_back: bool) -> int:
    """Where to resume fetching for one entity.

    Normally: the newest hour already stored, so a restart re-fetches nothing.

    For a backfill that reaches further back than the stored history, start at
    the requested window instead — resuming from the newest row would only
    ever walk forwards and the older hours would never arrive. Re-reading the
    overlap is cheap and the upsert makes it a no-op.
    """
    if latest is None:
        return start
    if reach_back and earliest is not None and start < earliest:
        return start
    return latest


def _cursors(
    stored: dict[str, tuple[int | None, int | None]],
    ids: tuple[str, ...],
    lookback_days: int,
    now: datetime | None,
    reach_back: bool = False,
) -> dict[str, int]:
    start = lookback_start(lookback_days, now)
    return {
        entity_id: _cursor_for(*stored.get(entity_id, (None, None)), start, reach_back) for entity_id in ids
    }


def _stored_bounds(rows: list[dict[str, Any]], key: str) -> dict[str, tuple[int | None, int | None]]:
    return {
        row[key]: (
            int(row["latest_unix"]) if row["latest_unix"] is not None else None,
            int(row["earliest_unix"]) if row.get("earliest_unix") is not None else None,
        )
        for row in rows
    }


def resolve_cursors(
    db: DatabaseManager,
    registry: TokenRegistry,
    lookback_days: int,
    now: datetime | None = None,
    reach_back: bool = False,
) -> dict[str, int]:
    """Per-token resume points: the newest stored hour, else the lookback start.

    Tokens with stored rows resume from the latest hour already written, so a
    restart re-fetches nothing. Tokens with no rows fetch the full window.
    ``reach_back`` additionally rewinds to the window start when the request
    reaches further back than the stored history.
    """
    stored = _stored_bounds(db.fetch_all(db.sql.get_latest_timestamps), "token_id")
    return _cursors(stored, registry.active_addresses, lookback_days, now, reach_back)


def resolve_pool_cursors(
    db: DatabaseManager,
    pool_ids: tuple[str, ...],
    lookback_days: int,
    now: datetime | None = None,
    reach_back: bool = False,
) -> dict[str, int]:
    """Per-pool resume points, same rule as tokens."""
    stored = _stored_bounds(db.fetch_all(db.sql.get_latest_pool_timestamps), "pool_id")
    return _cursors(stored, pool_ids, lookback_days, now, reach_back)


# --- individual targets ----------------------------------------------------


def ingest_token_metadata(
    db: DatabaseManager, client: SubgraphClient, registry: TokenRegistry, now: datetime | None = None
) -> int:
    """Refresh the ``token`` table. Returns the number of rows written."""
    rows = client.fetch_tokens(list(registry.active_addresses))
    if not rows:
        service_logger.warning("Subgraph returned no token metadata for %d tokens", len(registry.active))
        return 0
    prepared = prepare_token_rows(rows, now)
    return db.upsert_many(db.sql.insert_token, db.sql.insert_token_template, prepared)


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


def discover_pools(
    db: DatabaseManager,
    client: SubgraphClient,
    registry: TokenRegistry,
    pools_per_token: int,
    now: datetime | None = None,
) -> int:
    """Find and store the deepest pools for the tracked tokens."""
    rows = client.fetch_pools_for_tokens(list(registry.active_addresses), pools_per_token)
    prepared = prepare_pool_rows(rows, now)
    if not prepared:
        service_logger.warning("Pool discovery returned nothing for %d tokens", len(registry.active))
        return 0
    return db.upsert_many(db.sql.insert_pool, db.sql.insert_pool_template, prepared)


def tracked_pool_ids(db: DatabaseManager, registry: TokenRegistry) -> tuple[str, ...]:
    """Stored pools that hold at least one tracked token."""
    rows = db.fetch_all(db.sql.get_pools_for_tokens, {"token_ids": list(registry.active_addresses)})
    return tuple(row["id"] for row in rows)


def ingest_pool_hours(db: DatabaseManager, client: SubgraphClient, pool_id: str, since: int) -> int:
    """Page through one pool's hourly rows from ``since``, writing each page."""
    written = 0
    for page in client.iter_pool_hour_datas(pool_id, since):
        records = prepare_pool_hour_rows(page.rows, pool_id)
        if records:
            written += db.upsert_many(db.sql.insert_pool_hours, db.sql.insert_pool_hours_template, records)
    return written


# --- the run ---------------------------------------------------------------


def ingest_once(
    db: DatabaseManager,
    client: SubgraphClient,
    registry: TokenRegistry,
    settings: Settings,
    now: datetime | None = None,
    lookback_days: int | None = None,
    on_stage: StageHook | None = None,
) -> IngestResult:
    """Run one full ingest pass over every configured token and tracked pool.

    The pass has four stages — ``token_metadata``, ``token_hours``,
    ``pool_discovery``, ``pool_hours`` — each recorded as a
    :class:`StageRecord` in ``result.stages`` and passed to ``on_stage``.
    With pools off, the two pool stages are recorded as ``skipped``.

    Args:
        lookback_days: Overrides ``settings.lookback_days`` for this run, which
            is how ``candlegraph backfill --days`` reaches further back without
            changing the deployment's configuration.
        on_stage: Called after each stage (``ingest --progress``).
    """
    result = IngestResult()
    # An explicit window is a backfill request, which is the only case where
    # we deliberately rewind past what is already stored.
    reach_back = lookback_days is not None
    window = lookback_days if lookback_days is not None else settings.lookback_days

    with timed_stage("token_metadata", on_stage, result.stages) as detail:
        try:
            result.tokens_written = ingest_token_metadata(db, client, registry, now)
        except TARGET_ERRORS as exc:
            service_logger.error("Token metadata refresh failed: %s", exc)
            result.errors["__metadata__"] = str(exc)
            detail["errors"] = 1
        detail["tokens"] = result.tokens_written

    with timed_stage("token_hours", on_stage, result.stages) as detail:
        cursors = resolve_cursors(db, registry, window, now, reach_back)
        failed = 0
        # The active ones only, and the same set `resolve_cursors` built from:
        # iterating the whole registry here would look up a cursor that was
        # never created for a token nobody is polling.
        for token in registry.active:
            try:
                written = ingest_token_hours(db, client, registry, token.address, cursors[token.address])
            except TARGET_ERRORS as exc:
                service_logger.error("Ingest failed for %s: %s", token.symbol, exc)
                result.errors[token.symbol] = str(exc)
                failed += 1
                continue
            result.hours_by_symbol[token.symbol] = written
            result.hours_written += written
            service_logger.info("Ingested %d hourly rows for %s", written, token.symbol)
        detail.update(rows=result.hours_written, **{"by_symbol": dict(result.hours_by_symbol)})
        if failed:
            detail["errors"] = failed

    if settings.ingest_pools:
        _ingest_pools(db, client, registry, settings, result, window, now, reach_back, on_stage)
    else:
        for name in ("pool_discovery", "pool_hours"):
            record = StageRecord(name, "skipped", None, None, {"reason": "INGEST_POOLS is off"})
            result.stages.append(record)
            if on_stage is not None:
                on_stage(record)

    # Nothing is ever deleted. Stored history is the input to the analytics and
    # backtesting phases, so a poll must only ever add to it.
    service_logger.info(
        "Ingest run finished: %d token rows, %d token hours, %d pools, %d pool hours, %d errors",
        result.tokens_written,
        result.hours_written,
        result.pools_written,
        result.pool_hours_written,
        len(result.errors),
    )
    return result


def _ingest_pools(
    db: DatabaseManager,
    client: SubgraphClient,
    registry: TokenRegistry,
    settings: Settings,
    result: IngestResult,
    window: int,
    now: datetime | None,
    reach_back: bool = False,
    on_stage: StageHook | None = None,
) -> None:
    """Discover pools, then ingest each one's hourly rows into ``result``."""
    with timed_stage("pool_discovery", on_stage, result.stages) as detail:
        if settings.pools_per_token:
            try:
                result.pools_written = discover_pools(db, client, registry, settings.pools_per_token, now)
            except TARGET_ERRORS as exc:
                service_logger.error("Pool discovery failed: %s", exc)
                result.errors["__pools__"] = str(exc)
                detail["errors"] = 1
        detail["pools"] = result.pools_written

    with timed_stage("pool_hours", on_stage, result.stages) as detail:
        pool_ids = tracked_pool_ids(db, registry)
        detail["pools"] = len(pool_ids)
        if not pool_ids:
            detail["rows"] = 0
            return

        failed = 0
        pool_cursors = resolve_pool_cursors(db, pool_ids, window, now, reach_back)
        for pool_id in pool_ids:
            try:
                written = ingest_pool_hours(db, client, pool_id, pool_cursors[pool_id])
            except TARGET_ERRORS as exc:
                service_logger.error("Ingest failed for pool %s: %s", pool_id, exc)
                result.errors[f"pool:{pool_id}"] = str(exc)
                failed += 1
                continue
            result.pool_hours_by_pool[pool_id] = written
            result.pool_hours_written += written
        detail["rows"] = result.pool_hours_written
        if failed:
            detail["errors"] = failed

        service_logger.info(
            "Ingested %d hourly rows across %d pools", result.pool_hours_written, len(pool_ids)
        )


def ingest_job_body(ctx: JobContext) -> RunOutcome:
    """The ``ingest`` job: build a subgraph client and run one pass.

    The ``ingest_run`` row is written by the harness's ``IngestLedgerHook``,
    not here. ``ctx.params`` may carry ``lookback_days`` (a backfill),
    ``registry`` and ``now`` (both for tests and direct callers).
    """
    from candlegraph.harness.runs import Outcome, RunOutcome
    from candlegraph.tokens import get_token_registry

    registry = ctx.params.get("registry") or get_token_registry()
    with SubgraphClient(ctx.settings) as client:
        result = ingest_once(
            ctx.db,
            client,
            registry,
            ctx.settings,
            ctx.params.get("now"),
            ctx.params.get("lookback_days"),
            on_stage=ctx.on_stage,
        )
    result.run_id = ctx.run.run_id
    status = ledger.status_for(result.errors, result.rows_written > 0)
    return RunOutcome(
        outcome=Outcome(status),
        counts=result.counts(),
        errors=dict(result.errors),
        detail={"hours_by_symbol": result.hours_by_symbol, "pool_hours_by_pool": result.pool_hours_by_pool},
        payload=result,
    )


def run_ingest(
    db: DatabaseManager,
    registry: TokenRegistry,
    settings: Settings,
    now: datetime | None = None,
    trigger: str = ledger.SCHEDULED,
    lookback_days: int | None = None,
    parent_run_id: str | None = None,
    on_stage: StageHook | None = None,
) -> IngestResult:
    """One ingest run through the harness, for callers that want the result or the exception.

    Records the run in ``ingest_run`` (via the harness's ledger hook). Unlike
    :func:`candlegraph.harness.run_job`, this re-raises when the run could not
    start at all (bad configuration, no endpoint), after the row is closed as
    ``failed`` — the behaviour callers had before the harness existed.
    """
    from candlegraph.harness import JOBS, run_job

    params: dict[str, Any] = {"registry": registry, "now": now, "lookback_days": lookback_days}
    clock = (lambda: now) if now is not None else None
    run = run_job(
        JOBS["ingest"],
        settings,
        trigger=trigger,
        params=params,
        parent_run_id=parent_run_id,
        db=db,
        clock=clock,
        on_stage=on_stage,
    )
    if run.exception is not None:
        raise run.exception
    result: IngestResult = run.payload
    return result
