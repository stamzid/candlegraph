"""PostgreSQL access: the connection pool and every statement we issue.

Two properties matter here and are enforced by the integration tests:

* Nothing connects at import time. The pool is created on first use so the
  module can be imported in a test run with no database anywhere.
* Write failures raise. The previous version logged and returned zero, which
  turned a rejected batch into a silent data-loss bug.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import psycopg2
from psycopg2 import pool, sql
from psycopg2.extras import DictCursor, execute_values

from candlegraph.settings import get_settings
from candlegraph.utils.logging_utils import service_logger

# A ready-to-execute statement. Aliased at module level because the
# `DatabaseManager.sql` property shadows the `sql` module inside the class body.
SqlQuery = sql.Composed | str

# Columns written per hourly row, in statement order.
HOUR_COLUMNS = (
    "token_id",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "price_usd",
    "period_start_unix",
    "timestamp",
)

_HOUR_TEMPLATE = (
    "(%(id)s,%(symbol)s,%(open)s,%(high)s,%(low)s,%(close)s,%(priceUSD)s,%(periodStartUnix)s,%(timestamp)s)"
)
_TOKEN_TEMPLATE = "(%(id)s,%(name)s,%(symbol)s,%(totalSupply)s,%(volumeUSD)s,%(decimals)s)"


@dataclass(frozen=True)
class Statements:
    """Every SQL statement, bound to a schema name.

    Built per schema rather than hardcoded so a test database can use its own.
    """

    insert_token_hours: sql.Composed
    insert_token_hours_template: str
    insert_token: sql.Composed
    insert_token_template: str
    get_token_metadata: sql.Composed
    get_latest_timestamps: sql.Composed
    chart_query: sql.Composed
    delete_older_data: sql.Composed


@lru_cache(maxsize=4)
def statements_for(schema: str) -> Statements:
    """SQL for ``schema``, built once per schema name."""
    ns = sql.Identifier(schema)

    # Natural key is (token_id, period_start_unix): one row per token per hour.
    # Re-ingesting the same hour refreshes it instead of failing the batch.
    insert_token_hours = sql.SQL(
        """
        INSERT INTO {ns}.token_hours_data
            (token_id, symbol, open, high, low, close, price_usd, period_start_unix, timestamp)
        VALUES %s
        ON CONFLICT (token_id, period_start_unix) DO UPDATE SET
            symbol = EXCLUDED.symbol,
            open = EXCLUDED.open,
            high = EXCLUDED.high,
            low = EXCLUDED.low,
            close = EXCLUDED.close,
            price_usd = EXCLUDED.price_usd,
            timestamp = EXCLUDED.timestamp
        RETURNING id
        """
    ).format(ns=ns)

    insert_token = sql.SQL(
        """
        INSERT INTO {ns}.token (id, name, symbol, total_supply, volume_usd, decimals)
        VALUES %s
        ON CONFLICT (id) DO UPDATE SET
            name = EXCLUDED.name,
            symbol = EXCLUDED.symbol,
            total_supply = EXCLUDED.total_supply,
            volume_usd = EXCLUDED.volume_usd,
            decimals = EXCLUDED.decimals
        RETURNING id
        """
    ).format(ns=ns)

    get_token_metadata = sql.SQL("SELECT * FROM {ns}.token WHERE id = %(token_id)s").format(ns=ns)

    get_latest_timestamps = sql.SQL(
        """
        SELECT token_id, MAX(period_start_unix) AS latest_unix
        FROM {ns}.token_hours_data
        GROUP BY token_id
        """
    ).format(ns=ns)

    # The bucket label is the bucket's own start, computed from the interval,
    # not MIN(timestamp) of the rows present. A missing first hour must not
    # shift the candle's reported time.
    chart_query = sql.SQL(
        """
        SELECT
            to_timestamp(FLOOR(period_start_unix::numeric / %(interval)s) * %(interval)s) AS interval_start,
            array_agg(open ORDER BY period_start_unix) AS opens,
            array_agg(close ORDER BY period_start_unix DESC) AS closes,
            MAX(high) AS max_high,
            MIN(low) AS min_low,
            AVG(price_usd) AS avg_price_usd,
            COUNT(*) AS hours
        FROM {ns}.token_hours_data
        WHERE token_id = %(token_id)s
          AND (%(start_unix)s::bigint IS NULL OR period_start_unix >= %(start_unix)s)
          AND (%(end_unix)s::bigint IS NULL OR period_start_unix < %(end_unix)s)
        GROUP BY FLOOR(period_start_unix::numeric / %(interval)s)
        ORDER BY interval_start
        """
    ).format(ns=ns)

    delete_older_data = sql.SQL(
        "DELETE FROM {ns}.token_hours_data WHERE period_start_unix < %(interval_start)s"
    ).format(ns=ns)

    return Statements(
        insert_token_hours=insert_token_hours,
        insert_token_hours_template=_HOUR_TEMPLATE,
        insert_token=insert_token,
        insert_token_template=_TOKEN_TEMPLATE,
        get_token_metadata=get_token_metadata,
        get_latest_timestamps=get_latest_timestamps,
        chart_query=chart_query,
        delete_older_data=delete_older_data,
    )


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
