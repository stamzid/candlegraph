"""What ``GET /status`` answers, as a plain function.

The health check has two callers now: the HTTP endpoint in
:mod:`candlegraph.api.app` and the ``status`` MCP tool. The body is built
here so there is one definition of "what state is this thing in" rather
than two that drift (``docs/knowledge/mcp/DESIGN.md``: the server wraps the
functions that exist and adds no logic of its own).

One database round trip, not two: the freshness read *is* the liveness
probe (invariant 25).
"""

from __future__ import annotations

from typing import Any

import psycopg2

from candlegraph.ingestion.ledger import seconds_since_last_success
from candlegraph.settings import Settings
from candlegraph.storage import DatabaseManager
from candlegraph.tokens import TokenRegistry, get_token_registry
from candlegraph.utils.logging_utils import service_logger

# Data is called stale after this many missed poll intervals.
STALE_AFTER_INTERVALS = 3


def status_body(
    db: DatabaseManager, settings: Settings, registry: TokenRegistry | None = None
) -> tuple[dict[str, Any], bool]:
    """The status payload and whether the database answered.

    Args:
        db: The database; read only, one query.
        settings: For the poll interval staleness is measured against.
        registry: The tracked tokens. Defaults to the process-wide registry.

    Returns:
        ``(body, database_up)``. The caller decides what a down database
        means for it: the endpoint returns 503, the MCP tool just reports it.
        Staleness is never fatal — the API serving stale data is still the
        API working, and alerting on it is the observer's job.
    """
    try:
        age: float | None = seconds_since_last_success(db)
        database_up = True
    except psycopg2.Error as exc:
        service_logger.warning("Database unreachable from /status: %s", exc)
        age, database_up = None, False

    body: dict[str, Any] = {
        "status": "ok" if database_up else "degraded",
        "database": "up" if database_up else "down",
        "ingest": "enabled" if settings.ingest_enabled else "disabled",
        "tokens": list((registry or get_token_registry()).symbols),
    }

    if database_up:
        body["last_ingest_seconds_ago"] = None if age is None else round(age)
        body["data_fresh"] = age is not None and age <= settings.data_poll_interval * STALE_AFTER_INTERVALS

    return body, database_up
