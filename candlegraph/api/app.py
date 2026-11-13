"""FastAPI application: GraphQL plus a health check.

Ingestion normally runs in its own process (``candlegraph worker``); in the
compose stack the API sets ``INGEST_ENABLED=false`` and the worker does the
work. The in-process scheduler is kept for single-process deployments and for
``make up`` on a laptop. It is the harness's one scheduler builder hosting the
``ingest`` job alone, so there is only one definition of the job and of its
schedule (``docs/harness/runner/DESIGN.md``).

Either way the API comes up and stays up when the subgraph is unreachable: an
upstream outage degrades the data, not the service.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from apscheduler.schedulers.base import BaseScheduler
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from strawberry.fastapi import GraphQLRouter

from candlegraph.api.schema import chart_schema
from candlegraph.api.status import STALE_AFTER_INTERVALS, status_body
from candlegraph.harness.jobs import select
from candlegraph.harness.scheduler import build_scheduler
from candlegraph.settings import Settings, get_settings
from candlegraph.storage import get_db, set_db
from candlegraph.utils.logging_utils import configure_logging, quiet_health_probes, service_logger

__all__ = ["INGEST_JOBS", "STALE_AFTER_INTERVALS", "build_scheduler", "create_app"]

# The API hosts one job when INGEST_ENABLED is on: the same `ingest` job the
# worker runs, through the same builder and the same `run_job`.
INGEST_JOBS = ("ingest",)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI app. Called by the entrypoint and by the tests."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    quiet_health_probes(enabled=not settings.log_health_probes)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        scheduler: BaseScheduler | None = None
        if settings.ingest_enabled:
            service_logger.info(
                "Starting in-process ingest scheduler (every %ss, lookback %s days)",
                settings.data_poll_interval,
                settings.lookback_days,
            )
            scheduler = build_scheduler(settings, select(names=INGEST_JOBS))
            scheduler.start()
        else:
            service_logger.info("INGEST_ENABLED is off: serving the API only")
        try:
            yield
        finally:
            if scheduler is not None:
                # Not run_process's wait=True: uvicorn's shutdown must never be
                # held by an in-flight backfill. The worker waits; we do not.
                scheduler.shutdown(wait=False)
            get_db().close()
            set_db(None)

    app = FastAPI(title="candlegraph", version="0.5.0", lifespan=lifespan)

    @app.get("/status")
    def status() -> JSONResponse:
        """Liveness, a real database round-trip, and ingest freshness.

        The body is built by :func:`candlegraph.api.status.status_body`, which
        the ``status`` MCP tool also calls, so both surfaces report the same
        thing. Only the HTTP status code is decided here.
        """
        body, database_up = status_body(get_db(), settings)
        return JSONResponse(body, status_code=200 if database_up else 503)

    app.include_router(GraphQLRouter(chart_schema), prefix="/graphql")
    return app


app = create_app()
