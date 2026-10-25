"""Pool tables, the run ledger, and the quality report against real SQL.

These exercise the parts of phase 2 that only a database can prove: the pool
natural key, JSONB round-tripping in the ledger, and ``generate_series``-based
gap detection.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from candlegraph.ingestion import ledger
from candlegraph.ingestion.quality import build_report, find_gaps
from candlegraph.storage import DatabaseManager
from tests.conftest import SHIB, WBTC

pytestmark = pytest.mark.integration

HOUR = 3600
BASE = 1_700_000_000 // (24 * HOUR) * (24 * HOUR)
POOL_A = "0x" + "aa" * 20
POOL_B = "0x" + "bb" * 20


def pool_record(pool_id: str = POOL_A, tvl: str = "1000000", tx_count: int = 10) -> dict[str, Any]:
    return {
        "id": pool_id,
        "token0_id": WBTC,
        "token1_id": SHIB,
        "token0_symbol": "WBTC",
        "token1_symbol": "SHIB",
        "feeTier": 3000,
        "createdAtTimestamp": BASE,
        "liquidity": "123456",
        "volumeUSD": "50000",
        "feesUSD": "150",
        "txCount": tx_count,
        "totalValueLockedUSD": tvl,
        "updated_at": datetime.now(tz=UTC),
    }


def pool_hour_record(index: int, pool_id: str = POOL_A, tx_count: int = 5) -> dict[str, Any]:
    period_start = BASE + index * HOUR
    return {
        "pool_id": pool_id,
        "periodStartUnix": period_start,
        "timestamp": datetime.fromtimestamp(period_start, tz=UTC),
        "open": "1.0",
        "high": "1.2",
        "low": "0.9",
        "close": "1.1",
        "liquidity": "123456",
        "sqrtPrice": "999",
        "token0Price": "0.5",
        "token1Price": "2.0",
        "tvlUSD": "1000000",
        "volumeToken0": "10",
        "volumeToken1": "20",
        "volumeUSD": "1500",
        "feesUSD": "4.5",
        "txCount": tx_count,
    }


def token_hour_record(index: int, token: str = WBTC, symbol: str = "WBTC") -> dict[str, Any]:
    period_start = BASE + index * HOUR
    return {
        "id": token,
        "symbol": symbol,
        "open": "100",
        "high": "110",
        "low": "90",
        "close": "105",
        "priceUSD": "102",
        "volume": "12.5",
        "volumeUSD": "1275",
        "untrackedVolumeUSD": "1275",
        "totalValueLocked": "40",
        "totalValueLockedUSD": "5000000",
        "feesUSD": "3.82",
        "periodStartUnix": period_start,
        "timestamp": datetime.fromtimestamp(period_start, tz=UTC),
    }


def write_pools(db: DatabaseManager, rows: list[dict[str, Any]]) -> int:
    return db.upsert_many(db.sql.insert_pool, db.sql.insert_pool_template, rows)


def write_pool_hours(db: DatabaseManager, rows: list[dict[str, Any]]) -> int:
    return db.upsert_many(db.sql.insert_pool_hours, db.sql.insert_pool_hours_template, rows)


def write_token_hours(db: DatabaseManager, rows: list[dict[str, Any]]) -> int:
    return db.upsert_many(db.sql.insert_token_hours, db.sql.insert_token_hours_template, rows)


@pytest.fixture
def clean_db(db: DatabaseManager) -> DatabaseManager:
    db.execute(f'TRUNCATE "{db.schema}".pool_hours_data, "{db.schema}".pool, "{db.schema}".ingest_run')
    return db


# --- migration shape -------------------------------------------------------


def test_the_new_tables_exist(clean_db: DatabaseManager) -> None:
    rows = clean_db.fetch_all(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = %(schema)s",
        {"schema": clean_db.schema},
    )

    assert {"pool", "pool_hours_data", "ingest_run"} <= {row["table_name"] for row in rows}


@pytest.mark.parametrize(
    "column",
    [
        "volume",
        "volume_usd",
        "untracked_volume_usd",
        "total_value_locked",
        "total_value_locked_usd",
        "fees_usd",
    ],
)
def test_token_hours_gained_the_market_depth_columns(clean_db: DatabaseManager, column: str) -> None:
    rows = clean_db.fetch_all(
        """
        SELECT is_nullable FROM information_schema.columns
        WHERE table_schema = %(schema)s AND table_name = 'token_hours_data'
          AND column_name = %(column)s
        """,
        {"schema": clean_db.schema, "column": column},
    )

    # Nullable on purpose: rows written before this revision have no value,
    # and NULL ("not collected") must stay distinguishable from 0.
    assert rows and rows[0]["is_nullable"] == "YES"


def test_the_pool_natural_key_is_in_place(clean_db: DatabaseManager) -> None:
    rows = clean_db.fetch_all(
        """
        SELECT conname FROM pg_constraint
        WHERE conrelid = (quote_ident(%(schema)s) || '.pool_hours_data')::regclass
          AND contype = 'u'
        """,
        {"schema": clean_db.schema},
    )

    assert "uix_pool_id_period_start_unix" in {row["conname"] for row in rows}


# --- pool writes -----------------------------------------------------------


def test_pools_upsert_on_conflict(clean_db: DatabaseManager) -> None:
    write_pools(clean_db, [pool_record(tvl="1")])
    write_pools(clean_db, [pool_record(tvl="999")])

    rows = clean_db.fetch_all(f'SELECT * FROM "{clean_db.schema}".pool')

    assert len(rows) == 1
    assert rows[0]["total_value_locked_usd"] == 999
    assert rows[0]["token0_symbol"] == "WBTC"


def test_pool_hours_are_idempotent(clean_db: DatabaseManager) -> None:
    rows = [pool_hour_record(i) for i in range(3)]

    write_pool_hours(clean_db, rows)
    written = write_pool_hours(clean_db, rows)

    assert written == 3
    [count] = clean_db.fetch_all(f'SELECT COUNT(*) AS n FROM "{clean_db.schema}".pool_hours_data')
    assert count["n"] == 3


def test_a_revised_pool_hour_updates_in_place(clean_db: DatabaseManager) -> None:
    write_pool_hours(clean_db, [pool_hour_record(0, tx_count=5)])
    write_pool_hours(clean_db, [pool_hour_record(0, tx_count=42)])

    [row] = clean_db.fetch_all(f'SELECT tx_count FROM "{clean_db.schema}".pool_hours_data')

    assert row["tx_count"] == 42


def test_transaction_counts_are_stored(clean_db: DatabaseManager) -> None:
    """The whole reason pool data exists: tx counts are not on the token entity."""
    write_pool_hours(clean_db, [pool_hour_record(i, tx_count=7) for i in range(4)])

    [row] = clean_db.fetch_all(f'SELECT SUM(tx_count) AS total FROM "{clean_db.schema}".pool_hours_data')

    assert row["total"] == 28


def test_pool_cursors_are_per_pool(clean_db: DatabaseManager) -> None:
    write_pool_hours(clean_db, [pool_hour_record(0), pool_hour_record(5)])
    write_pool_hours(clean_db, [pool_hour_record(2, pool_id=POOL_B)])

    latest = {
        row["pool_id"]: row["latest_unix"]
        for row in clean_db.fetch_all(clean_db.sql.get_latest_pool_timestamps)
    }

    assert latest == {POOL_A: BASE + 5 * HOUR, POOL_B: BASE + 2 * HOUR}


def test_pools_are_found_from_either_side_of_the_pair(clean_db: DatabaseManager) -> None:
    write_pools(clean_db, [pool_record()])

    by_token0 = clean_db.fetch_all(clean_db.sql.get_pools_for_tokens, {"token_ids": [WBTC]})
    by_token1 = clean_db.fetch_all(clean_db.sql.get_pools_for_tokens, {"token_ids": [SHIB]})
    unrelated = clean_db.fetch_all(clean_db.sql.get_pools_for_tokens, {"token_ids": ["0xzz"]})

    assert len(by_token0) == len(by_token1) == 1
    assert unrelated == []


def test_tracked_pools_come_back_deepest_first(clean_db: DatabaseManager) -> None:
    write_pools(clean_db, [pool_record(POOL_A, tvl="10"), pool_record(POOL_B, tvl="900")])

    rows = clean_db.fetch_all(clean_db.sql.get_tracked_pools)

    assert [row["id"] for row in rows] == [POOL_B, POOL_A]


# --- the ledger ------------------------------------------------------------


def test_a_run_round_trips_through_the_ledger(clean_db: DatabaseManager) -> None:
    started = datetime(2026, 10, 25, 6, 0, tzinfo=UTC)
    record = ledger.start_run(clean_db, trigger=ledger.BACKFILL, lookback_days=90, now=started)

    ledger.finish_run(
        clean_db,
        record,
        ledger.SUCCEEDED,
        {"tokens_written": 3, "token_hours_written": 409, "pool_hours_written": 120},
        {},
        {"hours_by_symbol": {"WBTC": 168}},
        now=started + timedelta(seconds=12),
    )

    [row] = ledger.recent_runs(clean_db, limit=1)
    assert row["status"] == "succeeded"
    assert row["trigger"] == "backfill"
    assert row["lookback_days"] == 90
    assert row["token_hours_written"] == 409
    assert float(row["duration_seconds"]) == pytest.approx(12.0)
    # JSONB comes back as a dict, which is what the observer phase will read.
    assert row["detail"]["hours_by_symbol"]["WBTC"] == 168


def test_an_open_run_is_visible_while_it_is_running(clean_db: DatabaseManager) -> None:
    ledger.start_run(clean_db, now=datetime(2026, 10, 25, 6, 0, tzinfo=UTC))

    [row] = ledger.recent_runs(clean_db, limit=1)

    assert row["status"] == "running"
    assert row["finished_at"] is None


def test_errors_round_trip_as_jsonb(clean_db: DatabaseManager) -> None:
    record = ledger.start_run(clean_db)
    ledger.finish_run(clean_db, record, ledger.PARTIAL, {}, {"WBTC": "gateway 503"})

    [row] = ledger.recent_runs(clean_db, limit=1)

    assert row["errors"] == {"WBTC": "gateway 503"}


def test_recent_runs_are_newest_first(clean_db: DatabaseManager) -> None:
    for hour in range(3):
        ledger.start_run(clean_db, now=datetime(2026, 10, 25, hour, tzinfo=UTC))

    rows = ledger.recent_runs(clean_db, limit=10)

    assert [row["started_at"].hour for row in rows] == [2, 1, 0]


def test_a_failed_run_is_not_treated_as_the_last_success(clean_db: DatabaseManager) -> None:
    good = ledger.start_run(clean_db, now=datetime(2026, 10, 25, 1, tzinfo=UTC))
    ledger.finish_run(
        clean_db, good, ledger.SUCCEEDED, {}, {}, now=datetime(2026, 10, 25, 1, 0, 5, tzinfo=UTC)
    )
    bad = ledger.start_run(clean_db, now=datetime(2026, 10, 25, 2, tzinfo=UTC))
    ledger.finish_run(
        clean_db, bad, ledger.FAILED, {}, {"__run__": "down"}, now=datetime(2026, 10, 25, 2, 0, 5, tzinfo=UTC)
    )

    last = ledger.last_successful_run(clean_db)

    assert last is not None
    assert last["run_id"] == good.run_id


def test_staleness_is_measured_against_the_last_success(clean_db: DatabaseManager) -> None:
    record = ledger.start_run(clean_db, now=datetime(2026, 10, 25, 1, tzinfo=UTC))
    ledger.finish_run(
        clean_db, record, ledger.SUCCEEDED, {}, {}, now=datetime(2026, 10, 25, 1, 0, 0, tzinfo=UTC)
    )

    age = ledger.seconds_since_last_success(clean_db, now=datetime(2026, 10, 25, 2, 0, 0, tzinfo=UTC))

    assert age == pytest.approx(3600.0)


# --- quality ---------------------------------------------------------------


def test_a_complete_series_has_no_gaps(clean_db: DatabaseManager) -> None:
    write_token_hours(clean_db, [token_hour_record(i) for i in range(24)])

    assert find_gaps(clean_db, WBTC) == []


def test_a_missing_hour_is_found(clean_db: DatabaseManager) -> None:
    write_token_hours(clean_db, [token_hour_record(i) for i in (0, 1, 3, 4)])

    [gap] = find_gaps(clean_db, WBTC)

    assert gap.hours == 1
    assert gap.start_unix == BASE + 2 * HOUR


def test_consecutive_missing_hours_are_one_gap(clean_db: DatabaseManager) -> None:
    write_token_hours(clean_db, [token_hour_record(i) for i in (0, 5, 6)])

    [gap] = find_gaps(clean_db, WBTC)

    assert gap.hours == 4
    assert gap.start_unix == BASE + HOUR


def test_gaps_are_bounded_by_the_stored_range(clean_db: DatabaseManager) -> None:
    """A token that simply started late must not be reported as one huge gap."""
    write_token_hours(clean_db, [token_hour_record(i) for i in (10, 11, 12)])

    assert find_gaps(clean_db, WBTC) == []


def test_a_token_with_no_rows_has_no_gaps(clean_db: DatabaseManager) -> None:
    assert find_gaps(clean_db, WBTC) == []


def test_the_report_measures_coverage_for_real(clean_db: DatabaseManager) -> None:
    write_token_hours(clean_db, [token_hour_record(i) for i in range(10)])
    write_token_hours(clean_db, [token_hour_record(i, SHIB, "SHIB") for i in (0, 2, 4, 6, 8)])

    report = build_report(clean_db, expected_symbols=("WBTC", "SHIB"), min_expected_coverage=0.8)

    by_symbol = {token.symbol: token for token in report.tokens}
    assert by_symbol["WBTC"].coverage == 1.0
    assert by_symbol["SHIB"].coverage == pytest.approx(5 / 9)
    assert [t.symbol for t in report.under_covered] == ["SHIB"]
    assert len(by_symbol["SHIB"].gaps) == 4


def test_the_report_flags_a_token_that_ingested_nothing(clean_db: DatabaseManager) -> None:
    write_token_hours(clean_db, [token_hour_record(i) for i in range(5)])

    report = build_report(clean_db, expected_symbols=("WBTC", "GNO"))

    assert report.missing_tokens == ["GNO"]
    assert not report.ok


def test_the_report_detects_an_impossible_row(clean_db: DatabaseManager) -> None:
    broken = token_hour_record(0)
    broken["high"] = "1"
    broken["low"] = "99"
    write_token_hours(clean_db, [broken])

    report = build_report(clean_db)

    assert len(report.violations) == 1
    assert not report.ok


def test_the_report_measures_field_completeness(clean_db: DatabaseManager) -> None:
    full = [token_hour_record(i) for i in range(3)]
    sparse = token_hour_record(3)
    sparse["volume"] = None
    sparse["fees_usd"] = None
    sparse["feesUSD"] = None
    write_token_hours(clean_db, [*full, sparse])

    report = build_report(clean_db)

    assert report.completeness["rows_total"] == 4
    assert report.completeness["volume_present"] == 3
    assert report.completeness["fees_usd_present"] == 3


def test_a_healthy_database_reports_ok(clean_db: DatabaseManager) -> None:
    write_token_hours(clean_db, [token_hour_record(i) for i in range(24)])

    report = build_report(clean_db, expected_symbols=("WBTC",))

    assert report.ok
    assert report.total_rows == 24
