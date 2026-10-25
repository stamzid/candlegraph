"""Fixtures shared by every test tier.

The one rule enforced here: tests never read the developer's real ``.env`` and
never touch the process-wide settings, database, or token caches. Everything is
constructed explicitly so a test's behaviour is visible in the test.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from candlegraph.dba import set_db, statements_for
from candlegraph.settings import Settings, reset_settings_cache
from candlegraph.tokens import TokenRegistry, load_tokens, reset_token_cache

TOKENS_YAML = """
tokens:
  - address: "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"
    symbol: WBTC
    name: Wrapped BTC
  - address: "0x95ad61b0a150d79219dcf64e1e6cc01f0b64c4ce"
    symbol: SHIB
    name: SHIBA INU
"""

WBTC = "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"
SHIB = "0x95ad61b0a150d79219dcf64e1e6cc01f0b64c4ce"


# Variables that would silently change what a test is configured with if they
# happened to be set in the shell (or left behind by an earlier fixture).
AMBIENT_ENV = (
    "DATABASE_URL",
    "DB_HOST",
    "DB_NAME",
    "DB_PASS",
    "DB_PORT",
    "DB_SCHEMA",
    "DB_USER",
    "GRAPH_API_KEY",
    "INGEST_ENABLED",
    "LOOKBACK_DAYS",
    "RETAIN_HISTORY",
    "TOKENS_FILE",
    "TRANSPORT_URL",
)


@pytest.fixture(autouse=True)
def _clean_global_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Reset every module-level cache and ambient variable around each test."""
    for name in AMBIENT_ENV:
        monkeypatch.delenv(name, raising=False)
    reset_settings_cache()
    reset_token_cache()
    set_db(None)
    yield
    reset_settings_cache()
    reset_token_cache()
    set_db(None)


@pytest.fixture
def tokens_file(tmp_path: Path) -> Path:
    """A two-token registry file, independent of the repo's own config."""
    path = tmp_path / "tokens.yaml"
    path.write_text(TOKENS_YAML, encoding="utf-8")
    return path


@pytest.fixture
def registry(tokens_file: Path) -> TokenRegistry:
    return load_tokens(tokens_file)


def make_settings(**overrides: Any) -> Settings:
    """Settings built only from the given values.

    ``_env_file=None`` is the important part: it stops pydantic-settings from
    reading the developer's ``.env``, which would make tests pass or fail
    depending on a file that is not in the repo.
    """
    defaults: dict[str, Any] = {
        "graph_api_key": "test-key",
        "tokens_file": "config/tokens.yaml",
        "lookback_days": 7,
        "retain_history": True,
        "ingest_enabled": False,
    }
    return Settings(_env_file=None, **{**defaults, **overrides})


@pytest.fixture
def settings(tokens_file: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Test settings pointed at the temporary tokens file."""
    monkeypatch.delenv("GRAPH_API_KEY", raising=False)
    return make_settings(tokens_file=str(tokens_file))


@pytest.fixture
def frozen_now() -> datetime:
    """A fixed clock so lookback arithmetic is deterministic."""
    return datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)


class FakeDatabase:
    """A stand-in for :class:`~candlegraph.dba.DatabaseManager`.

    Records every statement it is handed and replays canned read results, so
    the ingest and resolver logic can be tested without a database.
    """

    def __init__(self, schema: str = "candlegraph", **reads: list[dict[str, Any]]) -> None:
        self.schema = schema
        self.sql = statements_for(schema)
        self.reads = reads
        self.upserts: list[tuple[str, list[dict[str, Any]]]] = []
        self.executed: list[tuple[str, dict[str, Any] | None]] = []
        self.fetches: list[tuple[str, dict[str, Any] | None]] = []
        self.delete_rowcount = 0
        self.closed = False

    def _label(self, query: Any) -> str:
        for name in ("get_latest_timestamps", "get_token_metadata", "chart_query", "delete_older_data"):
            if query is getattr(self.sql, name):
                return name
        if query is self.sql.insert_token:
            return "insert_token"
        if query is self.sql.insert_token_hours:
            return "insert_token_hours"
        return "unknown"

    def fetch_all(self, query: Any, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        label = self._label(query)
        self.fetches.append((label, params))
        return list(self.reads.get(label, []))

    def execute(self, query: Any, params: dict[str, Any] | None = None) -> int:
        self.executed.append((self._label(query), params))
        return self.delete_rowcount

    def upsert_many(
        self, query: Any, template: str, records: list[dict[str, Any]], page_size: int = 500
    ) -> int:
        self.upserts.append((self._label(query), list(records)))
        return len(records)

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_db() -> FakeDatabase:
    return FakeDatabase()
