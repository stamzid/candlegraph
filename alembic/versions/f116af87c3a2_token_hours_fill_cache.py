"""The fill cache: derived prices for hours the subgraph never emitted.

Stage 0, rollout step 3. Interpolating a thin token's gaps on every widget
change is wasted work, so the result is stored — but **never in
``token_hours_data``**. ``docs/roadmap/DESIGN.md`` §0 rejects writing filled
rows into the market table because coverage would then read 100% by
construction and ``NULL`` would stop meaning "not collected" (invariant 11).
A separate table keeps both properties: the quality report still measures
what was actually ingested, and a consumer has to ask for the estimates.

The table holds **only missing hours** — one row per hour that has no
``token_hours_data`` row — so it is a fraction of one token's history rather
than a second copy of it.

Plain, not a hypertable, and ``docs/storage/table-placement/DESIGN.md``
records why against the four questions. The decisive one is that this table
is *deleted from*: a cached fill is invalidated the moment a real row lands
for its hour, and a method is dropped wholesale when the cap or the
estimator changes. Invariant 4's "nothing deletes it" is a property of
market data, not of a derived cache.

``method`` is part of the natural key so stage 0 step 4's causal estimator
can sit beside the linear one without either overwriting the other.

Revision ID: f116af87c3a2
Revises: 35826afa331a
Create Date: 2026-11-07 02:05:27.883087

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from candlegraph.settings import get_settings

revision: str = "f116af87c3a2"
down_revision: str | None = "35826afa331a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = get_settings().db_schema


def upgrade() -> None:
    op.create_table(
        "token_hours_fill",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("token_id", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("period_start_unix", sa.BigInteger(), nullable=False),
        # Which estimator produced the value. 'linear' is display-only;
        # a causal method may be added later and must not collide with it.
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("price_usd", sa.Numeric(), nullable=True),
        sa.Column("close", sa.Numeric(), nullable=True),
        # How long the run of missing hours was. Interpolation gives a point
        # with no error bar; this is the next best thing, and it lets a
        # reader tell a one-hour fill from a twenty-two-hour guess.
        sa.Column("gap_hours", sa.Integer(), nullable=False),
        sa.Column(
            "computed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("token_id", "period_start_unix", "method", name="uq_token_hours_fill_natural"),
        schema=SCHEMA,
    )
    # The read path is "this symbol, this window, this method", which is also
    # how the cache is invalidated and how the UI loads a chart's fills.
    op.create_index(
        "ix_token_hours_fill_symbol_time",
        "token_hours_fill",
        ["symbol", "method", "period_start_unix"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("ix_token_hours_fill_symbol_time", table_name="token_hours_fill", schema=SCHEMA)
    op.drop_table("token_hours_fill", schema=SCHEMA)
