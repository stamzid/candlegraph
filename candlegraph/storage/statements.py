"""Every SQL statement candlegraph issues, built per schema.

Keeping the statements here rather than inline at the call sites means the
whole read and write surface of the database is readable in one file, and the
unit tests can assert on it without a connection.

Nothing in this module deletes market data. That is an invariant, not an
oversight: stored history is the input to the analytics phases.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from psycopg2 import sql

# A ready-to-execute statement. Aliased because `DatabaseManager.sql` shadows
# the `sql` module inside that class's body.
SqlQuery = sql.Composed | str


def _columns(*names: str) -> str:
    return ", ".join(names)


def _excluded(names: tuple[str, ...], *, key: tuple[str, ...] = ()) -> str:
    """`col = EXCLUDED.col` for every name outside ``key``.

    The key columns are what the conflict is detected on, so re-assigning them
    is at best a no-op and at worst hides a bug.
    """
    return ",\n            ".join(f"{n} = EXCLUDED.{n}" for n in names if n not in key)


# --- token_hours_data ------------------------------------------------------

TOKEN_HOUR_COLUMNS = (
    "token_id",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "price_usd",
    "volume",
    "volume_usd",
    "untracked_volume_usd",
    "total_value_locked",
    "total_value_locked_usd",
    "fees_usd",
    "period_start_unix",
    "timestamp",
)

_TOKEN_HOUR_TEMPLATE = (
    "(%(id)s,%(symbol)s,%(open)s,%(high)s,%(low)s,%(close)s,%(priceUSD)s,"
    "%(volume)s,%(volumeUSD)s,%(untrackedVolumeUSD)s,%(totalValueLocked)s,"
    "%(totalValueLockedUSD)s,%(feesUSD)s,%(periodStartUnix)s,%(timestamp)s)"
)

# --- token -----------------------------------------------------------------

TOKEN_COLUMNS = (
    "id",
    "name",
    "symbol",
    "total_supply",
    "volume_usd",
    "decimals",
    "volume",
    "fees_usd",
    "tx_count",
    "pool_count",
    "total_value_locked",
    "total_value_locked_usd",
    "derived_eth",
    "updated_at",
)

_TOKEN_TEMPLATE = (
    "(%(id)s,%(name)s,%(symbol)s,%(totalSupply)s,%(volumeUSD)s,%(decimals)s,"
    "%(volume)s,%(feesUSD)s,%(txCount)s,%(poolCount)s,%(totalValueLocked)s,"
    "%(totalValueLockedUSD)s,%(derivedETH)s,%(updated_at)s)"
)

# --- pool ------------------------------------------------------------------

POOL_COLUMNS = (
    "id",
    "token0_id",
    "token1_id",
    "token0_symbol",
    "token1_symbol",
    "fee_tier",
    "created_at_timestamp",
    "liquidity",
    "volume_usd",
    "fees_usd",
    "tx_count",
    "total_value_locked_usd",
    "updated_at",
)

_POOL_TEMPLATE = (
    "(%(id)s,%(token0_id)s,%(token1_id)s,%(token0_symbol)s,%(token1_symbol)s,"
    "%(feeTier)s,%(createdAtTimestamp)s,%(liquidity)s,%(volumeUSD)s,%(feesUSD)s,"
    "%(txCount)s,%(totalValueLockedUSD)s,%(updated_at)s)"
)

# --- pool_hours_data -------------------------------------------------------

POOL_HOUR_COLUMNS = (
    "pool_id",
    "period_start_unix",
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "liquidity",
    "sqrt_price",
    "token0_price",
    "token1_price",
    "tvl_usd",
    "volume_token0",
    "volume_token1",
    "volume_usd",
    "fees_usd",
    "tx_count",
)

_POOL_HOUR_TEMPLATE = (
    "(%(pool_id)s,%(periodStartUnix)s,%(timestamp)s,%(open)s,%(high)s,%(low)s,"
    "%(close)s,%(liquidity)s,%(sqrtPrice)s,%(token0Price)s,%(token1Price)s,"
    "%(tvlUSD)s,%(volumeToken0)s,%(volumeToken1)s,%(volumeUSD)s,%(feesUSD)s,%(txCount)s)"
)


@dataclass(frozen=True)
class Statements:
    """Every SQL statement, bound to a schema name.

    Built per schema rather than hardcoded so a test database can use its own.
    """

    # writes
    insert_token_hours: sql.Composed
    insert_token_hours_template: str
    insert_token: sql.Composed
    insert_token_template: str
    insert_pool: sql.Composed
    insert_pool_template: str
    insert_pool_hours: sql.Composed
    insert_pool_hours_template: str
    # cursors
    get_latest_timestamps: sql.Composed
    get_latest_pool_timestamps: sql.Composed
    # reads
    get_token_metadata: sql.Composed
    chart_query: sql.Composed
    get_tracked_pools: sql.Composed
    get_pools_for_tokens: sql.Composed
    # ledger
    start_ingest_run: sql.Composed
    finish_ingest_run: sql.Composed
    get_recent_runs: sql.Composed
    get_last_successful_run: sql.Composed
    # data quality
    coverage_report: sql.Composed
    missing_hours: sql.Composed
    ohlc_violations: sql.Composed
    field_completeness: sql.Composed


@lru_cache(maxsize=4)
def statements_for(schema: str) -> Statements:
    """SQL for ``schema``, built once per schema name."""
    ns = sql.Identifier(schema)

    # Natural key is (token_id, period_start_unix): one row per token per hour.
    # Re-ingesting the same hour refreshes it instead of failing the batch.
    insert_token_hours = sql.SQL(
        """
        INSERT INTO {ns}.token_hours_data ({cols})
        VALUES %s
        ON CONFLICT (token_id, period_start_unix) DO UPDATE SET
            {updates}
        RETURNING id
        """
    ).format(
        ns=ns,
        cols=sql.SQL(_columns(*TOKEN_HOUR_COLUMNS)),
        updates=sql.SQL(_excluded(TOKEN_HOUR_COLUMNS, key=("token_id", "period_start_unix"))),
    )

    insert_token = sql.SQL(
        """
        INSERT INTO {ns}.token ({cols})
        VALUES %s
        ON CONFLICT (id) DO UPDATE SET
            {updates}
        RETURNING id
        """
    ).format(
        ns=ns,
        cols=sql.SQL(_columns(*TOKEN_COLUMNS)),
        updates=sql.SQL(_excluded(TOKEN_COLUMNS, key=("id",))),
    )

    insert_pool = sql.SQL(
        """
        INSERT INTO {ns}.pool ({cols})
        VALUES %s
        ON CONFLICT (id) DO UPDATE SET
            {updates}
        RETURNING id
        """
    ).format(
        ns=ns,
        cols=sql.SQL(_columns(*POOL_COLUMNS)),
        updates=sql.SQL(_excluded(POOL_COLUMNS, key=("id",))),
    )

    insert_pool_hours = sql.SQL(
        """
        INSERT INTO {ns}.pool_hours_data ({cols})
        VALUES %s
        ON CONFLICT (pool_id, period_start_unix) DO UPDATE SET
            {updates}
        RETURNING id
        """
    ).format(
        ns=ns,
        cols=sql.SQL(_columns(*POOL_HOUR_COLUMNS)),
        updates=sql.SQL(_excluded(POOL_HOUR_COLUMNS, key=("pool_id", "period_start_unix"))),
    )

    get_latest_timestamps = sql.SQL(
        """
        SELECT
            token_id,
            MAX(period_start_unix) AS latest_unix,
            MIN(period_start_unix) AS earliest_unix
        FROM {ns}.token_hours_data
        GROUP BY token_id
        """
    ).format(ns=ns)

    get_latest_pool_timestamps = sql.SQL(
        """
        SELECT
            pool_id,
            MAX(period_start_unix) AS latest_unix,
            MIN(period_start_unix) AS earliest_unix
        FROM {ns}.pool_hours_data
        GROUP BY pool_id
        """
    ).format(ns=ns)

    get_token_metadata = sql.SQL("SELECT * FROM {ns}.token WHERE id = %(token_id)s").format(ns=ns)

    # The bucket label is the bucket's own start, computed from the interval,
    # not MIN(timestamp) of the rows present. A missing first hour must not
    # shift the candle's reported time.
    chart_query = sql.SQL(
        """
        SELECT
            to_timestamp(FLOOR(period_start_unix::numeric / %(interval)s) * %(interval)s) AS interval_start,
            array_agg(open ORDER BY period_start_unix) AS opens,
            array_agg(close ORDER BY period_start_unix DESC) AS closes,
            MAX(high) AS max_high,
            MIN(low) AS min_low,
            AVG(price_usd) AS avg_price_usd,
            SUM(volume) AS total_volume,
            SUM(volume_usd) AS total_volume_usd,
            SUM(fees_usd) AS total_fees_usd,
            AVG(total_value_locked_usd) AS avg_tvl_usd,
            COUNT(*) AS hours
        FROM {ns}.token_hours_data
        WHERE token_id = %(token_id)s
          AND (%(start_unix)s::bigint IS NULL OR period_start_unix >= %(start_unix)s)
          AND (%(end_unix)s::bigint IS NULL OR period_start_unix < %(end_unix)s)
        GROUP BY FLOOR(period_start_unix::numeric / %(interval)s)
        ORDER BY interval_start
        """
    ).format(ns=ns)

    get_tracked_pools = sql.SQL(
        "SELECT * FROM {ns}.pool ORDER BY total_value_locked_usd DESC NULLS LAST"
    ).format(ns=ns)

    get_pools_for_tokens = sql.SQL(
        """
        SELECT * FROM {ns}.pool
        WHERE token0_id = ANY(%(token_ids)s) OR token1_id = ANY(%(token_ids)s)
        ORDER BY total_value_locked_usd DESC NULLS LAST
        """
    ).format(ns=ns)

    # --- ledger ------------------------------------------------------------

    start_ingest_run = sql.SQL(
        """
        INSERT INTO {ns}.ingest_run (run_id, trigger, status, started_at, lookback_days)
        VALUES (%(run_id)s, %(trigger)s, 'running', %(started_at)s, %(lookback_days)s)
        RETURNING id
        """
    ).format(ns=ns)

    finish_ingest_run = sql.SQL(
        """
        UPDATE {ns}.ingest_run SET
            status = %(status)s,
            finished_at = %(finished_at)s,
            duration_seconds = %(duration_seconds)s,
            tokens_written = %(tokens_written)s,
            token_hours_written = %(token_hours_written)s,
            pools_written = %(pools_written)s,
            pool_hours_written = %(pool_hours_written)s,
            errors = %(errors)s,
            detail = %(detail)s
        WHERE run_id = %(run_id)s
        """
    ).format(ns=ns)

    get_recent_runs = sql.SQL(
        "SELECT * FROM {ns}.ingest_run ORDER BY started_at DESC LIMIT %(limit)s"
    ).format(ns=ns)

    get_last_successful_run = sql.SQL(
        """
        SELECT * FROM {ns}.ingest_run
        WHERE status IN ('succeeded', 'partial')
        ORDER BY started_at DESC
        LIMIT 1
        """
    ).format(ns=ns)

    # --- data quality ------------------------------------------------------

    # Coverage over the stored range, per token. `expected_hours` counts the
    # hour slots between the first and last row inclusive, so a token that
    # simply started late is not penalised for the hours before it existed.
    coverage_report = sql.SQL(
        """
        SELECT
            token_id,
            MIN(symbol) AS symbol,
            COUNT(*) AS stored_hours,
            MIN(period_start_unix) AS first_unix,
            MAX(period_start_unix) AS last_unix,
            ((MAX(period_start_unix) - MIN(period_start_unix)) / 3600 + 1) AS expected_hours
        FROM {ns}.token_hours_data
        WHERE (%(start_unix)s::bigint IS NULL OR period_start_unix >= %(start_unix)s)
        GROUP BY token_id
        ORDER BY symbol
        """
    ).format(ns=ns)

    # Every hour slot inside a token's stored range that has no row.
    missing_hours = sql.SQL(
        """
        WITH bounds AS (
            SELECT
                COALESCE(%(start_unix)s::bigint, MIN(period_start_unix)) AS lo,
                COALESCE(%(end_unix)s::bigint, MAX(period_start_unix)) AS hi
            FROM {ns}.token_hours_data
            WHERE token_id = %(token_id)s
        ),
        expected AS (
            SELECT generate_series(bounds.lo, bounds.hi, 3600) AS period_start_unix
            FROM bounds
            WHERE bounds.lo IS NOT NULL
        )
        SELECT expected.period_start_unix
        FROM expected
        LEFT JOIN {ns}.token_hours_data actual
          ON actual.token_id = %(token_id)s
         AND actual.period_start_unix = expected.period_start_unix
        WHERE actual.id IS NULL
        ORDER BY expected.period_start_unix
        """
    ).format(ns=ns)

    # Rows that cannot be true regardless of market conditions.
    ohlc_violations = sql.SQL(
        """
        SELECT token_id, symbol, period_start_unix, open, high, low, close
        FROM {ns}.token_hours_data
        WHERE high < low
           OR open > high OR open < low
           OR close > high OR close < low
           OR price_usd < 0
           OR volume < 0
           OR volume_usd < 0
        ORDER BY token_id, period_start_unix
        LIMIT %(limit)s
        """
    ).format(ns=ns)

    # How much of each enrichment column is actually populated.
    field_completeness = sql.SQL(
        """
        SELECT
            COUNT(*) AS rows_total,
            COUNT(volume) AS volume_present,
            COUNT(volume_usd) AS volume_usd_present,
            COUNT(total_value_locked_usd) AS tvl_usd_present,
            COUNT(fees_usd) AS fees_usd_present
        FROM {ns}.token_hours_data
        WHERE (%(start_unix)s::bigint IS NULL OR period_start_unix >= %(start_unix)s)
        """
    ).format(ns=ns)

    return Statements(
        insert_token_hours=insert_token_hours,
        insert_token_hours_template=_TOKEN_HOUR_TEMPLATE,
        insert_token=insert_token,
        insert_token_template=_TOKEN_TEMPLATE,
        insert_pool=insert_pool,
        insert_pool_template=_POOL_TEMPLATE,
        insert_pool_hours=insert_pool_hours,
        insert_pool_hours_template=_POOL_HOUR_TEMPLATE,
        get_latest_timestamps=get_latest_timestamps,
        get_latest_pool_timestamps=get_latest_pool_timestamps,
        get_token_metadata=get_token_metadata,
        chart_query=chart_query,
        get_tracked_pools=get_tracked_pools,
        get_pools_for_tokens=get_pools_for_tokens,
        start_ingest_run=start_ingest_run,
        finish_ingest_run=finish_ingest_run,
        get_recent_runs=get_recent_runs,
        get_last_successful_run=get_last_successful_run,
        coverage_report=coverage_report,
        missing_hours=missing_hours,
        ohlc_violations=ohlc_violations,
        field_completeness=field_completeness,
    )
