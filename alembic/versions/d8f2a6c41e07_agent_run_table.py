"""The agent_run table: one row per agent graph run.

Phase 4. Agents observe the system and post reports; this is where the
reports land, so ``candlegraph agent-runs`` can answer "what did the observer
say last night?" without a chat channel, and so the next run can compare
itself with the last one.

Deliberately separate from ``ingest_run``: different grain, different
readers, and ``data_fresh`` must never depend on whether an agent ran.

Revision ID: d8f2a6c41e07
Revises: c7a3e5b81d94
Create Date: 2026-10-25 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from candlegraph.settings import get_settings

revision: str = "d8f2a6c41e07"
down_revision: str | None = "c7a3e5b81d94"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = get_settings().db_schema


def upgrade() -> None:
    op.create_table(
        "agent_run",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=False), nullable=False, unique=True),
        # observer | janitor | ...
        sa.Column("agent", sa.String, nullable=False),
        # ok | info | warning | critical | error
        sa.Column("status", sa.String, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_seconds", sa.Numeric, nullable=True),
        sa.Column("findings_total", sa.Integer, nullable=False, server_default="0"),
        sa.Column("summary", sa.Text, nullable=False, server_default=""),
        sa.Column("report", sa.Text, nullable=False, server_default=""),
        sa.Column("findings", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("detail", postgresql.JSONB, nullable=False, server_default="{}"),
        schema=SCHEMA,
    )
    # The read pattern is "this agent, newest first".
    op.create_index("ix_agent_run_agent_started", "agent_run", ["agent", "started_at"], schema=SCHEMA)


def downgrade() -> None:
    op.drop_index("ix_agent_run_agent_started", table_name="agent_run", schema=SCHEMA)
    op.drop_table("agent_run", schema=SCHEMA)
