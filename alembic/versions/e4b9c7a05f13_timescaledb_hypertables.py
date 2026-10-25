"""Convert the three series tables to TimescaleDB hypertables.

``token_hours_data``, ``pool_hours_data`` and ``signal`` are measurements at
an instant, read by time window, and never deleted — so they are chunked on
``period_start_unix``. ``token`` and ``pool`` (current state) and
``ingest_run`` and ``agent_run`` (operational ledgers) stay plain tables;
``docs/storage/table-placement/DESIGN.md`` records why each, and
``test_the_ledger_and_state_tables_are_not_hypertables`` enforces it.

A hypertable requires every unique index to contain the partitioning column,
so each surrogate primary key ``(id)`` becomes ``(id, period_start_unix)``.
The natural-key unique constraints already contain it and are left alone,
which is what keeps the upserts in invariant 4 working unchanged.

Partitioning on ``period_start_unix`` rather than ``timestamp`` is deliberate:
every statement filters on the integer column, and chunk exclusion only
happens on the column the planner sees. An integer time dimension needs an
explicit "now" before any policy can run, hence ``set_integer_now_func``; no
compression or retention policy is installed (see the design document).

Revision ID: e4b9c7a05f13
Revises: d8f2a6c41e07
Create Date: 2026-10-25 20:00:00.000000

"""

from collections.abc import Sequence
from dataclasses import dataclass, field

import sqlalchemy as sa
from alembic import op

from candlegraph.settings import get_settings

revision: str = "e4b9c7a05f13"
down_revision: str | None = "d8f2a6c41e07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = get_settings().db_schema

# One chunk per 30 days. At one row per entity-hour this keeps the chunk
# count in the low hundreds for years, and a typical analytics window inside
# one or two chunks.
CHUNK_INTERVAL_SECONDS = 30 * 24 * 3600

# Integer time dimensions have no implicit "now"; policies and continuous
# aggregates need one before they can be created.
NOW_FUNCTION = "unix_now"

TIME_COLUMN = "period_start_unix"


@dataclass(frozen=True)
class Series:
    """A table being chunked, and everything needed to rebuild it plain again.

    The downgrade recreates constraints and indexes *by name*, so the names
    are listed here rather than left to PostgreSQL to generate.
    """

    table: str
    unique: tuple[str, list[str]]
    indexes: list[tuple[str, list[str]]] = field(default_factory=list)
    checks: list[tuple[str, str]] = field(default_factory=list)
    server_defaults: dict[str, str] = field(default_factory=dict)


SERIES = (
    Series(
        table="token_hours_data",
        unique=("uix_token_id_period_start_unix", ["token_id", "period_start_unix"]),
        indexes=[("ix_token_id_timestamp", ["token_id", "timestamp"])],
    ),
    Series(
        table="pool_hours_data",
        unique=("uix_pool_id_period_start_unix", ["pool_id", "period_start_unix"]),
        indexes=[("ix_pool_hours_pool_period", ["pool_id", "period_start_unix"])],
    ),
    Series(
        table="signal",
        unique=("uix_signal_strategy_symbol_period", ["strategy", "symbol", "period_start_unix"]),
        indexes=[("ix_signal_strategy_symbol_period", ["strategy", "symbol", "period_start_unix"])],
        checks=[("ck_signal_position", "signal IN (-1, 0, 1)")],
        server_defaults={"params": "'{}'::jsonb"},
    ),
)


def _primary_key_name(table: str) -> str:
    """The table's current primary key constraint name, read from the catalogue.

    Looked up rather than assumed: these keys were created without an explicit
    name, so the value is PostgreSQL's choice, not ours.
    """
    name = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT conname FROM pg_constraint "
                "WHERE contype = 'p' AND conrelid = format('%I.%I', :schema, :table)::regclass"
            ),
            {"schema": SCHEMA, "table": table},
        )
        .scalar()
    )
    if name is None:
        raise RuntimeError(f"{SCHEMA}.{table} has no primary key to convert")
    return str(name)


def _require_extension() -> None:
    """Fail with something actionable when the image is plain PostgreSQL.

    Silently leaving the tables unchunked would give a developer a different
    schema from CI's, which is worse than not starting.
    """
    available = (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb'"))
        .scalar()
    )
    if not available:
        raise RuntimeError(
            "the timescaledb extension is not available on this server. "
            "candlegraph runs on the timescale/timescaledb image; see "
            "docs/storage/timescaledb/DESIGN.md. Point DB_HOST at the compose "
            "database, or `make down && make up` to rebuild it."
        )


