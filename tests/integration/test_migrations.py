"""The migration chain as a whole: one head, every revision reachable, and a
full round trip to the empty database and back on the pinned image.

`test_timescale.py` round-trips one revision. This walks the entire chain,
which is what step 1 of `docs/rollout/ROLLOUT.md` asked for: the database
image changed, so "there is no dangling revision that can kill the
database" has to be proven on that image, not assumed from the previous one.
"""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from candlegraph.storage import DatabaseManager
from tests.integration.conftest import _env
from tests.integration.test_timescale import (
    BASE,
    HOUR,
    HYPERTABLES,
    KNOWLEDGE_TABLES,
    PLAIN_TABLES,
    hour_record,
    hypertable_names,
    write_hours,
)

pytestmark = pytest.mark.integration

ALL_TABLES = HYPERTABLES | PLAIN_TABLES | KNOWLEDGE_TABLES


def tables_in(db: DatabaseManager) -> set[str]:
    rows = db.fetch_all(
        "SELECT tablename FROM pg_tables WHERE schemaname = %(schema)s AND tablename <> 'alembic_version'",
        {"schema": db.schema},
    )
    return {row["tablename"] for row in rows}


def stamped_version(db: DatabaseManager) -> str | None:
    rows = db.fetch_all(f'SELECT version_num FROM "{db.schema}".alembic_version')
    return str(rows[0]["version_num"]) if rows else None


def test_the_chain_has_exactly_one_head_and_the_database_is_on_it(db: DatabaseManager) -> None:
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))

    assert len(scripts.get_heads()) == 1
    assert stamped_version(db) == scripts.get_current_head()


def test_the_whole_chain_round_trips_to_the_empty_database_and_back(
    db: DatabaseManager, migrated_dsn: str
) -> None:
    """`downgrade base` leaves nothing but the schema; `upgrade head` rebuilds
    every table, the hypertables as hypertables, on the image we actually run.

    Rows are written first so the downgrades run against populated tables —
    an empty table hides a lost sequence or a mis-named constraint.
    """
    write_hours(db, [hour_record(BASE + i * 40 * 24 * HOUR) for i in range(3)])
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))
    config = Config("alembic.ini")

    with _env(DATABASE_URL=migrated_dsn, DB_SCHEMA=db.schema):
        command.downgrade(config, "base")
        assert tables_in(db) == set(), "downgrade to base must remove every table the chain created"
        assert stamped_version(db) is None

        command.upgrade(config, "head")

    assert tables_in(db) >= ALL_TABLES
    assert hypertable_names(db) >= HYPERTABLES
    assert PLAIN_TABLES.isdisjoint(hypertable_names(db))
    assert stamped_version(db) == scripts.get_current_head()
    # The rebuilt schema accepts writes again (sequences and constraints came back).
    assert write_hours(db, [hour_record(BASE)]) == 1


def test_the_run_shape_migration_round_trips_on_populated_ledgers(
    db: DatabaseManager, migrated_dsn: str
) -> None:
    """be8aa030e200 back-fills `outcome` from `status = 'error'`, leaves `trigger`
    NULL on old rows, and its downgrade closes an unfinished agent row rather
    than failing on NOT NULL or deleting it."""
    from datetime import UTC, datetime

    from candlegraph.agents.base import AgentReport
    from candlegraph.agents.ledger import open_agent_run, record_agent_run
    from candlegraph.ingestion import ledger

    now = datetime(2026, 11, 13, 12, tzinfo=UTC)
    config = Config("alembic.ini")
    db.execute(f'TRUNCATE "{db.schema}".agent_run, "{db.schema}".ingest_run')
    with _env(DATABASE_URL=migrated_dsn, DB_SCHEMA=db.schema):
        command.downgrade(config, "f513c5d75c55")
        # Old-schema rows: one crashed agent run, one ingest run still `running`.
        record_agent_run(
            db, AgentReport("observer", "55555555-5555-5555-5555-555555555555", now, now, error="x")
        )
        db.execute(
            f'INSERT INTO "{db.schema}".ingest_run (run_id, trigger, status, started_at) '
            "VALUES ('66666666-6666-6666-6666-666666666666', 'scheduled', 'running', %(t)s)",
            {"t": now},
        )

        command.upgrade(config, "be8aa030e200")
        (agent,) = db.fetch_all(f'SELECT * FROM "{db.schema}".agent_run')
        assert agent["outcome"] == "failed" and agent["status"] == "error"
        assert agent["trigger"] is None  # not guessed
        logged = {r["job"]: r for r in db.fetch_all(f'SELECT * FROM "{db.schema}".run_log')}
        assert logged["ingest"]["outcome"] == "running"
        assert logged["observer"]["verdict"] is None

        open_agent_run(db, "77777777-7777-7777-7777-777777777777", "observer", "ui", now)
        command.downgrade(config, "f513c5d75c55")
        unfinished = db.fetch_all(
            f"SELECT * FROM \"{db.schema}\".agent_run WHERE run_id = '77777777-7777-7777-7777-777777777777'"
        )
        assert unfinished[0]["status"] == "error" and unfinished[0]["finished_at"] == now
        assert ledger.recent_runs(db, 5)[0]["status"] == "running"  # ingest rows untouched

        command.upgrade(config, "head")
    assert stamped_version(db) == ScriptDirectory.from_config(config).get_current_head()
