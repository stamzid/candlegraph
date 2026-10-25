"""The agent layer against real SQL: the agent_run table and an observer run.

The unit tier proves the judgement; this tier proves the plumbing — JSONB
round-trips, the ledger statements, and the observer reading a database with
a stale ledger and a real gap through the same statements production uses.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg2
import pytest

from candlegraph.agents.base import AgentReport, Finding, Severity
from candlegraph.agents.ledger import recent_agent_runs, record_agent_run
from candlegraph.agents.observer import run_observer
from candlegraph.agents.reporting import LedgerSink
from candlegraph.ingestion import ledger
from candlegraph.storage import DatabaseManager
from candlegraph.tokens import TokenRegistry
from tests.conftest import SHIB, WBTC, make_settings

pytestmark = pytest.mark.integration

HOUR = 3600
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


@pytest.fixture
def agent_db(db: DatabaseManager) -> DatabaseManager:
    db.execute(f'TRUNCATE "{db.schema}".agent_run, "{db.schema}".ingest_run')
    return db


def hour_record(token_id: str, symbol: str, unix: int, volume_usd: float) -> dict[str, Any]:
    return {
        "id": token_id,
        "symbol": symbol,
        "open": "100",
        "high": "101",
        "low": "99",
        "close": "100.5",
        "priceUSD": "100.5",
        "volume": "10",
        "volumeUSD": str(volume_usd),
        "untrackedVolumeUSD": None,
        "totalValueLocked": None,
        "totalValueLockedUSD": "1000",
        "feesUSD": "1",
        "periodStartUnix": unix,
        "timestamp": datetime.fromtimestamp(unix, tz=UTC),
    }


def seed_hours(
    db: DatabaseManager, token_id: str, symbol: str, hours: int, volume_usd: float, skip: range | None = None
) -> None:
    end = int(NOW.timestamp()) // HOUR * HOUR
    rows = [
        hour_record(token_id, symbol, end - i * HOUR, volume_usd)
        for i in range(1, hours + 1)
        if skip is None or i not in skip
    ]
    db.upsert_many(db.sql.insert_token_hours, db.sql.insert_token_hours_template, rows)


def seed_run(db: DatabaseManager, finished: datetime, status: str = "succeeded") -> None:
    record = ledger.start_run(db, now=finished - timedelta(seconds=10))
    ledger.finish_run(db, record, status, {"token_hours_written": 20}, {}, now=finished)


# --- the table -------------------------------------------------------------


def test_an_agent_run_round_trips_with_its_findings_as_jsonb(agent_db: DatabaseManager) -> None:
    report = AgentReport(
        "observer",
        "0f3b7c1e-5c2a-4d2b-9f3e-1a2b3c4d5e6f",
        NOW,
        NOW + timedelta(seconds=2),
        [Finding("gap", Severity.WARNING, "WETH", "7h gap", {"hours": 7})],
        summary="observer finished WARNING",
        markdown="# report",
        detail={"symbols_judged": 2},
    )

    assert record_agent_run(agent_db, report) == 1
    (row,) = recent_agent_runs(agent_db)

    assert row["agent"] == "observer" and row["status"] == "warning" and row["findings_total"] == 1
    assert row["findings"][0] == {
        "kind": "gap",
        "severity": "warning",
        "subject": "WETH",
        "message": "7h gap",
        "evidence": {"hours": 7},
    }
    assert row["detail"] == {"symbols_judged": 2, "error": None}
    assert float(row["duration_seconds"]) == 2.0
    assert row["finished_at"].tzinfo is not None


def test_recent_runs_filter_by_agent_and_come_newest_first(agent_db: DatabaseManager) -> None:
    for i, agent in enumerate(("observer", "janitor", "observer")):
        record_agent_run(
            agent_db,
            AgentReport(
                agent,
                f"00000000-0000-0000-0000-00000000000{i}",
                NOW + timedelta(minutes=i),
                NOW + timedelta(minutes=i),
            ),
        )

    assert [r["run_id"][-1] for r in recent_agent_runs(agent_db)] == ["2", "1", "0"]
    assert [r["agent"] for r in recent_agent_runs(agent_db, agent="janitor")] == ["janitor"]
    assert len(recent_agent_runs(agent_db, limit=1)) == 1


def test_a_duplicate_run_id_raises_rather_than_writing_twice(agent_db: DatabaseManager) -> None:
    report = AgentReport("observer", "11111111-1111-1111-1111-111111111111", NOW, NOW)
    record_agent_run(agent_db, report)

    with pytest.raises(psycopg2.IntegrityError):
        record_agent_run(agent_db, report)


# --- the observer ----------------------------------------------------------


def test_the_observer_finds_a_stale_ledger_and_a_gap_on_the_liquid_token(
    agent_db: DatabaseManager, registry: TokenRegistry
) -> None:
    # Eight days of hours for both tokens; WBTC is liquid and has an 8-hour
    # hole, SHIB is thin and has the same hole. Only WBTC's is a finding.
    seed_hours(agent_db, WBTC, "WBTC", 192, volume_usd=5_000_000, skip=range(40, 48))
    seed_hours(agent_db, SHIB, "SHIB", 192, volume_usd=500, skip=range(40, 48))
    seed_run(agent_db, NOW - timedelta(minutes=40))  # stale: 300s * 3 = 15m, critical at 30m

    report = run_observer(
        make_settings(), db=agent_db, registry=registry, now=NOW, sinks=[LedgerSink(agent_db)]
    )

    by_kind = {(f.kind, f.subject): f for f in report.findings}
    assert by_kind[("stale", "ingest")].severity == Severity.CRITICAL
    assert ("gap", "WBTC") in by_kind and ("gap", "SHIB") not in by_kind
    assert by_kind[("gap", "WBTC")].evidence["gap_hours"] == 8
    assert report.status == Severity.CRITICAL

    (row,) = recent_agent_runs(agent_db, agent="observer")
    assert row["status"] == "critical"
    assert json.dumps(row["findings"])  # already decoded JSONB


def test_a_fresh_complete_database_is_ok(agent_db: DatabaseManager, registry: TokenRegistry) -> None:
    seed_hours(agent_db, WBTC, "WBTC", 192, volume_usd=5_000_000)
    seed_hours(agent_db, SHIB, "SHIB", 192, volume_usd=500)
    for minutes in range(5, 60, 5):
        seed_run(agent_db, NOW - timedelta(minutes=minutes))

    report = run_observer(make_settings(), db=agent_db, registry=registry, now=NOW, sinks=[])

    assert report.status == Severity.OK, [f.message for f in report.findings]
    assert {f.kind for f in report.findings} == {"fresh", "ledger", "coverage", "drift"}


def test_an_empty_database_is_informational_not_critical(
    agent_db: DatabaseManager, registry: TokenRegistry
) -> None:
    report = run_observer(make_settings(), db=agent_db, registry=registry, now=NOW, sinks=[])

    # Configured tokens with no rows are CRITICAL (that is a real problem on a
    # running stack); the empty ledger itself is only INFO.
    kinds = {f.kind: f.severity for f in report.findings}
    assert kinds["stale"] == Severity.INFO
    assert kinds["missing_token"] == Severity.CRITICAL
