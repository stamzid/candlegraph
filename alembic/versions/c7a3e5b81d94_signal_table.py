"""The signal table: one row per strategy, symbol, and bar.

Phase 3. Storing signals rather than recomputing them on demand is what lets
a backtest be reproduced from the database alone, and gives the later agent
phases something to observe.

``score`` is nullable on purpose: a bar before a strategy's warm-up window
has no opinion, which is different from an opinion of zero.

Revision ID: c7a3e5b81d94
Revises: b4e7c91d02af
Create Date: 2026-10-25 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from candlegraph.settings import get_settings

revision: str = "c7a3e5b81d94"
down_revision: str | None = "b4e7c91d02af"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = get_settings().db_schema


def upgrade() -> None:
    op.create_table(
        "signal",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("strategy", sa.String, nullable=False),
        sa.Column("symbol", sa.String, nullable=False),
        sa.Column("period_start_unix", sa.BigInteger, nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        # -1 short, 0 flat, 1 long.
        sa.Column("signal", sa.SmallInteger, nullable=False),
        sa.Column("score", sa.Numeric, nullable=True),
        sa.Column("params", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("signal IN (-1, 0, 1)", name="ck_signal_position"),
        sa.UniqueConstraint(
            "strategy", "symbol", "period_start_unix", name="uix_signal_strategy_symbol_period"
        ),
        schema=SCHEMA,
    )
    # The read pattern is "this strategy, this symbol, newest first".
    op.create_index(
        "ix_signal_strategy_symbol_period",
        "signal",
        ["strategy", "symbol", "period_start_unix"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("ix_signal_strategy_symbol_period", table_name="signal", schema=SCHEMA)
    op.drop_table("signal", schema=SCHEMA)
