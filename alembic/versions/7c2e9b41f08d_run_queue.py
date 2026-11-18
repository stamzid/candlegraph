"""The pipeline work queue: one row per run a trigger asked for.

Stage 2, rollout step 5 (docs/rollout/ROLLOUT.md; pipelines DESIGN §7.2).

A trigger — an event, a schedule, or a person — does not run a pipeline. It
puts a row here, and the dispatcher claims it. That indirection buys the two
properties the step exists for:

* `UNIQUE (pipeline, dedup_key)` makes "exactly one hourly run per bar hour"
  a property of the schema rather than of whoever is awake. Twelve ingest
  runs in an hour insert one row.
* `claimed_by` / `lease_expires_at` make a crash recoverable: a row whose
  lease has expired goes back to `queued` and is claimed again. Re-running a
  pipeline from the start is safe because every step is declared idempotent.

Plain, not a hypertable, and deleted from on purpose once done — recorded in
docs/storage/table-placement/DESIGN.md before this migration was written.
There is deliberately no `run_event` table; the reason is recorded there too.

Revision ID: 7c2e9b41f08d
Revises: 43a899e29e3a
Create Date: 2026-11-18 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from candlegraph.settings import get_settings

revision: str = "7c2e9b41f08d"
down_revision: str | None = "43a899e29e3a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = get_settings().db_schema


def upgrade() -> None:
    op.create_table(
        "run_queue",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        # What to run, and the spec version it was asked for. A run is always
        # executed with the version that enqueued it.
        sa.Column("pipeline", sa.String, nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        # The idempotency key. For an hourly event trigger this is the bar
        # hour, so a second ingest in the same hour is a no-op insert.
        sa.Column("dedup_key", sa.String, nullable=False),
        # Why it was asked for: event | scheduled | manual | ui | mcp.
        sa.Column("trigger", sa.String, nullable=False),
        # The run that caused it, for an event trigger. NULL for a schedule.
        sa.Column("cause_run_id", postgresql.UUID(as_uuid=False), nullable=True),
        # queued -> claimed -> done | dead.
        sa.Column("state", sa.String, nullable=False, server_default="queued"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("claimed_by", sa.String, nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        # The pipeline run this row produced, once it has produced one. The
        # join from "what was asked for" to "what happened".
        sa.Column("run_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("pipeline", "dedup_key", name="uq_run_queue_dedup"),
        schema=SCHEMA,
    )
    # The claim query's WHERE: the oldest queued row that is due.
    op.create_index(
        "ix_run_queue_claimable",
        "run_queue",
        ["state", "available_at"],
        schema=SCHEMA,
    )
    # The reaper's WHERE: claimed rows whose lease has run out.
    op.create_index(
        "ix_run_queue_lease",
        "run_queue",
        ["lease_expires_at"],
        schema=SCHEMA,
        postgresql_where=sa.text("state = 'claimed'"),
    )


def downgrade() -> None:
    op.drop_index("ix_run_queue_lease", table_name="run_queue", schema=SCHEMA)
    op.drop_index("ix_run_queue_claimable", table_name="run_queue", schema=SCHEMA)
    op.drop_table("run_queue", schema=SCHEMA)
