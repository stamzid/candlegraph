"""The generic `run` ledger, and run_log over all three ledgers.

Harness rollout step 3 (docs/harness/runner/DESIGN.md).

`ingest_run` and `agent_run` stay the detailed ledgers of the two jobs that
have one. Every other job — the scheduled news fetch today, stage 2's
pipeline runs next — records itself in `run`: the same two-phase row
(inserted as `running`, updated by `run_id` when it ends), the same
`trigger` / `outcome` / `parent_run_id` columns, with counts and errors as
JSONB because a generic ledger cannot have a column per job.

Plain, not a hypertable, for the same reason as the other two ledgers: the
closing UPDATE carries no time coordinate. Recorded in
docs/storage/table-placement/DESIGN.md before this migration was written.

`run_log` is dropped and recreated with its third branch. The view pins a
column list, so any later migration touching one of the three ledgers has
to do the same.

Revision ID: 43a899e29e3a
Revises: be8aa030e200
Create Date: 2026-11-14 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from candlegraph.settings import get_settings

revision: str = "43a899e29e3a"
down_revision: str | None = "be8aa030e200"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = get_settings().db_schema

RUN_BRANCH = """
UNION ALL
SELECT run_id, job, trigger, outcome, NULL::text AS verdict,
       started_at, finished_at, duration_seconds, parent_run_id, detail
  FROM "{s}".run
"""

# Kept in full rather than imported from be8aa030e200: a migration has to
# read the same years from now as it does today.
RUN_LOG = """
CREATE VIEW "{s}".run_log AS
SELECT run_id, 'ingest'::text AS job, trigger, status AS outcome, NULL::text AS verdict,
       started_at, finished_at, duration_seconds, parent_run_id, detail
  FROM "{s}".ingest_run
UNION ALL
SELECT run_id, agent AS job, trigger, outcome,
       CASE WHEN outcome = 'failed' THEN NULL ELSE status END AS verdict,
       started_at, finished_at, duration_seconds, parent_run_id, detail
  FROM "{s}".agent_run
"""


def upgrade() -> None:
    op.create_table(
        "run",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=False), nullable=False, unique=True),
        # The registered job name: fetch_news today, pipeline steps later.
        sa.Column("job", sa.String, nullable=False),
        # Why it ran: scheduled | manual | backfill | ui | mcp | event. NULL is
        # never written here; the column is nullable only so a future job that
        # genuinely does not know cannot be forced to invent one (invariant 11).
        sa.Column("trigger", sa.String, nullable=True),
        # running | succeeded | partial | failed.
        sa.Column("outcome", sa.String, nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Numeric, nullable=True),
        # A generic ledger cannot have a column per job, so the job's own
        # numbers are JSONB: {"searched": 3, "cached": 2, ...}.
        sa.Column("counts", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("errors", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("detail", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("parent_run_id", postgresql.UUID(as_uuid=False), nullable=True),
        schema=SCHEMA,
    )
    # The read pattern is "this job, newest first", as on agent_run.
    op.create_index("ix_run_job_started", "run", ["job", "started_at"], schema=SCHEMA)
    op.execute(f'DROP VIEW "{SCHEMA}".run_log')
    op.execute((RUN_LOG + RUN_BRANCH).format(s=SCHEMA))


def downgrade() -> None:
    op.execute(f'DROP VIEW "{SCHEMA}".run_log')
    op.execute(RUN_LOG.format(s=SCHEMA))
    op.drop_index("ix_run_job_started", table_name="run", schema=SCHEMA)
    op.drop_table("run", schema=SCHEMA)
