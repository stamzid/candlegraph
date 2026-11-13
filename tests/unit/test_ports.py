"""``serve --port``: a busy port is detected by binding, as uvicorn would."""

from __future__ import annotations

import socket

from candlegraph.utils.ports import first_free, port_is_free


def test_a_listening_port_is_busy_and_a_released_one_is_free() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        assert port_is_free("127.0.0.1", port) is False
        assert first_free("127.0.0.1", [port]) is None
    assert port_is_free("127.0.0.1", port) is True
