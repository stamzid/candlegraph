"""One shape of a run: trigger, outcome and parent on agent_run; the run_log view.

Harness rollout step 2 (docs/harness/runner/DESIGN.md).

- ``agent_run`` is now written in two phases, like ``ingest_run``: a row is
  inserted when the run starts and updated when it ends. So ``finished_at``
  and ``status`` become nullable; they are NULL while the run is in progress.
- ``agent_run.outcome`` says whether the run completed. ``status`` keeps its
  old meaning (what the agent found). Existing rows with ``status = 'error'``
  get ``outcome = 'failed'``; all others ``succeeded``.
- ``agent_run.trigger`` says why it ran. Existing rows get NULL: we do not
  know, and a guessed value could not be told apart from a real one later.
- ``parent_run_id`` on both tables, for stage 2 pipelines. NULL today.
- ``run_log`` reads both ledgers with the same column names.

Revision ID: be8aa030e200
Revises: f513c5d75c55
Create Date: 2026-11-13 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from candlegraph.settings import get_settings

revision: str = "be8aa030e200"
down_revision: str | None = "f513c5d75c55"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = get_settings().db_schema

# The view lists its columns explicitly. A later migration that adds a third
# ledger (rollout step 3: the generic `run` table) must DROP and recreate it.
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
    op.add_column("agent_run", sa.Column("trigger", sa.String, nullable=True), schema=SCHEMA)
    op.add_column(
        "agent_run",
        sa.Column("outcome", sa.String, nullable=False, server_default="succeeded"),
        schema=SCHEMA,
    )
    op.add_column("agent_run", sa.Column("parent_run_id", postgresql.UUID(as_uuid=False)), schema=SCHEMA)
    op.add_column("ingest_run", sa.Column("parent_run_id", postgresql.UUID(as_uuid=False)), schema=SCHEMA)
    op.execute(f"UPDATE \"{SCHEMA}\".agent_run SET outcome = 'failed' WHERE status = 'error'")
    op.alter_column("agent_run", "finished_at", nullable=True, schema=SCHEMA)
    op.alter_column("agent_run", "status", nullable=True, schema=SCHEMA)
    op.execute(RUN_LOG.format(s=SCHEMA))


def downgrade() -> None:
    op.execute(f'DROP VIEW "{SCHEMA}".run_log')
    # Rows still open when we go back cannot satisfy NOT NULL. Keep them and
    # close them as errors rather than delete them.
    op.execute(
        f'UPDATE "{SCHEMA}".agent_run SET finished_at = COALESCE(finished_at, started_at), '
        "status = COALESCE(status, 'error') WHERE finished_at IS NULL OR status IS NULL"
    )
    op.alter_column("agent_run", "status", nullable=False, schema=SCHEMA)
    op.alter_column("agent_run", "finished_at", nullable=False, schema=SCHEMA)
    op.drop_column("ingest_run", "parent_run_id", schema=SCHEMA)
    op.drop_column("agent_run", "parent_run_id", schema=SCHEMA)
    op.drop_column("agent_run", "outcome", schema=SCHEMA)
    op.drop_column("agent_run", "trigger", schema=SCHEMA)
