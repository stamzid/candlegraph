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
from candlegraph.harness import JOBS, run_job
from candlegraph.ingestion import ledger
from candlegraph.knowledge.documents import RawDocument, ensure_source, store_document
from candlegraph.knowledge.statements import knowledge_statements_for
from candlegraph.storage import DatabaseManager
from candlegraph.tokens import TokenRegistry
from tests.conftest import SHIB, WBTC, make_settings

pytestmark = pytest.mark.integration

HOUR = 3600
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


@pytest.fixture
def agent_db(db: DatabaseManager) -> DatabaseManager:
    db.execute(
        f'TRUNCATE "{db.schema}".agent_run, "{db.schema}".ingest_run, '
        f'"{db.schema}".source, "{db.schema}".explanation CASCADE'
    )
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

    # Through the harness, as production runs it: the harness writes the row.
    run = run_job(
        JOBS["observer"],
        make_settings(),
        trigger="manual",
        params={"sinks": [LedgerSink(agent_db)], "registry": registry, "now": NOW},
        db=agent_db,
    )
    report = run.payload

    by_kind = {(f.kind, f.subject): f for f in report.findings}
    assert by_kind[("stale", "ingest")].severity == Severity.CRITICAL
    assert ("gap", "WBTC") in by_kind and ("gap", "SHIB") not in by_kind
    assert by_kind[("gap", "WBTC")].evidence["gap_hours"] == 8
    assert report.status == Severity.CRITICAL

    (row,) = recent_agent_runs(agent_db, agent="observer")
    assert row["status"] == "critical"
    assert row["outcome"] == "succeeded" and row["trigger"] == "manual"
    assert str(row["run_id"]) == run.run_id == report.run_id
    assert json.dumps(row["findings"])  # already decoded JSONB


def test_a_fresh_complete_database_is_ok(agent_db: DatabaseManager, registry: TokenRegistry) -> None:
    seed_hours(agent_db, WBTC, "WBTC", 192, volume_usd=5_000_000)
    seed_hours(agent_db, SHIB, "SHIB", 192, volume_usd=500)
    for minutes in range(5, 60, 5):
        seed_run(agent_db, NOW - timedelta(minutes=minutes))

    report = run_observer(make_settings(), db=agent_db, registry=registry, now=NOW, sinks=[])

    assert report.status == Severity.OK, [f.message for f in report.findings]
    assert {f.kind for f in report.findings} == {
        "fresh",
        "ledger",
        "coverage",
        "drift",
        "source_budget",
        "source_errors",
        "embedding_backlog",
        "uncited_explanations",
    }
    # The step-9 exit criterion: a stack with no knowledge rows reports the
    # new findings as the calm OK they are, against the real empty tables.
    knowledge = {f.kind: f for f in report.findings if f.kind.startswith(("source_", "embedding", "uncited"))}
    assert all(f.severity == Severity.OK for f in knowledge.values()), knowledge
    assert knowledge["source_budget"].evidence["month_spent"] == 0


def test_the_observer_reads_the_knowledge_ledger_budget_backlog_and_citations(
    agent_db: DatabaseManager, registry: TokenRegistry
) -> None:
    """The four knowledge findings against real rows: spend summed, streaks seen,
    the backlog counted per model, and an all-uncited window flagged."""
    seed_hours(agent_db, WBTC, "WBTC", 192, volume_usd=5_000_000)
    seed_hours(agent_db, SHIB, "SHIB", 192, volume_usd=500)
    for minutes in range(5, 60, 5):
        seed_run(agent_db, NOW - timedelta(minutes=minutes))
    statements = knowledge_statements_for(agent_db.schema)
    ensure_source(agent_db, "tavily", "search")
    # 850 credits this month and a three-call failure streak.
    agent_db.execute(
        f'INSERT INTO "{agent_db.schema}".source_call (source_id, called_at, credits, status) '
        "VALUES ('tavily', %(early)s, 850, 'ok'), ('tavily', %(a)s, 1, 'error'), "
        "('tavily', %(b)s, 1, 'rate_limited'), ('tavily', %(c)s, 1, 'error')",
        {
            "early": NOW - timedelta(days=2),
            "a": NOW - timedelta(hours=3),
            "b": NOW - timedelta(hours=2),
            "c": NOW - timedelta(hours=1),
        },
    )
    # One stored document whose chunk has no vector, and one uncited explanation.
    store_document(
        agent_db,
        RawDocument(source_id="tavily", url="https://x.com/a", raw_text="One sentence."),
        retrieved_at=NOW,
    )
    agent_db.execute(
        statements.upsert_explanation,
        {
            "strategy": "bollinger",
            "symbol": "WBTC",
            "period_start_unix": int(NOW.timestamp()) - 3600,
            "position": -1,
            "numeric_context": "{}",
            "citations": "[]",
            "rationale": "",
            "model": None,
            "generated_at": NOW - timedelta(hours=2),
        },
    )

    report = run_observer(make_settings(), db=agent_db, registry=registry, now=NOW, sinks=[])

    found = {f.kind: f for f in report.findings}
    assert found["source_budget"].severity == Severity.WARNING, "850 of 1000 is 85%"
    assert found["source_budget"].evidence["month_spent"] == 853
    assert found["source_errors"].severity == Severity.WARNING
    assert found["source_errors"].evidence["streak"] == ["error", "rate_limited", "error"]
    assert found["embedding_backlog"].severity == Severity.INFO
    assert found["embedding_backlog"].evidence["missing"] == 1
    assert found["uncited_explanations"].severity == Severity.WARNING, "every explanation uncited"
    assert "## Stages" in report.markdown and "collect_knowledge" in report.markdown


