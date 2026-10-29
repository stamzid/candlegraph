"""TimescaleDB: the placement decision, and that chunking changed nothing else.

The point of these is narrow. `test_database.py` already proves candles,
bucket boundaries, gaps and idempotency, and it passes unmodified against
hypertables — that is the real evidence the migration is correct. What is
left to pin here is the part a behavioural test cannot see: *which* tables
got chunked, that the chunking is on the column the queries filter, and that
the downgrade genuinely puts the rows back.

See docs/storage/table-placement/DESIGN.md for why each table is where it is.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from candlegraph.storage import DatabaseManager
from tests.conftest import WBTC

pytestmark = pytest.mark.integration

HOUR = 3600
# A long way apart, so the rows cannot land in the same 30-day chunk.
BASE = 1_700_000_000 // HOUR * HOUR

# docs/storage/table-placement/DESIGN.md is the rationale; this is the rule.
HYPERTABLES = {"token_hours_data", "pool_hours_data", "signal"}
PLAIN_TABLES = {"token", "pool", "ingest_run", "agent_run"}


def hypertable_names(db: DatabaseManager) -> set[str]:
    rows = db.fetch_all(
        "SELECT hypertable_name FROM timescaledb_information.hypertables "
        "WHERE hypertable_schema = %(schema)s",
        {"schema": db.schema},
    )
    return {row["hypertable_name"] for row in rows}


def hour_record(unix: int, token_id: str = WBTC, price: str = "100") -> dict[str, Any]:
    return {
        "id": token_id,
        "symbol": "WBTC",
        "open": price,
        "high": price,
        "low": price,
        "close": price,
        "priceUSD": price,
        "volume": "1",
        "volumeUSD": "1",
        "untrackedVolumeUSD": None,
        "totalValueLocked": None,
        "totalValueLockedUSD": "1",
        "feesUSD": "1",
        "periodStartUnix": unix,
        "timestamp": datetime.fromtimestamp(unix, tz=UTC),
    }


def write_hours(db: DatabaseManager, records: list[dict[str, Any]]) -> int:
    return db.upsert_many(db.sql.insert_token_hours, db.sql.insert_token_hours_template, records)


# --- the placement decision ------------------------------------------------


def test_the_extension_is_installed(db: DatabaseManager) -> None:
    rows = db.fetch_all("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")

    assert rows, "the migration requires TimescaleDB; the image must provide it"


def test_the_series_tables_are_hypertables(db: DatabaseManager) -> None:
    """Measurements at an instant, read by window, never deleted."""
    assert hypertable_names(db) >= HYPERTABLES


def test_the_ledger_and_state_tables_are_not_hypertables(db: DatabaseManager) -> None:
    """The decision not to chunk is as deliberate as the decision to chunk.

    `token` and `pool` are current state with no time coordinate. `ingest_run`
    is updated by `run_id` after the run finishes, which on a time-partitioned
    table would have to search every chunk.
    """
    assert PLAIN_TABLES.isdisjoint(hypertable_names(db))


def test_chunking_is_on_the_column_the_queries_filter(db: DatabaseManager) -> None:
    """Partitioning on `timestamp` would exclude no chunks: nothing filters on it."""
    rows = db.fetch_all(
        "SELECT hypertable_name, column_name FROM timescaledb_information.dimensions "
        "WHERE hypertable_schema = %(schema)s",
        {"schema": db.schema},
    )
    partitioned_on = {row["hypertable_name"]: row["column_name"] for row in rows}

    assert {partitioned_on[name] for name in HYPERTABLES} == {"period_start_unix"}


def test_the_primary_key_carries_the_partitioning_column(db: DatabaseManager) -> None:
    """A hypertable rejects any unique index that does not. The natural keys
    already contained it; the surrogate key had to grow."""
    rows = db.fetch_all(
        """
        SELECT a.attname
        FROM pg_constraint c
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
        WHERE c.contype = 'p'
          AND c.conrelid = format('%%I.%%I', %(schema)s, 'token_hours_data')::regclass
        """,
        {"schema": db.schema},
    )

    assert {row["attname"] for row in rows} == {"id", "period_start_unix"}


def test_rows_spread_across_chunks_by_time(db: DatabaseManager) -> None:
    write_hours(db, [hour_record(BASE), hour_record(BASE + 200 * 24 * HOUR)])

    rows = db.fetch_all(
        "SELECT COUNT(*) AS chunks FROM timescaledb_information.chunks "
        "WHERE hypertable_schema = %(schema)s AND hypertable_name = 'token_hours_data'",
        {"schema": db.schema},
    )

    assert int(rows[0]["chunks"]) == 2, "200 days apart cannot share a 30-day chunk"


# --- nothing else changed --------------------------------------------------


def test_a_hypertable_still_upserts_on_the_natural_key(db: DatabaseManager) -> None:
    """Invariant 4 survives chunking: re-ingesting an hour refreshes it."""
    write_hours(db, [hour_record(BASE, price="100")])
    write_hours(db, [hour_record(BASE, price="250")])

    rows = db.fetch_all(
        f'SELECT close FROM "{db.schema}".token_hours_data WHERE period_start_unix = %(unix)s',
        {"unix": BASE},
    )

    assert len(rows) == 1
    assert float(rows[0]["close"]) == 250.0


def test_an_upsert_spanning_chunks_writes_every_row(db: DatabaseManager) -> None:
    """One batch landing in several chunks still returns one row per record."""
    records = [hour_record(BASE + i * 40 * 24 * HOUR) for i in range(5)]

    assert write_hours(db, records) == 5


def test_a_window_read_excludes_the_chunks_it_does_not_need(db: DatabaseManager) -> None:
    """The reason for the migration, asserted rather than assumed."""
    write_hours(db, [hour_record(BASE + i * 40 * 24 * HOUR) for i in range(5)])

    plan = db.fetch_all(
        f"""
        EXPLAIN SELECT * FROM "{db.schema}".token_hours_data
        WHERE period_start_unix >= {BASE} AND period_start_unix < {BASE + HOUR}
        """
    )
    text = "\n".join(str(row["QUERY PLAN"]) for row in plan)

    assert text.count("_hyper_") <= 2, f"expected one chunk, got:\n{text}"


def test_candles_still_bucket_on_boundaries_through_time_bucket(db: DatabaseManager) -> None:
    """`chart_query` moved from FLOOR arithmetic to time_bucket; invariant 5 holds.

    The bucket label is the boundary computed from the interval, so a missing
    first hour must not shift it. Here the first hour of the day is absent.
    """
    day = BASE // (24 * HOUR) * (24 * HOUR)
    write_hours(db, [hour_record(day + 5 * HOUR), hour_record(day + 9 * HOUR)])

    rows = db.fetch_all(
        db.sql.chart_query,
        {"interval": 24 * HOUR, "token_id": WBTC, "start_unix": None, "end_unix": None},
    )

    assert len(rows) == 1
    assert int(rows[0]["interval_start"].timestamp()) == day
    assert int(rows[0]["hours"]) == 2


# --- the downgrade ---------------------------------------------------------


def test_the_timescale_migration_round_trips(db: DatabaseManager, migrated_dsn: str) -> None:
    """`downgrade` then `upgrade` on a populated database, rows and sequence intact.

    Converting a hypertable back is not something the extension offers, so the
    downgrade rebuilds the table. The risk it carries is losing a constraint
    name or the id sequence, which is exactly what this asserts.
    """
    from alembic import command
    from alembic.config import Config

    from tests.integration.conftest import _env

    write_hours(db, [hour_record(BASE + i * 40 * 24 * HOUR) for i in range(4)])

    def snapshot() -> tuple[set[str], set[str]]:
        constraints = db.fetch_all(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = format('%%I.%%I', %(schema)s, 'token_hours_data')::regclass",
            {"schema": db.schema},
        )
        indexes = db.fetch_all(
            "SELECT indexname FROM pg_indexes WHERE schemaname = %(schema)s "
            "AND tablename = 'token_hours_data'",
            {"schema": db.schema},
        )
        return ({r["conname"] for r in constraints}, {r["indexname"] for r in indexes})

    before = snapshot()
    config = Config("alembic.ini")

    with _env(DATABASE_URL=migrated_dsn, DB_SCHEMA=db.schema):
        command.downgrade(config, "-1")

        assert "token_hours_data" not in hypertable_names(db)
        rows = db.fetch_all(f'SELECT COUNT(*) AS n FROM "{db.schema}".token_hours_data')
        assert int(rows[0]["n"]) == 4, "the rebuild must not lose rows"

        # The recreated sequence has to be past the highest id, or the next
        # insert collides with a row that is already there.
        assert write_hours(db, [hour_record(BASE + HOUR)]) == 1

        command.upgrade(config, "head")

    assert "token_hours_data" in hypertable_names(db)
    assert snapshot() == before, "constraints and indexes must come back under their own names"
    rows = db.fetch_all(f'SELECT COUNT(*) AS n FROM "{db.schema}".token_hours_data')
    assert int(rows[0]["n"]) == 5


# --- the preflight ---------------------------------------------------------
#
# Both of these reproduce the 2026-10-28 outage, where the stack would not
# start and neither error named its cause. See
# docs/incidents/2026-10-28-stack-would-not-start/POSTMORTEM.md.


def test_a_version_table_stamped_with_an_unknown_revision_is_refused_with_a_cause(
    db: DatabaseManager, migrated_dsn: str
) -> None:
    """A draft revision applied locally and renamed before commit leaves the
    database at an id no checkout has. Alembic says only "can't locate"."""
    from alembic import command
    from alembic.config import Config

    from tests.integration.conftest import _env

    version_table = f'"{db.schema}".alembic_version'
    (current,) = db.fetch_all(f"SELECT version_num FROM {version_table}")
    db.execute(f"UPDATE {version_table} SET version_num = 'd2f8b6a41c35'")
    try:
        with (
            _env(DATABASE_URL=migrated_dsn, DB_SCHEMA=db.schema),
            pytest.raises(RuntimeError, match="stamped at revision d2f8b6a41c35") as raised,
        ):
            command.upgrade(Config("alembic.ini"), "head")
        message = str(raised.value)
        assert "COMPOSE_PROJECT_NAME" in message
        assert "renamed or rewritten before it was committed" in message
    finally:
        db.execute(
            f"UPDATE {version_table} SET version_num = %(version)s",
            {"version": current["version_num"]},
        )


def test_a_server_that_does_not_preload_timescaledb_is_refused_before_any_ddl() -> None:
    """The image with the library on disk but `shared_preload_libraries` empty:
    exactly what a volume from plain postgres:17 looks like under the
    TimescaleDB image. `pg_available_extensions` still says available, so the
    old check passed and `CREATE EXTENSION` killed the connection."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy.exc import OperationalError

    from tests.integration.conftest import POSTGRES_IMAGE, _env, _libpq_url

    try:
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:  # pragma: no cover
        pytest.skip("testcontainers is not installed")

    # The image's own init scripts would CREATE EXTENSION during initdb and
    # die on exactly the condition under test, so they are hidden behind an
    # empty tmpfs. What remains is a data directory initialised with no
    # TimescaleDB involvement, served by the TimescaleDB binary: the shape of
    # every volume that predates the move to this image.
    container = (
        PostgresContainer(POSTGRES_IMAGE, driver=None)
        .with_command(["postgres", "-c", "shared_preload_libraries="])
        .with_tmpfs_mount("/docker-entrypoint-initdb.d")
    )
    try:
        container.start()
    except Exception as exc:  # pragma: no cover - depends on the host
        pytest.skip(f"no Docker for a second throwaway Postgres: {exc}")

    try:
        dsn = _libpq_url(container.get_connection_url())
        with (
            _env(DATABASE_URL=dsn, DB_SCHEMA="candlegraph"),
            pytest.raises(RuntimeError, match="installed on this server but not preloaded") as raised,
        ):
            command.upgrade(Config("alembic.ini"), "head")
        assert "shared_preload_libraries=timescaledb" in str(raised.value)

        manager = DatabaseManager(dsn=dsn, schema="candlegraph", min_connections=1, max_connections=2)
        try:
            tables = manager.fetch_all(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'candlegraph' AND tablename <> 'alembic_version'"
            )
        finally:
            manager.close()
        assert tables == [], "the preflight must run before the first revision, not after it"
    except OperationalError as exc:  # pragma: no cover - the image refused the command
        pytest.fail(f"the throwaway server did not come up: {exc}")
    finally:
        container.stop()
