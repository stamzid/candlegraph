"""The console entrypoint hands the configured host and port to uvicorn."""

from __future__ import annotations

from typing import Any

import pytest

from candlegraph.cli import main
from tests.conftest import make_settings


def test_serve_binds_the_configured_address(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        "candlegraph.cli.get_settings",
        lambda: make_settings(host="127.0.0.1", port=9999, log_level="WARNING"),
    )
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: captured.update(app=app, **kwargs))

    assert main(["serve"]) == 0
    assert captured == {
        "app": "candlegraph.api.app:app",
        "host": "127.0.0.1",
        "port": 9999,
        "log_level": "warning",
    }
