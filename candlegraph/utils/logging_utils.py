"""Logging setup for the service.

Importing this module is side-effect free beyond creating the logger object;
handlers are attached by :func:`configure_logging`, which the entrypoint calls.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar

LOGGER_NAME = "candlegraph"
LOG_FORMAT = "%(levelname)s: %(asctime)s %(name)s %(message)s%(run)s"

service_logger = logging.getLogger(LOGGER_NAME)

# Set by harness.run_job for the length of a run, so every line logged inside
# the run can be matched to its ledger row.
current_run_id: ContextVar[str | None] = ContextVar("current_run_id", default=None)


class RunIdFilter(logging.Filter):
    """Adds ``run=<first 8 chars of run_id>`` to lines logged during a run."""

    def filter(self, record: logging.LogRecord) -> bool:
        run_id = current_run_id.get()
        record.run = f" run={run_id[:8]}" if run_id else ""
        return True


def configure_logging(level: str = "INFO") -> logging.Logger:
    """Attach a stream handler to the service logger, exactly once.

    Safe to call repeatedly: a second call updates the level instead of adding
    a duplicate handler (which would double every line).
    """
    service_logger.setLevel(level.upper())
    if not service_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        handler.addFilter(RunIdFilter())
        service_logger.addHandler(handler)
    service_logger.propagate = False
    return service_logger


HEALTH_PATH = "/status"
UVICORN_ACCESS_LOGGER = "uvicorn.access"


class HealthProbeFilter(logging.Filter):
    """Drop access-log lines for successful health probes.

    Uvicorn formats its access line from ``record.args`` as
    ``(client, method, path, http_version, status_code)``. A 2xx ``GET
    /status`` is the compose probe saying nothing happened; it is dropped so
    the log shows requests. Anything else about ``/status`` — a 503, a 500 —
    is kept, because that is the probe saying something *did* happen.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            return True
        method, path, status = args[1], args[2], args[4]
        is_ok_probe = (
            method == "GET" and path == HEALTH_PATH and isinstance(status, int) and 200 <= status < 300
        )
        return not is_ok_probe


def quiet_health_probes(enabled: bool = True) -> None:
    """Install (or remove) :class:`HealthProbeFilter` on uvicorn's access logger. Idempotent."""
    access = logging.getLogger(UVICORN_ACCESS_LOGGER)
    for existing in list(access.filters):
        if isinstance(existing, HealthProbeFilter):
            access.removeFilter(existing)
    if enabled:
        access.addFilter(HealthProbeFilter())
