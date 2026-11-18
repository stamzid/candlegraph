"""`token_daily`: daily candles maintained by the database.

Stage 2, rollout step 6 (docs/storage/timescaledb/DESIGN.md). The payoff the
TimescaleDB migration deferred until something had a consumer for it: the
candle query, when it is asked for a whole number of days.

Two things about the SELECT are load-bearing and easy to get wrong.

**It materialises sums and counts, never an average.** `AVG(price_usd)` per
day, averaged again per week, weights each day equally — and days here do not
have equal weight, because Uniswap emits no row for an hour with no swaps
(invariant 19). A thin day of four hours would count as much as a full one.
Storing `price_sum` and `hours` and dividing at read time gives the same
number as aggregating the hourly rows directly, which is what the equality
test checks.

**`first()`/`last()` rather than `array_agg(... ORDER BY ...)`.** They are
TimescaleDB's aggregates for exactly this, they parallelise, and they do not
store the whole day's values to then use two of them.

Real-time aggregation is left **on** (`materialized_only = false`), so the
current, incomplete bucket is computed from the hourly rows and a candle for
today exists between refreshes. With it off, today's candle would be absent
until the next refresh, which would be a visible change in what the API
returns.

A continuous aggregate is a view, not a table, so it needs no placement
decision — but the placement document records it anyway, because what it
holds is a copy of data that lives somewhere else.

Revision ID: 9d4b7a2c6e15
Revises: 7c2e9b41f08d
Create Date: 2026-11-18 14:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

from candlegraph.settings import get_settings

revision: str = "9d4b7a2c6e15"
down_revision: str | None = "7c2e9b41f08d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = get_settings().db_schema

DAY = 86400

CREATE = """
CREATE MATERIALIZED VIEW "{s}".token_daily
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT
    token_id,
    time_bucket({day}::bigint, period_start_unix) AS day,
    first(open, period_start_unix)  AS day_open,
    last(close, period_start_unix)  AS day_close,
    MAX(high)                        AS day_high,
    MIN(low)                         AS day_low,
    SUM(price_usd)                   AS price_sum,
    SUM(volume)                      AS volume,
    SUM(volume_usd)                  AS volume_usd,
    SUM(fees_usd)                    AS fees_usd,
    SUM(total_value_locked_usd)      AS tvl_sum,
    COUNT(*)                         AS hours,
    COUNT(price_usd)                 AS price_hours,
    COUNT(total_value_locked_usd)    AS tvl_hours
FROM "{s}".token_hours_data
GROUP BY token_id, day
WITH NO DATA
"""

# No refresh policy. `refresh_continuous_aggregate` is invalidation-driven, so
# refreshing the whole range costs 0.8 ms when nothing changed and 12 ms after
# a 90-day backfill (measured). A policy would add a second schedule with its
# own window to keep in step with LOOKBACK_DAYS, for no gain; the harness runs
# the refresh as a step of the hourly pipeline, where it is a recorded run.


def upgrade() -> None:
    # `WITH NO DATA` matters here: creating it with data would refresh, and
    # `refresh_continuous_aggregate()` cannot run inside a transaction block,
    # which is what Alembic gives us. The first refresh is the job's.
    op.execute(CREATE.format(s=SCHEMA, day=DAY))
    # `COUNT(price_usd)` and `COUNT(*)` differ when a column is NULL, which is
    # what "not collected" means here (invariant 11). The read path divides by
    # the matching count, so a token with no TVL reported gets NULL rather
    # than a number diluted by the hours that reported nothing.


def downgrade() -> None:
    op.execute(f'DROP MATERIALIZED VIEW IF EXISTS "{SCHEMA}".token_daily')
