"""Is a TCP port free to bind? For ``candlegraph serve --port``.

Checked by binding, not by connecting: a connect probe misses a listener on
another interface, and on macOS Docker Desktop publishes ``APP_HOST_PORT``
on ``0.0.0.0`` so a local ``serve`` on the same port fails at bind time.
There is a race between this check and uvicorn's own bind; the check exists
to turn the common collision into a sentence with free alternatives, not to
reserve the port.
"""

from __future__ import annotations

import socket
from collections.abc import Iterable


def port_is_free(host: str, port: int) -> bool:
    """True if ``host:port`` can be bound right now."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        # As uvicorn does, so a port in TIME_WAIT is "free" here exactly when
        # it is free for uvicorn.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def first_free(host: str, ports: Iterable[int]) -> int | None:
    """The first of ``ports`` that is free on ``host``, or ``None``."""
    return next((port for port in ports if port_is_free(host, port)), None)
