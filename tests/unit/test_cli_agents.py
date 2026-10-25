"""The agent commands: dispatch, exit codes, and what they forward."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from candlegraph.agents.base import AgentReport, Finding, Severity
from candlegraph.cli import main
from candlegraph.storage import set_db
from tests.conftest import FakeDatabase, make_settings

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


@pytest.fixture
def cli_env(tokens_file: Path, monkeypatch: pytest.MonkeyPatch) -> FakeDatabase:
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    monkeypatch.setattr("candlegraph.cli.get_settings", lambda: make_settings(tokens_file=str(tokens_file)))
    db = FakeDatabase()
    set_db(db)
    return db


def report(agent: str, severity: Severity = Severity.OK, error: str | None = None) -> AgentReport:
    rep = AgentReport(
        agent, "run-1", NOW, NOW, [Finding("k", severity, "s", "m")], summary="sum", error=error
    )
    rep.markdown = f"# {agent} report"
    return rep


# --- observe ---------------------------------------------------------------


def test_observe_prints_the_markdown_and_exits_zero(
    cli_env: FakeDatabase, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, Any] = {}

    def fake(settings: Any, **kwargs: Any) -> AgentReport:
        captured.update(kwargs)
        return report("observer", Severity.WARNING)

    monkeypatch.setattr("candlegraph.agents.observer.run_observer", fake)

    assert main(["observe"]) == 0
    assert "# observer report" in capsys.readouterr().out
    assert captured["db"] is cli_env
    assert [s.name for s in captured["sinks"]] == ["log", "ledger"]


def test_observe_strict_exits_one_at_warning_and_two_on_error(
    cli_env: FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "candlegraph.agents.observer.run_observer", lambda s, **k: report("observer", Severity.WARNING)
    )
    assert main(["observe", "--strict"]) == 1

    monkeypatch.setattr(
        "candlegraph.agents.observer.run_observer", lambda s, **k: report("observer", Severity.INFO)
    )
    assert main(["observe", "--strict"]) == 0

    monkeypatch.setattr(
        "candlegraph.agents.observer.run_observer", lambda s, **k: report("observer", error="boom")
    )
    assert main(["observe"]) == 2


def test_observe_json_and_sinks_flags(
    cli_env: FakeDatabase, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        "candlegraph.agents.observer.run_observer", lambda s, **k: captured.update(k) or report("observer")
    )

    assert main(["observe", "--json", "--sinks", "log"]) == 0
    assert '"status": "ok"' in capsys.readouterr().out
    assert [s.name for s in captured["sinks"]] == ["log"]


# --- janitor ---------------------------------------------------------------


def test_janitor_forwards_checks_and_open_pr(cli_env: FakeDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        "candlegraph.agents.janitor.run_janitor", lambda s, **k: captured.update(k) or report("janitor")
    )

    assert main(["janitor", "--open-pr", "--checks", "lint,typecheck"]) == 0
    assert captured["open_pr"] is True
    assert captured["checks"] == ("lint", "typecheck")
    assert [s.name for s in captured["sinks"]] == ["log", "ledger"]


def test_janitor_defaults_leave_open_pr_to_settings(
    cli_env: FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        "candlegraph.agents.janitor.run_janitor", lambda s, **k: captured.update(k) or report("janitor")
    )

    main(["janitor"])

    assert captured["open_pr"] is None and captured["checks"] is None


def test_janitor_strict_exit_code(cli_env: FakeDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "candlegraph.agents.janitor.run_janitor", lambda s, **k: report("janitor", Severity.CRITICAL)
    )

    assert main(["janitor", "--strict", "--sinks", "log"]) == 1


# --- agents ----------------------------------------------------------------


def test_agents_runs_the_schedule_loop(cli_env: FakeDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[Any] = []
    monkeypatch.setattr("candlegraph.agents.scheduler.run_agents", lambda s: called.append(s))

    assert main(["agents"]) == 0
    assert len(called) == 1


# --- agent-runs ------------------------------------------------------------


def test_agent_runs_prints_a_table_or_says_there_are_none(
    cli_env: FakeDatabase, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["agent-runs"]) == 0
    assert "no agent runs recorded yet" in capsys.readouterr().out

    cli_env.reads["get_recent_agent_runs"] = [
        {
            "finished_at": NOW,
            "agent": "observer",
            "status": "warning",
            "findings_total": 3,
            "duration_seconds": 1.5,
            "summary": "observer finished WARNING",
            "report": "# full",
        }
    ]
    assert main(["agent-runs", "--agent", "observer", "--limit", "5"]) == 0
    out = capsys.readouterr().out
    assert "observer  warning" in out and "observer finished WARNING" in out
    assert cli_env.fetches[-1][1] == {"limit": 5, "agent": "observer"}


def test_agent_runs_show_prints_the_newest_report(
    cli_env: FakeDatabase, capsys: pytest.CaptureFixture[str]
) -> None:
    cli_env.reads["get_recent_agent_runs"] = [
        {
            "report": "# the full report",
            "finished_at": NOW,
            "agent": "janitor",
            "status": "ok",
            "findings_total": 0,
            "duration_seconds": 1,
            "summary": "",
        }
    ]

    assert main(["agent-runs", "--show"]) == 0
    assert capsys.readouterr().out.strip() == "# the full report"
