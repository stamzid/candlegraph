"""initial_schema

Revision ID: 4a4f4da52183
Revises: 
Create Date: 2024-05-06 19:02:26.409670

"""
from typing import Sequence, Union

from alembic import op
from datetime import datetime
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4a4f4da52183'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'token_hours_data',
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("token_id", sa.String, nullable=False),
        sa.Column("symbol", sa.String, nullable=False),
        sa.Column("open", sa.Numeric, default=0),
        sa.Column("high", sa.Numeric, default=0),
        sa.Column("low", sa.Numeric, default=0),
        sa.Column("close", sa.Numeric, default=0),
        sa.Column("price_usd", sa.Numeric, default=0),
        sa.Column("period_start_unix", sa.BigInteger, default=0),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("token_id", "price_usd", "timestamp", name="uix_token_id_timestamp"),
        schema="candlegraph"
    )
    op.create_index('ix_token_id_timestamp', 'token_hours_data', ['token_id', 'timestamp'], schema="candlegraph")

    op.create_table(
        'token',
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("name", sa.String, primary_key=False),
        sa.Column("symbol", sa.String, nullable=False),
        sa.Column("total_supply", sa.String, nullable=False),
        sa.Column("volume_usd", sa.String, nullable=False),
        sa.Column("decimals", sa.String, nullable=False),
        schema="candlegraph"
    )


def downgrade() -> None:
    op.drop_index("ix_token_id_timestamp", table_name="token_hours_data", schema="candlegraph")
    op.drop_table("token_hours_data", schema="candlegraph")
    op.drop_table("token")
