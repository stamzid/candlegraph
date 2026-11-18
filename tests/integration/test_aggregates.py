"""`token_daily` against real SQL: does it hold the same candles as the rows?

The risk a continuous aggregate introduces is not slowness, it is **two
definitions of a candle that can drift apart**. Most of this file is one
comparison run several ways: aggregate the hourly rows, read the aggregate,
assert the answers are identical. Everything else here exists because it is a
way the two could disagree — a missing hour, a NULL column, a backfill that
rewrote the past, a bucket that is still filling.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from candlegraph.api.schema import chart_statement
from candlegraph.harness import JOBS, Outcome, run_job
from candlegraph.storage import DatabaseManager
from tests.conftest import WBTC, make_settings

pytestmark = pytest.mark.integration

HOUR = 3600
DAY = 24 * HOUR
# Fixed and old, so "the newest bucket" is never the real current day.
BASE = 1_700_000_000 // DAY * DAY


def hour_row(index: int, price: float, **overrides: Any) -> dict[str, Any]:
    period_start = BASE + index * HOUR
    row = {
        "id": WBTC,
        "symbol": "WBTC",
        "open": price,
        "high": price + 1,
        "low": price - 1,
        "close": price + 0.5,
        "priceUSD": price,
        "volume": "1",
        "volumeUSD": "1000",
        "untrackedVolumeUSD": "1000",
        "totalValueLocked": "10",
        "totalValueLockedUSD": "5000",
        "feesUSD": "3",
        "periodStartUnix": period_start,
        "timestamp": datetime.fromtimestamp(period_start, tz=UTC),
    }
    row.update(overrides)
    return row


def write(db: DatabaseManager, rows: list[dict[str, Any]]) -> None:
    db.upsert_many(db.sql.insert_token_hours, db.sql.insert_token_hours_template, rows)


def refresh(db: DatabaseManager) -> None:
    db.execute_outside_transaction(db.sql.refresh_token_daily)


@pytest.fixture
def agg_db(db: DatabaseManager) -> DatabaseManager:
    db.execute(f'TRUNCATE "{db.schema}".token_hours_data')
    refresh(db)  # drop whatever a previous test materialised
    return db


def candles(db: DatabaseManager, interval: int, *, aggregate: bool) -> list[dict[str, Any]]:
    statement = db.sql.chart_query_daily if aggregate else db.sql.chart_query
    return db.fetch_all(
        statement,
        {"interval": interval, "token_id": WBTC, "start_unix": None, "end_unix": None},
    )


def comparable(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    """One tuple per bucket, with numerics rounded past float noise."""

    def number(value: Any) -> Any:
        return None if value is None else round(float(value), 6)

    return [
        (
            row["interval_start"],
            number((row["opens"] or [None])[0]),
            number((row["closes"] or [None])[0]),
            number(row["max_high"]),
            number(row["min_low"]),
            number(row["avg_price_usd"]),
            number(row["total_volume_usd"]),
            number(row["avg_tvl_usd"]),
            int(row["hours"]),
        )
        for row in rows
    ]


# --- the comparison ---------------------------------------------------------


@pytest.mark.parametrize("interval", [DAY, 2 * DAY, 7 * DAY, 30 * DAY])
def test_the_aggregate_and_the_raw_path_agree(agg_db: DatabaseManager, interval: int) -> None:
    """The test the whole aggregate rests on.

    If these ever differ, the API's answer depends on which statement ran,
    which is worse than the aggregate being slow.
    """
    write(agg_db, [hour_row(i, 100.0 + (i % 37)) for i in range(60 * 24)])
    refresh(agg_db)

    assert comparable(candles(agg_db, interval, aggregate=True)) == comparable(
        candles(agg_db, interval, aggregate=False)
    )


def test_they_agree_when_hours_are_missing(agg_db: DatabaseManager) -> None:
    """The case an average-of-averages gets wrong.

    Day 0 has four hours and day 1 has twenty-four. A weekly average built
    from daily averages would weight them equally; the stored sum and count
    do not, which is why the aggregate materialises those and divides late.
    """
    rows = [hour_row(i, 100.0) for i in range(4)]
    rows += [hour_row(24 + i, 200.0) for i in range(24)]
    write(agg_db, rows)
    refresh(agg_db)

    weekly_agg = candles(agg_db, 7 * DAY, aggregate=True)
    weekly_raw = candles(agg_db, 7 * DAY, aggregate=False)

    assert comparable(weekly_agg) == comparable(weekly_raw)
    # And the number is the hour-weighted one, not (100 + 200) / 2.
    expected = (4 * 100.0 + 24 * 200.0) / 28
    assert round(float(weekly_agg[0]["avg_price_usd"]), 6) == round(expected, 6)


def test_a_not_collected_column_stays_null_rather_than_becoming_a_number(
    agg_db: DatabaseManager,
) -> None:
    """Invariant 11 through the aggregate: NULL means not collected.

    If the average divided by `hours` instead of the count of non-NULL
    values, a day where half the hours reported no TVL would read as half
    the real figure.
    """
    rows = [hour_row(i, 100.0, totalValueLockedUSD=None) for i in range(12)]
    rows += [hour_row(12 + i, 100.0, totalValueLockedUSD="8000") for i in range(12)]
    write(agg_db, rows)
    refresh(agg_db)

    (bucket,) = candles(agg_db, DAY, aggregate=True)
    assert round(float(bucket["avg_tvl_usd"]), 6) == 8000.0
    assert comparable(candles(agg_db, DAY, aggregate=True)) == comparable(
        candles(agg_db, DAY, aggregate=False)
    )


def test_a_day_with_no_rows_produces_no_bucket(agg_db: DatabaseManager) -> None:
    """Invariant 6 survives the aggregate: gaps are absent, not zero-filled."""
    write(agg_db, [hour_row(i, 100.0) for i in range(24)])
    write(agg_db, [hour_row(72 + i, 100.0) for i in range(24)])  # day 3; day 1 and 2 empty
    refresh(agg_db)

    daily = candles(agg_db, DAY, aggregate=True)

    assert len(daily) == 2
    assert comparable(daily) == comparable(candles(agg_db, DAY, aggregate=False))


def test_the_open_is_the_first_hour_and_the_close_the_last(agg_db: DatabaseManager) -> None:
    """Ordering is the thing `first`/`last` buy; a plain MIN/MAX would not."""
    write(agg_db, [hour_row(i, 100.0 + i) for i in range(24)])
    refresh(agg_db)

    (bucket,) = candles(agg_db, DAY, aggregate=True)

    assert round(float(bucket["opens"][0]), 6) == 100.0  # hour 0's open
    assert round(float(bucket["closes"][0]), 6) == round(100.0 + 23 + 0.5, 6)  # hour 23's close


# --- staying in step --------------------------------------------------------


def test_a_backfill_that_rewrites_an_old_hour_is_picked_up_by_a_refresh(
    agg_db: DatabaseManager,
) -> None:
    """Invariant 13's case, and the reason the refresh covers the whole range.

    A windowed refresh would leave this bucket wrong until somebody noticed.
    """
    write(agg_db, [hour_row(i, 100.0) for i in range(400 * 24)])
    refresh(agg_db)
    first_day_before = candles(agg_db, DAY, aggregate=True)[0]

    # Rewrite one hour, a year back.
    write(agg_db, [hour_row(3, 999.0)])
    refresh(agg_db)
    first_day_after = candles(agg_db, DAY, aggregate=True)[0]

    assert first_day_after["max_high"] != first_day_before["max_high"]
    assert comparable(candles(agg_db, DAY, aggregate=True)) == comparable(
        candles(agg_db, DAY, aggregate=False)
    )


def watermark(db: DatabaseManager) -> int:
    """How far `token_daily` has been materialised.

    Everything after it is computed live from the hourly rows on every read
    (real-time aggregation); everything before it comes from the stored
    buckets. The watermark is a high-water mark — it moves forward with each
    refresh and never backwards — so "before it" means "anywhere the
    aggregate has already covered", which after a while is all of history.
    """
    (row,) = db.fetch_all(
        "SELECT _timescaledb_functions.cagg_watermark(mat_hypertable_id) AS w "
        "FROM _timescaledb_catalog.continuous_agg WHERE user_view_name = 'token_daily'"
    )
    return int(row["w"])


def test_an_hour_newer_than_the_watermark_needs_no_refresh(agg_db: DatabaseManager) -> None:
    """Real-time aggregation covers the hours ingest has just written.

    This is the normal case and the reason `materialized_only` is left off:
    between pipeline runs the API still answers from the newest data, so what
    it returns does not depend on when the refresh last ran.
    """
    start = max(BASE, watermark(agg_db)) // DAY * DAY + DAY
    first = [hour_row(0, 100.0) | {"periodStartUnix": start + i * HOUR} for i in range(24)]
    write(agg_db, _stamped(first))
    refresh(agg_db)

    # A new day, after the watermark the refresh above just set.
    later = [hour_row(0, 500.0) | {"periodStartUnix": start + DAY + i * HOUR} for i in range(5)]
    write(agg_db, _stamped(later))

    daily = candles(agg_db, DAY, aggregate=True)
    assert len(daily) >= 2
    assert comparable(daily) == comparable(candles(agg_db, DAY, aggregate=False))


def test_an_hour_written_behind_the_watermark_waits_for_a_refresh(
    agg_db: DatabaseManager,
) -> None:
    """The other half, and the reason the refresh is a step of every hourly run.

    A backfill rewrites hours the aggregate has already materialised. Those
    are served from the stored buckets until something refreshes, so an
    aggregate with no refresh job would drift from the rows behind it. The
    drift is bounded by one pipeline cycle, and `AGGREGATES_ENABLED=false`
    sidesteps it entirely.
    """
    start = max(BASE, watermark(agg_db)) // DAY * DAY + DAY
    rows = [hour_row(0, 100.0) | {"periodStartUnix": start + i * HOUR} for i in range(48)]
    write(agg_db, _stamped(rows))
    refresh(agg_db)
    before = comparable(candles(agg_db, DAY, aggregate=True))

    # Rewrite the first hour, which is now well behind the watermark.
    rewritten = hour_row(0, 100.0) | {"periodStartUnix": start, "high": 9999}
    write(agg_db, _stamped([rewritten]))

    assert comparable(candles(agg_db, DAY, aggregate=True)) == before  # still the old number
    refresh(agg_db)
    after = comparable(candles(agg_db, DAY, aggregate=True))
    assert after != before
    assert after == comparable(candles(agg_db, DAY, aggregate=False))


def _stamped(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep `timestamp` consistent with an overridden `periodStartUnix`."""
    for row in rows:
        row["timestamp"] = datetime.fromtimestamp(int(row["periodStartUnix"]), tz=UTC)
    return rows


