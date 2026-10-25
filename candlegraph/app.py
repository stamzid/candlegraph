"""FastAPI application: GraphQL plus a health check, with ingestion alongside.

Ingestion runs on a background scheduler, including the very first backfill.
That is deliberate: the API must come up and stay up even when the subgraph is
unreachable, so an outage upstream degrades the data rather than the service.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from strawberry.fastapi import GraphQLRouter

from candlegraph.dba import get_db, set_db
from candlegraph.ingest import run_ingest
from candlegraph.schema import chart_schema
from candlegraph.settings import Settings, get_settings
from candlegraph.tokens import get_token_registry
from candlegraph.utils.logging_utils import configure_logging, service_logger

INGEST_JOB_ID = "ingest"


def ingest_job(settings: Settings) -> None:
    """One scheduled ingest pass. Never raises: a failed run must not kill the job."""
    try:
        registry = get_token_registry()
        run_ingest(get_db(), registry, settings)
    except Exception:
        service_logger.exception("Ingest run failed; will retry at the next interval")


def build_scheduler(settings: Settings) -> BackgroundScheduler:
    """A scheduler whose ingest job fires immediately, then every poll interval."""
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        ingest_job,
        trigger="interval",
        seconds=settings.data_poll_interval,
        args=[settings],
        id=INGEST_JOB_ID,
        # Run the initial backfill as the job's first firing rather than
        # blocking startup on it.
        next_run_time=datetime.now(tz=UTC),
        # A slow backfill must not stack up behind the next tick.
        max_instances=1,
        coalesce=True,
    )
    return scheduler


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI app. Called by the entrypoint and by the tests."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        scheduler: BackgroundScheduler | None = None
        if settings.ingest_enabled:
            service_logger.info(
                "Starting ingest scheduler (every %ss, lookback %s days)",
                settings.data_poll_interval,
                settings.lookback_days,
            )
            scheduler = build_scheduler(settings)
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

    app = FastAPI(title="candlegraph", version="0.2.0", lifespan=lifespan)

    @app.get("/status")
    def status() -> JSONResponse:
        """Liveness plus a real database round-trip."""
        database_up = get_db().ping()
        body = {
            "status": "ok" if database_up else "degraded",
            "database": "up" if database_up else "down",
            "ingest": "enabled" if settings.ingest_enabled else "disabled",
            "tokens": list(get_token_registry().symbols),
        }
        return JSONResponse(body, status_code=200 if database_up else 503)

    app.include_router(GraphQLRouter(chart_schema), prefix="/graphql")
    return app


app = create_app()
