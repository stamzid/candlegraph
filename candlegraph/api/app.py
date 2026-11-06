"""FastAPI application: GraphQL plus a health check.

Ingestion normally runs in its own process (``candlegraph worker``); in the
compose stack the API sets ``INGEST_ENABLED=false`` and the worker does the
work. The in-process scheduler is kept for single-process deployments and for
``make up`` on a laptop, and it uses the worker's own scheduler builder so
there is only one definition of the job.

Either way the API comes up and stays up when the subgraph is unreachable: an
upstream outage degrades the data, not the service.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from strawberry.fastapi import GraphQLRouter

from candlegraph.api.schema import chart_schema
from candlegraph.api.status import STALE_AFTER_INTERVALS, status_body
from candlegraph.ingestion.worker import INGEST_JOB_ID, build_scheduler, ingest_tick
from candlegraph.settings import Settings, get_settings
from candlegraph.storage import get_db, set_db
from candlegraph.utils.logging_utils import configure_logging, quiet_health_probes, service_logger

__all__ = ["INGEST_JOB_ID", "STALE_AFTER_INTERVALS", "build_scheduler", "create_app", "ingest_job"]


def ingest_job(settings: Settings) -> None:
    """One scheduled ingest pass. Never raises.

    Kept as a module-level name because the in-process scheduler and the tests
    both patch it; it delegates to the worker's implementation.
    """
    ingest_tick(settings)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI app. Called by the entrypoint and by the tests."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    quiet_health_probes(enabled=not settings.log_health_probes)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        scheduler: BackgroundScheduler | None = None
        if settings.ingest_enabled:
            service_logger.info(
                "Starting in-process ingest scheduler (every %ss, lookback %s days)",
                settings.data_poll_interval,
                settings.lookback_days,
            )
            scheduler = build_scheduler(settings, job=ingest_job)
            scheduler.start()
        else:
            service_logger.info("INGEST_ENABLED is off: serving the API only")
        try:
            yield
        finally:
            if scheduler is not None:
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
