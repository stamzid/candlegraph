"""The PostgreSQL connection pool and thin query helpers.

Two properties matter here and are enforced by the integration tests:

* Nothing connects at import time. The pool is created on first use so the
  module can be imported in a test run with no database anywhere.
* Write failures raise. Logging and returning zero turns a rejected batch into
  a silent data-loss bug.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import psycopg2
from psycopg2 import pool
from psycopg2.extras import DictCursor, execute_values

from candlegraph.settings import get_settings
from candlegraph.storage.statements import SqlQuery, Statements, statements_for
from candlegraph.utils.logging_utils import service_logger


class DatabaseManager:
    """A lazily-connected pool plus thin query helpers."""

    def __init__(
        self,
        dsn: str,
        schema: str = "candlegraph",
        min_connections: int = 2,
        max_connections: int = 10,
    ) -> None:
        self.dsn = dsn
        self.schema = schema
        self.min_connections = min_connections
        self.max_connections = max_connections
        self._pool: pool.ThreadedConnectionPool | None = None

    @property
    def sql(self) -> Statements:
        """SQL statements bound to this manager's schema."""
        return statements_for(self.schema)

    # --- connection handling ----------------------------------------------

    @property
    def pool(self) -> pool.ThreadedConnectionPool:
        """The connection pool, created on first access."""
        if self._pool is None:
            service_logger.info(
                "Opening connection pool (min=%s max=%s)", self.min_connections, self.max_connections
            )
            self._pool = pool.ThreadedConnectionPool(
                minconn=self.min_connections,
                maxconn=self.max_connections,
                dsn=self.dsn,
                # Everything downstream assumes UTC; do not inherit the server's.
                options="-c timezone=UTC",
            )
        return self._pool

    def close(self) -> None:
        """Close every pooled connection. Idempotent."""
        if self._pool is not None:
            self._pool.closeall()
            self._pool = None

    def ping(self) -> bool:
        """True when a trivial query round-trips. Used by the health check."""
        try:
            with self.cursor() as cursor:
                cursor.execute("SELECT 1")
            return True
        except psycopg2.Error as exc:
            service_logger.warning("Database ping failed: %s", exc)
            return False

    @contextmanager
    def connection(self) -> Iterator[Any]:
        """Borrow a connection and always return it to the pool."""
        conn = self.pool.getconn()
        try:
            yield conn
        finally:
            self.pool.putconn(conn)

    @contextmanager
    def cursor(self, commit: bool = False) -> Iterator[Any]:
        """A dict cursor. Commits on clean exit when ``commit`` is set, else rolls back."""
        with self.connection() as conn:
            cursor = conn.cursor(cursor_factory=DictCursor)
            try:
                yield cursor
            except Exception:
                conn.rollback()
                raise
            else:
                if commit:
                    conn.commit()
                else:
                    conn.rollback()
            finally:
                cursor.close()

    # --- queries -----------------------------------------------------------

    def fetch_all(self, query: SqlQuery, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Run a read query and return rows as plain dicts."""
        with self.cursor() as cursor:
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    def execute(self, query: SqlQuery, params: dict[str, Any] | None = None) -> int:
        """Run a write statement and return the affected row count."""
        with self.cursor(commit=True) as cursor:
            cursor.execute(query, params)
            return int(cursor.rowcount)

    def upsert_many(
        self,
        query: SqlQuery,
        template: str,
        records: Sequence[dict[str, Any]],
        page_size: int = 500,
    ) -> int:
        """Batch-upsert ``records``, returning the number of rows written.

        The statement must end in ``RETURNING``; the returned rows are counted
        so the caller gets a true total across pages rather than the last
        page's ``rowcount``.
        """
        if not records:
            return 0
        with self.cursor(commit=True) as cursor:
            returned = execute_values(
                cursor, query, records, template=template, page_size=page_size, fetch=True
            )
            return len(returned)


_db: DatabaseManager | None = None


def get_db() -> DatabaseManager:
    """The process-wide manager, built from settings on first use."""
    global _db
    if _db is None:
        settings = get_settings()
        _db = DatabaseManager(
            dsn=settings.dsn,
            schema=settings.db_schema,
            min_connections=settings.min_db_connections,
            max_connections=settings.max_db_connections,
        )
    return _db


def set_db(manager: DatabaseManager | None) -> None:
    """Replace (or clear) the process-wide manager. For tests and shutdown."""
    global _db
    _db = manager