# --- the job ----------------------------------------------------------------


def test_the_refresh_job_records_what_the_view_holds(agg_db: DatabaseManager) -> None:
    write(agg_db, [hour_row(i, 100.0) for i in range(72)])

    run = run_job(JOBS["refresh_aggregates"], make_settings(), db=agg_db)

    assert run.outcome is Outcome.SUCCEEDED
    assert run.result is not None
    assert run.result.counts == {"buckets": 3, "tokens": 1, "hours": 72}
    assert run.result.detail["view"] == "token_daily"


def test_running_refresh_aggregates_twice_writes_the_same_state(agg_db: DatabaseManager) -> None:
    """Named by tests/unit/harness/test_jobs.py::IDEMPOTENCE_PROOFS.

    The second refresh finds nothing invalidated and leaves the view as it
    was — which is what makes it safe as a retried pipeline step.
    """
    write(agg_db, [hour_row(i, 100.0) for i in range(72)])

    first = run_job(JOBS["refresh_aggregates"], make_settings(), db=agg_db)
    after_one = comparable(candles(agg_db, DAY, aggregate=True))
    second = run_job(JOBS["refresh_aggregates"], make_settings(), db=agg_db)

    assert comparable(candles(agg_db, DAY, aggregate=True)) == after_one
    assert first.result is not None and second.result is not None
    assert first.result.counts == second.result.counts
    assert first.outcome is second.outcome is Outcome.SUCCEEDED


