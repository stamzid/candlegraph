"""Market depth on token hours, pool-level tables, and the ingest run ledger.

Phase 2 of the roadmap. Three changes:

* ``token_hours_data`` gains volume, TVL, and fee columns. All are nullable
  because rows written before this revision genuinely do not have the data —
  a NULL here means "not collected", which is distinct from zero.
* ``pool`` and ``pool_hours_data`` arrive. Transaction counts live only on the
  pool entity in the subgraph, so pair-level data is the only source for them.
* ``ingest_run`` records every run, which is what makes staleness and failure
  observable without reading logs.

Revision ID: b4e7c91d02af
Revises: 8c1d47f2b3a0
Create Date: 2026-10-25 08:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from candlegraph.settings import get_settings

revision: str = "b4e7c91d02af"
down_revision: str | None = "8c1d47f2b3a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = get_settings().db_schema

# (column, type) pairs added to token_hours_data.
TOKEN_HOUR_COLUMNS = [
    ("volume", sa.Numeric),
    ("volume_usd", sa.Numeric),
    ("untracked_volume_usd", sa.Numeric),
    ("total_value_locked", sa.Numeric),
    ("total_value_locked_usd", sa.Numeric),
    ("fees_usd", sa.Numeric),
]

# Aggregate columns added to the token entity.
TOKEN_COLUMNS = [
    ("volume", sa.Numeric),
    ("fees_usd", sa.Numeric),
    ("tx_count", sa.BigInteger),
    ("pool_count", sa.BigInteger),
    ("total_value_locked", sa.Numeric),
    ("total_value_locked_usd", sa.Numeric),
    ("derived_eth", sa.Numeric),
]


def upgrade() -> None:
    for name, column_type in TOKEN_HOUR_COLUMNS:
        op.add_column("token_hours_data", sa.Column(name, column_type(), nullable=True), schema=SCHEMA)
    for name, column_type in TOKEN_COLUMNS:
        op.add_column("token", sa.Column(name, column_type(), nullable=True), schema=SCHEMA)

    op.add_column(
        "token",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )

    op.create_table(
        "pool",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("token0_id", sa.String, nullable=False),
        sa.Column("token1_id", sa.String, nullable=False),
        sa.Column("token0_symbol", sa.String, nullable=True),
        sa.Column("token1_symbol", sa.String, nullable=True),
        sa.Column("fee_tier", sa.BigInteger, nullable=True),
        sa.Column("created_at_timestamp", sa.BigInteger, nullable=True),
        sa.Column("liquidity", sa.Numeric, nullable=True),
        sa.Column("volume_usd", sa.Numeric, nullable=True),
        sa.Column("fees_usd", sa.Numeric, nullable=True),
        sa.Column("tx_count", sa.BigInteger, nullable=True),
        sa.Column("total_value_locked_usd", sa.Numeric, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )
    # Pools are discovered per tracked token, so both sides are looked up.
    op.create_index("ix_pool_token0", "pool", ["token0_id"], schema=SCHEMA)
    op.create_index("ix_pool_token1", "pool", ["token1_id"], schema=SCHEMA)

    op.create_table(
        "pool_hours_data",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("pool_id", sa.String, nullable=False),
        sa.Column("period_start_unix", sa.BigInteger, nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric, nullable=True),
        sa.Column("high", sa.Numeric, nullable=True),
        sa.Column("low", sa.Numeric, nullable=True),
        sa.Column("close", sa.Numeric, nullable=True),
        sa.Column("liquidity", sa.Numeric, nullable=True),
        sa.Column("sqrt_price", sa.Numeric, nullable=True),
        sa.Column("token0_price", sa.Numeric, nullable=True),
        sa.Column("token1_price", sa.Numeric, nullable=True),
        sa.Column("tvl_usd", sa.Numeric, nullable=True),
        sa.Column("volume_token0", sa.Numeric, nullable=True),
        sa.Column("volume_token1", sa.Numeric, nullable=True),
        sa.Column("volume_usd", sa.Numeric, nullable=True),
        sa.Column("fees_usd", sa.Numeric, nullable=True),
        sa.Column("tx_count", sa.BigInteger, nullable=True),
        # Same natural key rule as token_hours_data: one row per pool per hour.
        sa.UniqueConstraint("pool_id", "period_start_unix", name="uix_pool_id_period_start_unix"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_pool_hours_pool_period",
        "pool_hours_data",
        ["pool_id", "period_start_unix"],
        schema=SCHEMA,
    )

    op.create_table(
        "ingest_run",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=False), nullable=False, unique=True),
        # scheduled | manual | backfill
        sa.Column("trigger", sa.String, nullable=False),
        # running | succeeded | partial | failed
        sa.Column("status", sa.String, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Numeric, nullable=True),
        sa.Column("lookback_days", sa.Integer, nullable=True),
        sa.Column("tokens_written", sa.Integer, nullable=False, server_default="0"),
        sa.Column("token_hours_written", sa.Integer, nullable=False, server_default="0"),
        sa.Column("pools_written", sa.Integer, nullable=False, server_default="0"),
        sa.Column("pool_hours_written", sa.Integer, nullable=False, server_default="0"),
        sa.Column("errors", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("detail", postgresql.JSONB, nullable=False, server_default="{}"),
        schema=SCHEMA,
    )
    op.create_index("ix_ingest_run_started_at", "ingest_run", ["started_at"], schema=SCHEMA)


def downgrade() -> None:
    op.drop_index("ix_ingest_run_started_at", table_name="ingest_run", schema=SCHEMA)
    op.drop_table("ingest_run", schema=SCHEMA)

    op.drop_index("ix_pool_hours_pool_period", table_name="pool_hours_data", schema=SCHEMA)
    op.drop_table("pool_hours_data", schema=SCHEMA)

    op.drop_index("ix_pool_token1", table_name="pool", schema=SCHEMA)
    op.drop_index("ix_pool_token0", table_name="pool", schema=SCHEMA)
    op.drop_table("pool", schema=SCHEMA)

    op.drop_column("token", "updated_at", schema=SCHEMA)
    for name, _ in reversed(TOKEN_COLUMNS):
        op.drop_column("token", name, schema=SCHEMA)
    for name, _ in reversed(TOKEN_HOUR_COLUMNS):
        op.drop_column("token_hours_data", name, schema=SCHEMA)
