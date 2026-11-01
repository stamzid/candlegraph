"""Allow ``rate_limited`` as a ``source_call`` status.

The Tavily design (``docs/knowledge/tavily/DESIGN.md``) distinguishes a rate
limit (HTTP 429/432) from an ordinary error on purpose: retrying one spends
the budget on failures, and the observer counts the two differently. The
step-2 migration created the check constraint with only ``ok``, ``error``
and ``budget_refused``; this widens it.

Revision ID: 35826afa331a
Revises: f399ca0770a0
Create Date: 2026-11-01 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from candlegraph.settings import get_settings

revision: str = "35826afa331a"
down_revision: str | None = "f399ca0770a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = get_settings().db_schema

CONSTRAINT = "ck_source_call_status"
OLD = "status IN ('ok', 'error', 'budget_refused')"
NEW = "status IN ('ok', 'error', 'budget_refused', 'rate_limited')"


def upgrade() -> None:
    op.drop_constraint(CONSTRAINT, "source_call", schema=SCHEMA, type_="check")
    op.create_check_constraint(CONSTRAINT, "source_call", sa.text(NEW), schema=SCHEMA)


def downgrade() -> None:
    # The old constraint cannot hold rows the new one allowed: fold them into
    # `error` (the message column still says it was a rate limit) first, so
    # the downgrade works on a populated table.
    op.execute(
        sa.text(
            f"UPDATE \"{SCHEMA}\".source_call SET status = 'error', "
            f"error = COALESCE(error, 'rate limited') WHERE status = 'rate_limited'"
        )
    )
    op.drop_constraint(CONSTRAINT, "source_call", schema=SCHEMA, type_="check")
    op.create_check_constraint(CONSTRAINT, "source_call", sa.text(OLD), schema=SCHEMA)