def test_refreshing_is_possible_from_a_pooled_connection(agg_db: DatabaseManager) -> None:
    """`refresh_continuous_aggregate()` refuses to run inside a transaction.

    psycopg2 opens one before the first statement, so the ordinary write
    helper cannot call it. This pins the autocommit path that can.
    """
    write(agg_db, [hour_row(i, 100.0) for i in range(24)])

    with pytest.raises(Exception, match="transaction block"):
        agg_db.execute(agg_db.sql.refresh_token_daily)

    agg_db.execute_outside_transaction(agg_db.sql.refresh_token_daily)
    assert candles(agg_db, DAY, aggregate=True)


# --- routing ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("hours", "expect_aggregate"),
    [(1, False), (4, False), (23, False), (24, True), (48, True), (168, True), (25, False)],
)
def test_only_whole_days_are_served_from_the_aggregate(
    agg_db: DatabaseManager, hours: int, expect_aggregate: bool
) -> None:
    """A four-hour candle cannot be built from daily buckets."""
    chosen = chart_statement(agg_db, hours * HOUR, make_settings())

    assert (chosen is agg_db.sql.chart_query_daily) is expect_aggregate


def test_the_setting_pins_every_request_to_the_hourly_rows(agg_db: DatabaseManager) -> None:
    chosen = chart_statement(agg_db, DAY, make_settings(aggregates_enabled=False))

    assert chosen is agg_db.sql.chart_query
