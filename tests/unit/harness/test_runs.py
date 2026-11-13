"""``run_job``: open, run, close, fan out — and never raise."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from candlegraph.agents.base import AgentReport, Finding, Severity
from candlegraph.harness import JOBS, Outcome, RunOutcome, run_job
from candlegraph.harness.jobs import JobContext
from candlegraph.harness.runs import Run
from candlegraph.harness.stages import StageRecord
from candlegraph.ingestion.runner import IngestResult
from candlegraph.storage import set_db
from tests.conftest import FakeDatabase, make_settings


@pytest.fixture
def db() -> FakeDatabase:
    database = FakeDatabase()
    set_db(database)
    return database


class Recorder:
    def __init__(self) -> None:
        self.events: list[str] = []

    def started(self, run: Run) -> None:
        self.events.append(f"started:{run.outcome}")

    def stage(self, run: Run, record: StageRecord) -> None:
        self.events.append(f"stage:{record.name}")

    def finished(self, run: Run, outcome: RunOutcome) -> None:
        self.events.append(f"finished:{outcome.outcome}")


class Exploding:
    def started(self, run: Run) -> None:
        raise RuntimeError("hook down")

    stage = started  # type: ignore[assignment]
    finished = started  # type: ignore[assignment]


def _job(body: Any) -> Any:
    return replace(JOBS["observer"], run=body)


def test_the_lifecycle_runs_in_order_and_the_run_closes(db: FakeDatabase) -> None:
    recorder = Recorder()
    record = StageRecord(name="assess", status="ok", started_at=datetime.now(tz=UTC), duration_seconds=0.1)

    def body(ctx: JobContext) -> RunOutcome:
        recorder.events.append("body")
        assert ctx.on_stage is not None
        ctx.on_stage(record)
        return RunOutcome(outcome=Outcome.SUCCEEDED, payload="x")

    seen: list[StageRecord] = []
    run = run_job(_job(body), make_settings(), hooks=[recorder], on_stage=seen.append, parent_run_id="p")

    assert recorder.events == ["started:running", "body", "stage:assess", "finished:succeeded"]
    assert seen == [record]
    assert run.payload == "x" and run.parent_run_id == "p" and run.finished_at is not None


def test_a_body_that_raises_is_a_failed_run_not_an_exception(
    db: FakeDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    """An exception escaping a tick stops APScheduler rescheduling the job."""

    def body(_ctx: JobContext) -> RunOutcome:
        raise RuntimeError("subgraph down")

    with caplog.at_level("ERROR", logger="candlegraph"):
        run = run_job(_job(body), make_settings(), hooks=[])

    assert run.outcome is Outcome.FAILED
    assert run.result is not None and run.result.errors["__run__"] == "RuntimeError: subgraph down"
    assert "Observer run failed" in caplog.text


def test_a_failing_hook_is_logged_and_the_others_still_fire(
    db: FakeDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    recorder = Recorder()
    with caplog.at_level("WARNING", logger="candlegraph"):
        run = run_job(
            _job(lambda _c: RunOutcome(outcome=Outcome.SUCCEEDED)),
            make_settings(),
            hooks=[Exploding(), recorder],
        )

    assert run.outcome is Outcome.SUCCEEDED
    assert recorder.events == ["started:running", "finished:succeeded"]
    assert "hook down" in caplog.text


# --- the three bodies ----------------------------------------------------------


class _Client:
    def __init__(self, _settings: Any) -> None:
        pass

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_exc: object) -> None:
        pass


def test_the_ingest_job_opens_and_closes_its_ledger_row_around_the_pass(
    tokens_file: Path, db: FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ingest_run row is the harness's, not the body's: open first, close last."""
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    captured: dict[str, Any] = {}

    def fake_once(
        database: Any, _client: Any, _registry: Any, _settings: Any, _now: Any, lookback: Any, **kw: Any
    ) -> IngestResult:
        captured.update(lookback=lookback, executed_before=[label for label, _ in database.executed])
        kw["on_stage"](StageRecord("token_hours", "ok", None, 0.1, {"rows": 3}))
        return IngestResult(hours_written=3, errors={"WBTC": "boom"})

    monkeypatch.setattr("candlegraph.ingestion.runner.SubgraphClient", _Client)
    monkeypatch.setattr("candlegraph.ingestion.runner.ingest_once", fake_once)
    run = run_job(
        JOBS["ingest"],
        make_settings(tokens_file=str(tokens_file)),
        trigger="backfill",
        params={"lookback_days": 9},
        parent_run_id="11111111-1111-1111-1111-111111111111",
    )

    assert captured["lookback"] == 9
    assert captured["executed_before"] == ["start_ingest_run"]
    (start_label, start), (finish_label, finish) = db.executed
    assert (start_label, finish_label) == ("start_ingest_run", "finish_ingest_run")
    assert start is not None and finish is not None
    assert start["run_id"] == finish["run_id"] == run.run_id
    assert start["trigger"] == "backfill" and start["lookback_days"] == 9
    assert start["parent_run_id"] == "11111111-1111-1111-1111-111111111111"
    assert finish["status"] == "partial"
    assert [s["name"] for s in json.loads(finish["detail"])["stages"]] == ["token_hours"]
    assert run.outcome is Outcome.PARTIAL
    assert isinstance(run.payload, IngestResult) and run.payload.run_id == run.run_id


def test_the_ingest_job_survives_a_broken_tokens_file(
    tmp_path: Path, db: FakeDatabase, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text("tokens: []\n", encoding="utf-8")
    monkeypatch.setenv("TOKENS_FILE", str(broken))

    with caplog.at_level("ERROR", logger="candlegraph"):
        run = run_job(JOBS["ingest"], make_settings(tokens_file=str(broken)))

    assert run.outcome is Outcome.FAILED
    assert "Ingest run failed" in caplog.text


def _report(error: str | None = None, severity: Severity = Severity.WARNING) -> AgentReport:
    now = datetime.now(tz=UTC)
    findings = [] if error else [Finding(kind="c", severity=severity, subject="s", message="m")]
    return AgentReport(
        agent="observer", run_id="r", started_at=now, finished_at=now, findings=findings, error=error
    )


def test_an_agent_report_maps_to_outcome_and_verdict(
    db: FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("candlegraph.agents.observer.run_observer", lambda *_a, **_k: _report())
    run = run_job(JOBS["observer"], make_settings())
    assert run.outcome is Outcome.SUCCEEDED
    assert run.result is not None and run.result.verdict == "warning"


def test_a_crashed_graph_is_a_failed_run_with_no_verdict(
    db: FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not verdict=error: that is the conflation the harness exists to remove."""
    monkeypatch.setattr("candlegraph.agents.observer.run_observer", lambda *_a, **_k: _report(error="boom"))
    run = run_job(JOBS["observer"], make_settings())
    assert run.outcome is Outcome.FAILED
    assert run.result is not None and run.result.verdict is None
    assert isinstance(run.payload, AgentReport)


def test_the_janitor_job_passes_its_params_through(db: FakeDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        "candlegraph.agents.janitor.run_janitor",
        lambda _s, **k: captured.update(k) or _report(severity=Severity.OK),
    )
    run_job(JOBS["janitor"], make_settings(), params={"open_pr": True, "checks": ("lint",), "sinks": []})
    assert captured["open_pr"] is True and captured["checks"] == ("lint",) and captured["sinks"] == []
