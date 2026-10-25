"""Guards that keep the unit tier genuinely offline.

Any real outbound HTTP request from a unit test is a bug in the test: it makes
the suite slow, flaky, and dependent on an API key. Fail loudly instead.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(self: Any, request: httpx.Request) -> httpx.Response:
        raise AssertionError(
            f"unit test attempted a real HTTP request to {request.url}; inject an httpx.MockTransport instead"
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
