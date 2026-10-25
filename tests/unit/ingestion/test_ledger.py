"""The run ledger: classification, round-trip shape, and staleness."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from candlegraph.ingestion import ledger
from tests.conftest import FakeDatabase

STARTED = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
FINISHED = datetime(2026, 10, 25, 12, 0, 30, tzinfo=UTC)


def params_for(db: FakeDatabase, label: str) -> dict[str, Any]:
    return next(p for name, p in db.executed if name == label)


# --- classification --------------------------------------------------------


def test_a_run_with_no_errors_succeeded() -> None:
    assert ledger.status_for({}, wrote_anything=True) == ledger.SUCCEEDED


def test_a_run_with_no_errors_and_no_rows_still_succeeded() -> None:
    """Nothing new upstream is a success, not a failure."""
    assert ledger.status_for({}, wrote_anything=False) == ledger.SUCCEEDED


def test_some_errors_but_some_rows_is_partial() -> None:
    """Partial must be distinguishable from clean, and from total failure."""
    assert ledger.status_for({"WBTC": "boom"}, wrote_anything=True) == ledger.PARTIAL


def test_errors_and_nothing_written_is_failed() -> None:
    assert ledger.status_for({"WBTC": "boom"}, wrote_anything=False) == ledger.FAILED


# --- opening a run ---------------------------------------------------------


def test_starting_a_run_writes_a_row_with_a_unique_id() -> None:
    db = FakeDatabase()

    first = ledger.start_run(db, trigger=ledger.BACKFILL, lookback_days=90, now=STARTED)
    second = ledger.start_run(db, now=STARTED)

    assert first.run_id != second.run_id
    params = params_for(db, "start_ingest_run")
    assert params["trigger"] == ledger.BACKFILL
    assert params["lookback_days"] == 90
    assert params["started_at"] == STARTED


def test_starting_a_run_defaults_to_the_scheduled_trigger() -> None:
    db = FakeDatabase()

    record = ledger.start_run(db, now=STARTED)

    assert record.trigger == ledger.SCHEDULED
    assert record.started_at == STARTED


def test_a_broken_ledger_does_not_stop_a_run() -> None:
    """Observability failing must never cost us the data."""

    class BrokenDatabase(FakeDatabase):
        def execute(self, query: Any, params: Any = None) -> int:
            raise RuntimeError("ledger table is gone")

    record = ledger.start_run(BrokenDatabase(), now=STARTED)

    assert record.run_id


# --- closing a run ---------------------------------------------------------


def test_finishing_a_run_records_counts_and_duration() -> None:
    db = FakeDatabase()
    record = ledger.start_run(db, now=STARTED)

    ledger.finish_run(
        db,
        record,
        ledger.SUCCEEDED,
        {"tokens_written": 3, "token_hours_written": 400, "pool_hours_written": 90},
        {},
        {"hours_by_symbol": {"WBTC": 168}},
        now=FINISHED,
    )

    params = params_for(db, "finish_ingest_run")
    assert params["run_id"] == record.run_id
    assert params["status"] == ledger.SUCCEEDED
    assert params["duration_seconds"] == 30.0
    assert params["tokens_written"] == 3
    assert params["token_hours_written"] == 400
    assert params["pool_hours_written"] == 90
    assert params["pools_written"] == 0


def test_errors_and_detail_are_stored_as_json() -> None:
    """The columns are JSONB; psycopg2 would reject a bare dict."""
    db = FakeDatabase()
    record = ledger.start_run(db, now=STARTED)

    ledger.finish_run(db, record, ledger.PARTIAL, {}, {"WBTC": "boom"}, {"n": 1}, now=FINISHED)

    params = params_for(db, "finish_ingest_run")
    assert json.loads(params["errors"]) == {"WBTC": "boom"}
    assert json.loads(params["detail"]) == {"n": 1}


def test_detail_survives_values_json_cannot_encode() -> None:
    db = FakeDatabase()
    record = ledger.start_run(db, now=STARTED)

    ledger.finish_run(db, record, ledger.SUCCEEDED, {}, {}, {"when": STARTED}, now=FINISHED)

    assert "2026-10-25" in json.loads(params_for(db, "finish_ingest_run")["detail"])["when"]


def test_a_clock_that_goes_backwards_does_not_yield_a_negative_duration() -> None:
    record = ledger.RunRecord(started_at=FINISHED)

    assert record.duration_seconds(STARTED) == 0.0


def test_closing_a_run_tolerates_a_broken_ledger() -> None:
    class BrokenDatabase(FakeDatabase):
        def execute(self, query: Any, params: Any = None) -> int:
            raise RuntimeError("ledger table is gone")

    ledger.finish_run(BrokenDatabase(), ledger.RunRecord(), ledger.SUCCEEDED, {}, {})


# --- reading the ledger ----------------------------------------------------


def test_recent_runs_passes_the_limit_through() -> None:
    db = FakeDatabase(get_recent_runs=[{"run_id": "a"}])

    assert ledger.recent_runs(db, limit=5) == [{"run_id": "a"}]
    assert ("get_recent_runs", {"limit": 5}) in db.fetches


def test_last_successful_run_is_none_on_a_fresh_database() -> None:
    assert ledger.last_successful_run(FakeDatabase()) is None


def test_staleness_is_none_when_nothing_has_ever_succeeded() -> None:
    assert ledger.seconds_since_last_success(FakeDatabase()) is None


def test_staleness_is_measured_from_the_finish_time() -> None:
    db = FakeDatabase(get_last_successful_run=[{"finished_at": FINISHED}])

    age = ledger.seconds_since_last_success(db, now=FINISHED + timedelta(minutes=10))

    assert age == pytest.approx(600.0)


def test_staleness_falls_back_to_the_start_time_for_a_run_still_open() -> None:
    db = FakeDatabase(get_last_successful_run=[{"started_at": STARTED, "finished_at": None}])

    assert ledger.seconds_since_last_success(db, now=STARTED + timedelta(minutes=1)) == pytest.approx(60.0)


def test_a_naive_stored_timestamp_is_read_as_utc() -> None:
    """Postgres can hand back a naive datetime; assuming local time would lie."""
    naive = FINISHED.replace(tzinfo=None)
    db = FakeDatabase(get_last_successful_run=[{"finished_at": naive}])

    assert ledger.seconds_since_last_success(db, now=FINISHED) == pytest.approx(0.0)
