"""The console entrypoint hands the configured host and port to uvicorn."""

from __future__ import annotations

from typing import Any

import pytest

from candlegraph.__main__ import main
from tests.conftest import make_settings


def test_main_serves_on_the_configured_address(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        "candlegraph.__main__.get_settings",
        lambda: make_settings(host="127.0.0.1", port=9999, log_level="WARNING"),
    )
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: captured.update(app=app, **kwargs))

    main()

    assert captured == {
        "app": "candlegraph.app:app",
        "host": "127.0.0.1",
        "port": 9999,
        "log_level": "warning",
    }