def upgrade() -> None:
    _require_extension()
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    op.execute(
        sa.text(
            f'CREATE OR REPLACE FUNCTION "{SCHEMA}".{NOW_FUNCTION}() RETURNS BIGINT '
            "LANGUAGE SQL STABLE AS $$ SELECT EXTRACT(EPOCH FROM now())::BIGINT $$"
        )
    )

    for series in SERIES:
        # The partitioning column has to be in every unique index, and the
        # surrogate key is the only one missing it.
        op.drop_constraint(_primary_key_name(series.table), series.table, schema=SCHEMA, type_="primary")
        op.create_primary_key(f"{series.table}_pkey", series.table, ["id", TIME_COLUMN], schema=SCHEMA)
        # migrate_data moves whatever is already stored into chunks, so this
        # revision works on a populated database and not only a fresh one.
        op.execute(
            sa.text(
                f"SELECT create_hypertable('\"{SCHEMA}\".{series.table}', '{TIME_COLUMN}', "
                f"chunk_time_interval => {CHUNK_INTERVAL_SECONDS}, migrate_data => true)"
            )
        )
        op.execute(
            sa.text(
                f"SELECT set_integer_now_func('\"{SCHEMA}\".{series.table}', '\"{SCHEMA}\".{NOW_FUNCTION}')"
            )
        )


def _rebuild_as_plain_table(series: Series) -> None:
    """Copy a hypertable into an ordinary table of the same name.

    TimescaleDB cannot un-chunk a table in place, so the data is copied into
    a plain table and swapped. ``LIKE ... EXCLUDING ALL`` carries the columns,
    their types and their NOT NULLs but nothing else, which is what lets the
    keys and indexes come back under their original names instead of
    PostgreSQL's generated ones.
    """
    table, staging = series.table, f"{series.table}__plain"

    op.execute(sa.text(f'CREATE TABLE "{SCHEMA}".{staging} (LIKE "{SCHEMA}".{table} EXCLUDING ALL)'))
    op.execute(sa.text(f'INSERT INTO "{SCHEMA}".{staging} SELECT * FROM "{SCHEMA}".{table}'))
    # Drops the hypertable, its chunks, and the sequence it owned.
    op.execute(sa.text(f'DROP TABLE "{SCHEMA}".{table}'))
    op.execute(sa.text(f'ALTER TABLE "{SCHEMA}".{staging} RENAME TO {table}'))

    # Recreate the sequence that went with the dropped table, positioned past
    # the highest id so the next insert does not collide.
    sequence = f"{table}_id_seq"
    op.execute(sa.text(f'CREATE SEQUENCE "{SCHEMA}".{sequence} OWNED BY "{SCHEMA}".{table}.id'))
    op.execute(
        sa.text(
            f"SELECT setval('\"{SCHEMA}\".{sequence}', "
            f'GREATEST((SELECT COALESCE(MAX(id), 0) FROM "{SCHEMA}".{table}), 1))'
        )
    )
    op.execute(
        sa.text(
            f'ALTER TABLE "{SCHEMA}".{table} ALTER COLUMN id SET DEFAULT nextval(\'"{SCHEMA}".{sequence}\')'
        )
    )

    op.create_primary_key(f"{table}_pkey", table, ["id"], schema=SCHEMA)
    unique_name, unique_columns = series.unique
    op.create_unique_constraint(unique_name, table, unique_columns, schema=SCHEMA)
    for name, condition in series.checks:
        op.create_check_constraint(name, table, sa.text(condition), schema=SCHEMA)
    for name, columns in series.indexes:
        op.create_index(name, table, columns, schema=SCHEMA)
    for column, default in series.server_defaults.items():
        op.execute(sa.text(f'ALTER TABLE "{SCHEMA}".{table} ALTER COLUMN {column} SET DEFAULT {default}'))


def downgrade() -> None:
    for series in SERIES:
        _rebuild_as_plain_table(series)
    op.execute(sa.text(f'DROP FUNCTION IF EXISTS "{SCHEMA}".{NOW_FUNCTION}()'))
    # The extension itself is left installed. Dropping it would be a
    # destructive act well beyond undoing this revision.
