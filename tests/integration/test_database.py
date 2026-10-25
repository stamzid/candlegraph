"""Statements executed against a real PostgreSQL.

Covers what unit tests structurally cannot: that the migrations apply, that the
upsert really is idempotent, and that bucketing produces correct OHLC.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg2
import pytest

from candlegraph.dba import DatabaseManager
from candlegraph.helpers import format_chart_data
from tests.conftest import SHIB, WBTC

pytestmark = pytest.mark.integration

HOUR = 3600
# Buckets are absolute (aligned to the epoch, not to the first row), so the
# fixture starts on a day boundary and every candle width divides it evenly.
BASE = 1_700_000_000 // (24 * HOUR) * (24 * HOUR)


def hour_record(
    index: int,
    *,
    token: str = WBTC,
    symbol: str = "WBTC",
    open_: str = "100",
    high: str = "110",
    low: str = "90",
    close: str = "105",
    price: str = "102",
) -> dict[str, Any]:
    period_start = BASE + index * HOUR
    return {
        "id": token,
        "symbol": symbol,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "priceUSD": price,
        "periodStartUnix": period_start,
        "timestamp": datetime.fromtimestamp(period_start, tz=UTC),
    }


def write_hours(db: DatabaseManager, records: list[dict[str, Any]]) -> int:
    return db.upsert_many(db.sql.insert_token_hours, db.sql.insert_token_hours_template, records)


def chart(db: DatabaseManager, token: str, hours: int) -> list[dict[str, Any]]:
    return db.fetch_all(
        db.sql.chart_query,
        {"interval": hours * HOUR, "token_id": token, "start_unix": None, "end_unix": None},
    )


# --- migrations ------------------------------------------------------------


def test_migrations_create_the_expected_tables(db: DatabaseManager) -> None:
    rows = db.fetch_all(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = %(schema)s",
        {"schema": db.schema},
    )

    assert {"token", "token_hours_data", "alembic_version"} <= {row["table_name"] for row in rows}


def test_the_natural_key_constraint_is_in_place(db: DatabaseManager) -> None:
    rows = db.fetch_all(
        """
        SELECT conname FROM pg_constraint
        WHERE conrelid = (quote_ident(%(schema)s) || '.token_hours_data')::regclass
          AND contype = 'u'
        """,
        {"schema": db.schema},
    )
    names = {row["conname"] for row in rows}

    assert "uix_token_id_period_start_unix" in names
    assert "uix_token_id_timestamp" not in names


def test_period_start_unix_is_not_nullable(db: DatabaseManager) -> None:
    [row] = db.fetch_all(
        """
        SELECT is_nullable FROM information_schema.columns
        WHERE table_schema = %(schema)s AND table_name = 'token_hours_data'
          AND column_name = 'period_start_unix'
        """,
        {"schema": db.schema},
    )

    assert row["is_nullable"] == "NO"


# --- writes ----------------------------------------------------------------


def test_a_batch_insert_writes_every_row(db: DatabaseManager) -> None:
    assert write_hours(db, [hour_record(i) for i in range(5)]) == 5


def test_re_ingesting_the_same_window_is_idempotent(db: DatabaseManager) -> None:
    """A restart re-reads overlapping hours; it must not duplicate them."""
    records = [hour_record(i) for i in range(3)]

    write_hours(db, records)
    write_hours(db, records)

    [row] = db.fetch_all(f'SELECT COUNT(*) AS n FROM "{db.schema}".token_hours_data')
    assert row["n"] == 3


def test_a_duplicate_hour_updates_instead_of_failing_the_batch(db: DatabaseManager) -> None:
    """The old constraint included price_usd, so a revised price failed the lot."""
    write_hours(db, [hour_record(0, close="105", price="102")])

    written = write_hours(db, [hour_record(0, close="999", price="998"), hour_record(1), hour_record(2)])

    assert written == 3
    [row] = db.fetch_all(
        f'SELECT close, price_usd FROM "{db.schema}".token_hours_data WHERE period_start_unix = %(t)s',
        {"t": BASE},
    )
    assert row["close"] == Decimal("999")
    assert row["price_usd"] == Decimal("998")


def test_a_failing_write_raises_rather_than_returning_zero(db: DatabaseManager) -> None:
    """Silent failure here is the data-loss bug this release fixes."""
    broken = hour_record(0)
    broken["symbol"] = None

    with pytest.raises(psycopg2.Error):
        write_hours(db, [broken])

    assert db.fetch_all(f'SELECT 1 FROM "{db.schema}".token_hours_data') == []


def test_token_metadata_upserts_on_conflict(db: DatabaseManager) -> None:
    first = {
        "id": WBTC,
        "name": "Wrapped BTC",
        "symbol": "WBTC",
        "totalSupply": "1",
        "volumeUSD": "2",
        "decimals": "8",
    }
    db.upsert_many(db.sql.insert_token, db.sql.insert_token_template, [first])
    db.upsert_many(db.sql.insert_token, db.sql.insert_token_template, [{**first, "volumeUSD": "99"}])

    rows = db.fetch_all(db.sql.get_token_metadata, {"token_id": WBTC})

    assert len(rows) == 1
    assert rows[0]["volume_usd"] == "99"


def test_timestamps_round_trip_as_utc(db: DatabaseManager) -> None:
    write_hours(db, [hour_record(0)])

    [row] = db.fetch_all(f'SELECT timestamp FROM "{db.schema}".token_hours_data')

    assert row["timestamp"] == datetime.fromtimestamp(BASE, tz=UTC)
    assert row["timestamp"].utcoffset() == datetime.now(tz=UTC).utcoffset()


# --- cursors ---------------------------------------------------------------


def test_latest_timestamps_are_per_token(db: DatabaseManager) -> None:
    write_hours(db, [hour_record(0), hour_record(5)])
    write_hours(db, [hour_record(2, token=SHIB, symbol="SHIB")])

    latest = {row["token_id"]: row["latest_unix"] for row in db.fetch_all(db.sql.get_latest_timestamps)}

    assert latest == {WBTC: BASE + 5 * HOUR, SHIB: BASE + 2 * HOUR}


def test_latest_timestamps_are_empty_on_a_fresh_database(db: DatabaseManager) -> None:
    assert db.fetch_all(db.sql.get_latest_timestamps) == []


# --- pruning ---------------------------------------------------------------


def test_pruning_deletes_only_rows_before_the_cutoff(db: DatabaseManager) -> None:
    write_hours(db, [hour_record(i) for i in range(5)])

    deleted = db.execute(db.sql.delete_older_data, {"interval_start": BASE + 3 * HOUR})

    assert deleted == 3
    remaining = db.fetch_all(f'SELECT period_start_unix FROM "{db.schema}".token_hours_data ORDER BY 1')
    assert [row["period_start_unix"] for row in remaining] == [BASE + 3 * HOUR, BASE + 4 * HOUR]


# --- candle aggregation ----------------------------------------------------


def test_one_hour_candles_return_the_stored_rows(db: DatabaseManager) -> None:
    write_hours(db, [hour_record(i) for i in range(3)])

    rows = chart(db, WBTC, 1)

    assert len(rows) == 3
    assert [row["hours"] for row in rows] == [1, 1, 1]


def test_candles_are_scoped_to_one_token(db: DatabaseManager) -> None:
    write_hours(db, [hour_record(0), hour_record(1)])
    write_hours(db, [hour_record(0, token=SHIB, symbol="SHIB")])

    assert len(chart(db, WBTC, 1)) == 2
    assert len(chart(db, SHIB, 1)) == 1


def test_an_unknown_token_yields_no_buckets(db: DatabaseManager) -> None:
    write_hours(db, [hour_record(0)])

    assert chart(db, "0x" + "11" * 20, 1) == []


@pytest.mark.parametrize("width", [2, 3, 4, 6])
def test_wider_candles_group_the_right_number_of_hours(db: DatabaseManager, width: int) -> None:
    write_hours(db, [hour_record(i) for i in range(12)])

    rows = chart(db, WBTC, width)

    assert len(rows) == 12 // width
    assert all(row["hours"] == width for row in rows)


def test_a_candle_takes_ohlc_from_the_right_hours(db: DatabaseManager) -> None:
    """Open from the first hour, close from the last, high/low across the bucket."""
    write_hours(
        db,
        [
            hour_record(0, open_="100", high="150", low="80", close="120", price="110"),
            hour_record(1, open_="120", high="130", low="60", close="125", price="100"),
            hour_record(2, open_="125", high="200", low="115", close="190", price="150"),
        ],
    )

    [row] = chart(db, WBTC, 3)
    opens, closes, highs, lows, price_usd = format_chart_data([row])

    assert opens[0][2] == 100.0
    assert closes[0][2] == 190.0
    assert highs[0][2] == 200.0
    assert lows[0][2] == 60.0
    assert price_usd[0][2] == pytest.approx(120.0)


def test_the_bucket_label_is_the_boundary_not_the_first_row(db: DatabaseManager) -> None:
    """With hour 0 of the bucket missing, the label must still be the boundary.

    The old query labelled buckets with ``MIN(timestamp)``, so a missing first
    hour silently shifted the candle's reported time.
    """
    write_hours(db, [hour_record(i) for i in (1, 2, 3)])

    [row] = chart(db, WBTC, 4)

    assert row["interval_start"] == datetime.fromtimestamp(BASE, tz=UTC)
    assert row["hours"] == 3


def test_a_gap_produces_no_candle_rather_than_an_empty_one(db: DatabaseManager) -> None:
    """Documented behaviour: buckets with no data are absent, not zero-filled."""
    write_hours(db, [hour_record(0), hour_record(1), hour_record(6), hour_record(7)])

    rows = chart(db, WBTC, 2)

    assert [row["hours"] for row in rows] == [2, 2]
    starts = [int(row["interval_start"].timestamp()) for row in rows]
    assert starts[1] - starts[0] > 2 * HOUR


def test_candles_come_back_in_chronological_order(db: DatabaseManager) -> None:
    write_hours(db, [hour_record(i) for i in (5, 0, 3, 1)])

    rows = chart(db, WBTC, 1)

    assert [row["interval_start"] for row in rows] == sorted(row["interval_start"] for row in rows)


def test_the_time_window_filters_the_buckets(db: DatabaseManager) -> None:
    write_hours(db, [hour_record(i) for i in range(6)])

    rows = db.fetch_all(
        db.sql.chart_query,
        {
            "interval": HOUR,
            "token_id": WBTC,
            "start_unix": BASE + 2 * HOUR,
            "end_unix": BASE + 4 * HOUR,
        },
    )

    assert [int(row["interval_start"].timestamp()) for row in rows] == [BASE + 2 * HOUR, BASE + 3 * HOUR]


def test_the_pool_round_trips(db: DatabaseManager) -> None:
    assert db.ping() is True
