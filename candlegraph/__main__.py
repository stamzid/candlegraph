"""Console entrypoint: ``python -m candlegraph`` / ``candlegraph``."""

from __future__ import annotations

import uvicorn

from candlegraph.settings import get_settings
from candlegraph.utils.logging_utils import configure_logging, service_logger


def main() -> None:
    """Serve the API on the configured host and port."""
    settings = get_settings()
    configure_logging(settings.log_level)
    service_logger.info("Starting candlegraph on %s:%s", settings.host, settings.port)
    try:
        uvicorn.run(
            "candlegraph.app:app",
            host=settings.host,
            port=settings.port,
            log_level=settings.log_level.lower(),
        )
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        service_logger.warning("Shutting down on KeyboardInterrupt")


if __name__ == "__main__":
    main()
