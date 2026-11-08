"""The fill cache learns to say how unsure it is.

Stage 0, rollout step 4. The model-based estimators (``causal``, the Kalman
local-level filter, and ``bridge``, the Brownian bridge) produce a posterior
mean *and* a standard deviation that widens with the gap. Linear
interpolation cannot: ``docs/roadmap/DESIGN.md`` §0 lists "a point with no
error bar" as its main cost, and this column is what closes that gap for the
two estimators that can.

``sigma_log`` is one standard deviation in **log** space, because the
estimators model log prices — a random walk in logs cannot produce a
negative price, and a 1% move costs the same at 3 as at 30,000. A consumer
turns it into a price band with ``price * exp(±z * sigma_log)``. It is
nullable and stays ``NULL`` for ``linear`` rows, which is invariant 11's
rule applied here: "this estimator has no error bar" is not "the error bar
is zero".

No change to the natural key. ``method`` already distinguished the
estimators, which is what lets all three hold an estimate for the same
missing hour so they can be compared against each other.

Revision ID: f513c5d75c55
Revises: f116af87c3a2
Create Date: 2026-11-08 09:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from candlegraph.settings import get_settings

revision: str = "f513c5d75c55"
down_revision: str | None = "f116af87c3a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = get_settings().db_schema


def upgrade() -> None:
    # Nullable with no default: a `linear` row genuinely has no band, and a
    # server default of 0 would claim certainty it has not earned.
    op.add_column(
        "token_hours_fill",
        sa.Column("sigma_log", sa.Numeric(), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("token_hours_fill", "sigma_log", schema=SCHEMA)