def test_an_empty_database_is_informational_not_critical(
    agent_db: DatabaseManager, registry: TokenRegistry
) -> None:
    report = run_observer(make_settings(), db=agent_db, registry=registry, now=NOW, sinks=[])

    # Configured tokens with no rows are CRITICAL (that is a real problem on a
    # running stack); the empty ledger itself is only INFO.
    kinds = {f.kind: f.severity for f in report.findings}
    assert kinds["stale"] == Severity.INFO
    assert kinds["missing_token"] == Severity.CRITICAL


# --- one shape of a run (harness rollout step 2) --------------------------------


def test_run_log_reads_both_ledgers_in_one_vocabulary(agent_db: DatabaseManager) -> None:
    from candlegraph.agents.ledger import close_agent_run, open_agent_run

    seed_run(agent_db, NOW - timedelta(minutes=10))
    good = "22222222-2222-2222-2222-222222222222"
    crashed = "33333333-3333-3333-3333-333333333333"
    for run_id, error in ((good, None), (crashed, "Boom: x")):
        open_agent_run(agent_db, run_id, "observer", "scheduled", NOW)
        report = AgentReport("observer", run_id, NOW, NOW, error=error)
        close_agent_run(
            agent_db,
            run_id=run_id,
            agent="observer",
            trigger="scheduled",
            outcome="failed" if error else "succeeded",
            started_at=NOW,
            finished_at=NOW + timedelta(seconds=2),
            report=report,
        )

    rows = agent_db.fetch_all(agent_db.sql.get_run_log, {"limit": 10, "job": None})
    by_id = {str(r["run_id"]): r for r in rows}
    assert {r["job"] for r in rows} == {"ingest", "observer"}
    assert by_id[good]["outcome"] == "succeeded" and by_id[good]["verdict"] == "ok"
    # A crashed graph: failed, and no verdict — not a verdict of "error".
    assert by_id[crashed]["outcome"] == "failed" and by_id[crashed]["verdict"] is None
    ingest = next(r for r in rows if r["job"] == "ingest")
    assert ingest["outcome"] == "succeeded" and ingest["verdict"] is None

    only = agent_db.fetch_all(agent_db.sql.get_run_log, {"limit": 10, "job": "ingest"})
    assert [r["job"] for r in only] == ["ingest"]


def test_a_run_opened_and_never_closed_is_visible_as_running(agent_db: DatabaseManager) -> None:
    """The detector two-phase rows buy: a process that died mid-run leaves a trace.

    It stays out of recent_agent_runs, whose readers expect a finished row.
    """
    from candlegraph.agents.ledger import open_agent_run

    open_agent_run(agent_db, "44444444-4444-4444-4444-444444444444", "observer", "ui", NOW)

    (row,) = agent_db.fetch_all(agent_db.sql.get_run_log, {"limit": 10, "job": "observer"})
    assert row["outcome"] == "running" and row["finished_at"] is None and row["trigger"] == "ui"
    assert recent_agent_runs(agent_db, agent="observer") == []
