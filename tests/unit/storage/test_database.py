"""Statement construction and pool lifecycle, with no database involved.

These are the offline half of the data layer's coverage; the statements are
executed for real in ``tests/integration/test_database.py``.
"""

from __future__ import annotations

from typing import Any

import psycopg2
import pytest
from psycopg2 import sql

from candlegraph.settings import reset_settings_cache
from candlegraph.storage import DatabaseManager, get_db, set_db


def render(composed: sql.Composed) -> str:
    """Flatten a Composed into readable SQL without needing a connection."""
    parts: list[str] = []
    for part in composed.seq:
        if isinstance(part, sql.Identifier):
            parts.append('"' + '"."'.join(part.strings) + '"')
        else:
            parts.append(part.string)
    return "".join(parts)


class FakeCursor:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows = rows or []
        self.executed: list[tuple[Any, Any]] = []
        self.rowcount = len(self.rows)
        self.closed = False
        self.description = True

    def execute(self, query: Any, params: Any = None) -> None:
        self.executed.append((query, params))

    def fetchall(self) -> list[Any]:
        return self.rows

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self, cursor: FakeCursor | None = None) -> None:
        self.cursor_obj = cursor or FakeCursor()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, **_kwargs: Any) -> FakeCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.borrowed = 0
        self.returned = 0
        self.closed = False

    def getconn(self) -> FakeConnection:
        self.borrowed += 1
        return self.connection

    def putconn(self, _conn: FakeConnection) -> None:
        self.returned += 1

    def closeall(self) -> None:
        self.closed = True


@pytest.fixture
def manager_with_pool() -> tuple[DatabaseManager, FakePool, FakeConnection]:
    connection = FakeConnection()
    fake_pool = FakePool(connection)
    manager = DatabaseManager(dsn="postgresql://ignored/db")
    manager._pool = fake_pool  # type: ignore[assignment]
    return manager, fake_pool, connection


# --- pool lifecycle --------------------------------------------------------


def test_no_pool_is_created_until_it_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing the module used to open 16 connections, making tests impossible."""

    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the pool must not be created eagerly")

    monkeypatch.setattr("candlegraph.storage.database.pool.ThreadedConnectionPool", explode)

    manager = DatabaseManager(dsn="postgresql://nowhere/db")

    assert manager._pool is None


def test_the_pool_is_built_once_with_the_configured_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    sentinel = FakePool(FakeConnection())

    def record(**kwargs: Any) -> FakePool:
        calls.append(kwargs)
        return sentinel

    monkeypatch.setattr("candlegraph.storage.database.pool.ThreadedConnectionPool", record)
    manager = DatabaseManager(dsn="postgresql://host/db", min_connections=3, max_connections=9)

    assert manager.pool is sentinel
    assert manager.pool is sentinel
    assert len(calls) == 1
    assert calls[0]["minconn"] == 3
    assert calls[0]["maxconn"] == 9
    assert calls[0]["dsn"] == "postgresql://host/db"
    assert "timezone=UTC" in calls[0]["options"]


def test_close_is_idempotent(manager_with_pool: tuple[DatabaseManager, FakePool, FakeConnection]) -> None:
    manager, fake_pool, _ = manager_with_pool

    manager.close()
    manager.close()

    assert fake_pool.closed is True
    assert manager._pool is None


def test_connections_are_returned_even_when_the_query_raises(
    manager_with_pool: tuple[DatabaseManager, FakePool, FakeConnection],
) -> None:
    manager, fake_pool, _ = manager_with_pool

    with pytest.raises(RuntimeError), manager.connection():
        raise RuntimeError("boom")

    assert (fake_pool.borrowed, fake_pool.returned) == (1, 1)


def test_a_write_commits_on_success(
    manager_with_pool: tuple[DatabaseManager, FakePool, FakeConnection],
) -> None:
    manager, _, connection = manager_with_pool

    manager.execute("UPDATE x SET y = 1")

    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.cursor_obj.closed is True


def test_a_write_rolls_back_and_re_raises(
    manager_with_pool: tuple[DatabaseManager, FakePool, FakeConnection],
) -> None:
    """Swallowing write errors is how the old code lost data silently."""
    manager, _, connection = manager_with_pool

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise psycopg2.IntegrityError("duplicate key")

    connection.cursor_obj.execute = boom  # type: ignore[method-assign]

    with pytest.raises(psycopg2.IntegrityError):
        manager.execute("INSERT INTO x VALUES (1)")

    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_a_read_does_not_leave_a_transaction_open(
    manager_with_pool: tuple[DatabaseManager, FakePool, FakeConnection],
) -> None:
    manager, _, connection = manager_with_pool

    manager.fetch_all("SELECT 1")

    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_fetch_all_returns_plain_dicts(
    manager_with_pool: tuple[DatabaseManager, FakePool, FakeConnection],
) -> None:
    manager, _, connection = manager_with_pool
    connection.cursor_obj.rows = [{"a": 1}]

    assert manager.fetch_all("SELECT 1") == [{"a": 1}]
    assert type(manager.fetch_all("SELECT 1")[0]) is dict


def test_upsert_many_short_circuits_on_no_records(
    manager_with_pool: tuple[DatabaseManager, FakePool, FakeConnection],
) -> None:
    manager, fake_pool, _ = manager_with_pool

    assert manager.upsert_many(manager.sql.insert_token, manager.sql.insert_token_template, []) == 0
    assert fake_pool.borrowed == 0


def test_upsert_many_counts_the_returned_rows(
    manager_with_pool: tuple[DatabaseManager, FakePool, FakeConnection], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counting RETURNING rows is exact; cursor.rowcount only sees the last page."""
    manager, _, _ = manager_with_pool
    captured: dict[str, Any] = {}

    def fake_execute_values(cursor: Any, query: Any, records: Any, **kwargs: Any) -> list[tuple[int]]:
        captured.update(kwargs, records=records)
        return [(i,) for i in range(len(records))]

    monkeypatch.setattr("candlegraph.storage.database.execute_values", fake_execute_values)

    written = manager.upsert_many(
        manager.sql.insert_token, manager.sql.insert_token_template, [{"id": "a"}, {"id": "b"}]
    )

    assert written == 2
    assert captured["fetch"] is True
    assert captured["template"] == manager.sql.insert_token_template


def test_ping_reports_failure_instead_of_raising(
    manager_with_pool: tuple[DatabaseManager, FakePool, FakeConnection],
) -> None:
    manager, _, connection = manager_with_pool

    assert manager.ping() is True

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise psycopg2.OperationalError("server closed the connection")

    connection.cursor_obj.execute = boom  # type: ignore[method-assign]

    assert manager.ping() is False


# --- module-level accessor -------------------------------------------------


def test_get_db_is_built_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/d")
    monkeypatch.setenv("DB_SCHEMA", "custom")
    monkeypatch.setenv("MAX_DB_CONNECTIONS", "4")
    reset_settings_cache()

    manager = get_db()

    assert manager.dsn == "postgresql://u:p@h:5432/d"
    assert manager.schema == "custom"
    assert manager.max_connections == 4
    assert get_db() is manager


def test_set_db_overrides_and_clears() -> None:
    replacement = DatabaseManager(dsn="postgresql://replacement/db")

    set_db(replacement)
    assert get_db() is replacement

    set_db(None)
    assert get_db() is not replacement
