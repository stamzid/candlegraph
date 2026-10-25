"""Logging setup for the service.

Importing this module is side-effect free beyond creating the logger object;
handlers are attached by :func:`configure_logging`, which the entrypoint calls.
"""

from __future__ import annotations

import logging

LOGGER_NAME = "candlegraph"
LOG_FORMAT = "%(levelname)s: %(asctime)s %(name)s %(message)s"

service_logger = logging.getLogger(LOGGER_NAME)


def configure_logging(level: str = "INFO") -> logging.Logger:
    """Attach a stream handler to the service logger, exactly once.

    Safe to call repeatedly: a second call updates the level instead of adding
    a duplicate handler (which would double every line).
    """
    service_logger.setLevel(level.upper())
    if not service_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        service_logger.addHandler(handler)
    service_logger.propagate = False
    return service_logger
