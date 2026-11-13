"""The ledger rows are written by the harness: opened before the body, closed after.

Rollout step 2. The point of opening first: a run whose process dies midway
still leaves a row, visible in run_log as `running`.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from candlegraph.agents.base import AgentReport, Finding, Severity
from candlegraph.agents.reporting import LedgerSink, LogSink
from candlegraph.harness import JOBS, Outcome, run_job
from candlegraph.storage import set_db
from candlegraph.utils.logging_utils import RunIdFilter, current_run_id
from tests.conftest import FakeDatabase, make_settings

NOW = datetime(2026, 11, 13, 12, tzinfo=UTC)


@pytest.fixture
def db() -> FakeDatabase:
    database = FakeDatabase()
    set_db(database)
    return database


def _report(run_id: str, error: str | None = None) -> AgentReport:
    findings = (
        [] if error else [Finding(kind="stale", severity=Severity.WARNING, subject="WETH", message="m")]
    )
    return AgentReport(
        agent="observer", run_id=run_id, started_at=NOW, finished_at=NOW, findings=findings, error=error
    )


def _agent_writes(db: FakeDatabase) -> list[tuple[str, dict[str, Any]]]:
    return [(label, params or {}) for label, params in db.executed if "agent_run" in label]


def test_the_agent_row_is_opened_before_the_graph_and_closed_after(
    db: FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen_during_body: list[list[str]] = []

    def fake_observer(_settings: Any, **kwargs: Any) -> AgentReport:
        seen_during_body.append([label for label, _ in db.executed])
        return _report(kwargs["run_id"])

    monkeypatch.setattr("candlegraph.agents.observer.run_observer", fake_observer)
    run = run_job(
        JOBS["observer"],
        make_settings(),
        trigger="ui",
        params={"sinks": [LedgerSink(db)]},
        db=db,
        clock=lambda: NOW,
    )

    assert seen_during_body == [["open_agent_run"]]
    (open_label, opened), (close_label, closed) = _agent_writes(db)
    assert (open_label, close_label) == ("open_agent_run", "close_agent_run")
    assert opened["run_id"] == closed["run_id"] == run.run_id
    assert opened["trigger"] == closed["trigger"] == "ui"
    # status keeps its old meaning (the verdict); outcome is the new axis.
    assert closed["status"] == "warning" and closed["outcome"] == "succeeded"
    assert closed["findings_total"] == 1


def test_a_crashed_graph_closes_as_failed_with_status_error(
    db: FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """outcome=failed, and status stays 'error' as every existing reader expects."""
    monkeypatch.setattr(
        "candlegraph.agents.observer.run_observer", lambda _s, **k: _report(k["run_id"], error="Boom: x")
    )
    run_job(JOBS["observer"], make_settings(), params={"sinks": [LedgerSink(db)]}, db=db)

    (_, closed) = _agent_writes(db)[-1]
    assert closed["outcome"] == "failed" and closed["status"] == "error"
    assert json.loads(closed["detail"])["error"] == "Boom: x"


def test_a_body_that_raises_still_closes_the_row(db: FakeDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> AgentReport:
        raise RuntimeError("no graph")

    monkeypatch.setattr("candlegraph.agents.observer.run_observer", boom)
    run = run_job(JOBS["observer"], make_settings(), params={"sinks": [LedgerSink(db)]}, db=db)

    assert run.outcome is Outcome.FAILED
    (_, closed) = _agent_writes(db)[-1]
    assert closed["outcome"] == "failed" and closed["status"] == "error"
    assert "RuntimeError: no graph" in json.loads(closed["detail"])["error"]


def test_without_the_ledger_sink_no_row_is_written(db: FakeDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    """The janitor in CI runs with log,file and no database; it must not try to write."""
    monkeypatch.setattr("candlegraph.agents.janitor.run_janitor", lambda _s, **k: _report(k["run_id"]))
    run_job(JOBS["janitor"], make_settings(agent_report_sinks="log,file"), db=db)
    run_job(JOBS["janitor"], make_settings(), params={"sinks": [LogSink()]}, db=db)

    assert _agent_writes(db) == []


def test_the_sink_setting_decides_when_no_sinks_are_passed(
    db: FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scheduled run passes no sinks; AGENT_REPORT_SINKS decides."""
    monkeypatch.setattr("candlegraph.agents.observer.run_observer", lambda _s, **k: _report(k["run_id"]))
    run_job(JOBS["observer"], make_settings(agent_report_sinks="log,ledger"), db=db)

    assert [label for label, _ in _agent_writes(db)] == ["open_agent_run", "close_agent_run"]


class OpenFails(FakeDatabase):
    def execute(self, query: Any, params: dict[str, Any] | None = None) -> int:
        if self._label(query) == "open_agent_run":
            raise ConnectionError("blip")
        return super().execute(query, params)


def test_a_failed_open_is_a_warning_and_the_close_still_records_the_run(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    db = OpenFails()
    set_db(db)
    monkeypatch.setattr("candlegraph.agents.observer.run_observer", lambda _s, **k: _report(k["run_id"]))

    with caplog.at_level("WARNING", logger="candlegraph"):
        run = run_job(JOBS["observer"], make_settings(), params={"sinks": [LedgerSink(db)]}, db=db)

    assert run.outcome is Outcome.SUCCEEDED
    assert "Could not open agent_run row" in caplog.text
    assert [label for label, _ in _agent_writes(db)] == ["close_agent_run"]  # an upsert


def test_log_lines_inside_a_run_carry_its_run_id(db: FakeDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    tagged: list[str] = []
    run_filter = RunIdFilter()

    def fake_observer(_settings: Any, **kwargs: Any) -> AgentReport:
        record = logging.LogRecord("candlegraph", logging.INFO, __file__, 1, "inside", None, None)
        run_filter.filter(record)
        tagged.append(record.run)  # type: ignore[attr-defined]
        return _report(kwargs["run_id"])

    monkeypatch.setattr("candlegraph.agents.observer.run_observer", fake_observer)
    run = run_job(JOBS["observer"], make_settings(), params={"sinks": []}, db=db)

    assert tagged == [f" run={run.run_id[:8]}"]
    outside = logging.LogRecord("candlegraph", logging.INFO, __file__, 1, "outside", None, None)
    run_filter.filter(outside)
    assert outside.run == ""  # type: ignore[attr-defined]
    assert current_run_id.get() is None
