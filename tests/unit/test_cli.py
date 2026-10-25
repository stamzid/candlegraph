"""The CLI: dispatch, exit codes, and the arguments each command forwards."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from candlegraph.cli import main
from candlegraph.ingestion.runner import IngestResult
from candlegraph.storage import set_db
from tests.conftest import FakeDatabase, make_settings


@pytest.fixture
def cli_env(tokens_file: Path, monkeypatch: pytest.MonkeyPatch) -> FakeDatabase:
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    monkeypatch.setattr("candlegraph.cli.get_settings", lambda: make_settings(tokens_file=str(tokens_file)))
    db = FakeDatabase()
    set_db(db)
    return db


def capture_ingest(monkeypatch: pytest.MonkeyPatch, result: IngestResult) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_run(db: Any, registry: Any, settings: Any, **kwargs: Any) -> IngestResult:
        captured.update(kwargs, registry=registry)
        return result

    monkeypatch.setattr("candlegraph.ingestion.runner.run_ingest", fake_run)
    return captured


# --- argument parsing ------------------------------------------------------


def test_no_command_is_an_error() -> None:
    with pytest.raises(SystemExit) as exc:
        main([])

    assert exc.value.code != 0


def test_an_unknown_command_is_an_error() -> None:
    with pytest.raises(SystemExit):
        main(["bogus"])


# --- serve -----------------------------------------------------------------


def test_serve_starts_uvicorn(cli_env: FakeDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: captured.update(app=app, **kw))

    assert main(["serve"]) == 0
    assert captured["app"] == "candlegraph.api.app:app"


# --- worker ----------------------------------------------------------------


def test_worker_runs_the_worker_loop(cli_env: FakeDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[Any] = []
    monkeypatch.setattr("candlegraph.ingestion.worker.run_worker", lambda s: called.append(s))

    assert main(["worker"]) == 0
    assert len(called) == 1


# --- ingest and backfill ---------------------------------------------------


def test_ingest_runs_one_pass_with_the_configured_window(
    cli_env: FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = capture_ingest(monkeypatch, IngestResult(hours_written=5))

    assert main(["ingest"]) == 0
    assert captured["trigger"] == "manual"
    assert captured["lookback_days"] is None


def test_backfill_defaults_to_ninety_days(cli_env: FakeDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    """The phase-2 exit criterion is a 90-day backfill, so that is the default."""
    captured = capture_ingest(monkeypatch, IngestResult(hours_written=5))

    assert main(["backfill"]) == 0
    assert captured["lookback_days"] == 90
    assert captured["trigger"] == "backfill"


def test_backfill_accepts_an_explicit_window(cli_env: FakeDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = capture_ingest(monkeypatch, IngestResult())

    main(["backfill", "--days", "30"])

    assert captured["lookback_days"] == 30


def test_a_run_with_errors_exits_non_zero(cli_env: FakeDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    """CI needs a failed ingest to fail the step."""
    capture_ingest(monkeypatch, IngestResult(errors={"WBTC": "boom"}))

    assert main(["ingest"]) == 1


# --- report ----------------------------------------------------------------


def test_report_prints_the_table(cli_env: FakeDatabase, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["report"]) == 0
    assert "Data-quality report" in capsys.readouterr().out


def test_report_exits_zero_by_default_even_when_dirty(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without --strict the report is informational, not a gate."""
    monkeypatch.setattr("candlegraph.cli.get_settings", lambda: make_settings(tokens_file=str(tokens_file)))
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    set_db(FakeDatabase())

    assert main(["report"]) == 0
    assert "MISSING" in capsys.readouterr().out


def test_strict_report_fails_when_a_token_is_missing(
    cli_env: FakeDatabase, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["report", "--strict"]) == 1
    assert "MISSING" in capsys.readouterr().out


def test_strict_report_passes_on_a_clean_database(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    monkeypatch.setattr("candlegraph.cli.get_settings", lambda: make_settings(tokens_file=str(tokens_file)))
    set_db(
        FakeDatabase(
            coverage_report=[
                {
                    "token_id": addr,
                    "symbol": sym,
                    "stored_hours": 25,
                    "first_unix": 1_700_000_000,
                    "last_unix": 1_700_000_000 + 24 * 3600,
                    "expected_hours": 25,
                }
                for addr, sym in (("0xa", "WBTC"), ("0xb", "SHIB"))
            ]
        )
    )

    assert main(["report", "--strict"]) == 0
    assert "OK:" in capsys.readouterr().out


def test_report_window_is_passed_through(cli_env: FakeDatabase, capsys: pytest.CaptureFixture[str]) -> None:
    main(["report", "--days", "7"])

    labels = [label for label, _ in cli_env.fetches]
    assert "coverage_report" in labels
    params = next(p for label, p in cli_env.fetches if label == "coverage_report")
    assert params["start_unix"] is not None


# --- runs ------------------------------------------------------------------


def test_runs_reports_an_empty_ledger(cli_env: FakeDatabase, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["runs"]) == 0
    assert "no ingest runs recorded yet" in capsys.readouterr().out


def test_runs_lists_the_ledger(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("candlegraph.cli.get_settings", lambda: make_settings(tokens_file=str(tokens_file)))
    set_db(
        FakeDatabase(
            get_recent_runs=[
                {
                    "started_at": datetime(2026, 10, 25, 6, 0, tzinfo=UTC),
                    "trigger": "scheduled",
                    "status": "succeeded",
                    "token_hours_written": 409,
                    "pool_hours_written": 120,
                    "duration_seconds": 3.5,
                }
            ]
        )
    )

    assert main(["runs"]) == 0
    out = capsys.readouterr().out
    assert "succeeded" in out
    assert "409" in out


def test_runs_tolerates_an_unfinished_run(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A run still in flight has no duration yet."""
    monkeypatch.setattr("candlegraph.cli.get_settings", lambda: make_settings(tokens_file=str(tokens_file)))
    set_db(
        FakeDatabase(
            get_recent_runs=[
                {
                    "started_at": datetime(2026, 10, 25, 6, 0, tzinfo=UTC),
                    "trigger": "manual",
                    "status": "running",
                    "token_hours_written": 0,
                    "pool_hours_written": 0,
                    "duration_seconds": None,
                }
            ]
        )
    )

    assert main(["runs"]) == 0
    assert "running" in capsys.readouterr().out
