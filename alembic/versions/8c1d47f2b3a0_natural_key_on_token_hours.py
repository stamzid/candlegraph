"""Make (token_id, period_start_unix) the natural key of token_hours_data.

The original unique constraint included ``price_usd``, so re-ingesting an hour
whose price had been revised inserted a second row for that hour instead of
conflicting, and a batch containing one true duplicate failed in full. This
revision de-duplicates existing rows, keeping the most recently inserted row
per (token, hour), and replaces the constraint so writes can upsert.

Revision ID: 8c1d47f2b3a0
Revises: 4a4f4da52183
Create Date: 2026-10-25 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from candlegraph.settings import get_settings

revision: str = "8c1d47f2b3a0"
down_revision: str | None = "4a4f4da52183"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = get_settings().db_schema
OLD_CONSTRAINT = "uix_token_id_timestamp"
NEW_CONSTRAINT = "uix_token_id_period_start_unix"


def upgrade() -> None:
    op.execute(
        sa.text(
            f"""
            DELETE FROM "{SCHEMA}".token_hours_data a
            USING "{SCHEMA}".token_hours_data b
            WHERE a.token_id = b.token_id
              AND a.period_start_unix = b.period_start_unix
              AND a.id < b.id
            """
        )
    )
    op.execute(sa.text(f'DELETE FROM "{SCHEMA}".token_hours_data WHERE period_start_unix IS NULL'))

    op.drop_constraint(OLD_CONSTRAINT, "token_hours_data", schema=SCHEMA, type_="unique")
    op.alter_column("token_hours_data", "period_start_unix", nullable=False, schema=SCHEMA)
    op.create_unique_constraint(
        NEW_CONSTRAINT, "token_hours_data", ["token_id", "period_start_unix"], schema=SCHEMA
    )


def downgrade() -> None:
    op.drop_constraint(NEW_CONSTRAINT, "token_hours_data", schema=SCHEMA, type_="unique")
    op.alter_column("token_hours_data", "period_start_unix", nullable=True, schema=SCHEMA)
    op.create_unique_constraint(
        OLD_CONSTRAINT, "token_hours_data", ["token_id", "price_usd", "timestamp"], schema=SCHEMA
    )
