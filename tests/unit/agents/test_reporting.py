"""Rendering and the sinks. A sink failing never fails the run."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from candlegraph.agents.base import AgentReport, Severity
from candlegraph.agents.reporting import (
    FileSink,
    GitHubIssueSink,
    LedgerSink,
    LogSink,
    WebhookSink,
    build_sinks,
    finish_report,
    publish,
    render_markdown,
)
from tests.conftest import FakeDatabase, make_settings
from tests.unit.agents.conftest import FakeShell, fail, finding, ok

STARTED = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def report(*severities: Severity, error: str | None = None) -> AgentReport:
    return finish_report(
        "observer",
        "run-1",
        STARTED,
        [finding(s, subject=f"s{i}") for i, s in enumerate(severities)],
        lambda agent, findings: f"{agent} summary",
        sections=[("Coverage", "| a |\n|---|"), ("Empty", "   ")],
        error=error,
        now=STARTED,
    )


# --- rendering -------------------------------------------------------------


def test_markdown_has_title_summary_findings_and_only_non_empty_sections() -> None:
    text = report(Severity.WARNING, Severity.OK).markdown

    assert text.startswith("# observer — 2026-10-25 12:00 UTC — WARNING")
    assert "observer summary" in text
    assert "| warning | check | s0 | m |" in text
    assert "## Coverage" in text and "## Empty" not in text
    assert "run `run-1`" in text


def test_markdown_lists_findings_worst_first_and_escapes_pipes() -> None:
    rep = report(Severity.OK, Severity.CRITICAL)
    rep.findings[1] = finding(Severity.OK, message="a | b")
    text = render_markdown(rep)

    assert text.index("| critical |") < text.index("| ok |")
    assert "a \\| b" in text


def test_an_errored_report_shows_the_error_and_no_findings() -> None:
    text = report(error="RuntimeError: db").markdown

    assert "— ERROR" in text
    assert "## Error" in text and "RuntimeError: db" in text
    assert "_No findings._" in text


# --- sinks -----------------------------------------------------------------


def test_the_log_sink_logs_at_a_level_matching_the_status(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO", logger="candlegraph"):
        LogSink().post(report(Severity.OK))
        LogSink().post(report(Severity.CRITICAL))

    levels = [record.levelname for record in caplog.records]
    assert levels[0] == "INFO" and "ERROR" in levels


def test_the_ledger_sink_writes_one_agent_run_row(fake_db: FakeDatabase) -> None:
    LedgerSink(fake_db).post(report(Severity.WARNING))

    label, params = fake_db.executed[0]
    assert label == "insert_agent_run"
    assert params is not None
    assert params["agent"] == "observer" and params["status"] == "warning" and params["findings_total"] == 1
    assert json.loads(params["findings"])[0]["severity"] == "warning"
    assert json.loads(params["detail"])["error"] is None


def test_the_file_sink_writes_markdown_named_by_agent_and_time(tmp_path: Path) -> None:
    sink = FileSink(tmp_path / "reports")
    rep = report(Severity.OK)

    sink.post(rep)

    path = tmp_path / "reports" / "observer-20261025-120000.md"
    assert path.read_text(encoding="utf-8") == rep.markdown


def test_the_webhook_sink_posts_a_slack_compatible_body() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    WebhookSink("https://hooks.example/abc", transport=httpx.MockTransport(handler)).post(
        report(Severity.WARNING)
    )

    body = json.loads(seen[0].content)
    assert body["text"].startswith("[observer] WARNING: observer summary")
    assert body["status"] == "warning" and body["findings"][0]["subject"] == "s0"


def test_the_webhook_sink_raises_on_a_rejected_post() -> None:
    sink = WebhookSink(
        "https://hooks.example/abc", transport=httpx.MockTransport(lambda _r: httpx.Response(500))
    )

    with pytest.raises(httpx.HTTPStatusError):
        sink.post(report(Severity.OK))


def test_the_github_sink_only_opens_an_issue_at_warning_or_worse() -> None:
    shell = FakeShell({"gh issue create": ok("https://github.com/o/r/issues/7\n")})
    sink = GitHubIssueSink(shell, repo="o/r")

    sink.post(report(Severity.INFO))
    assert shell.calls == []

    sink.post(report(Severity.WARNING))
    ((command, _, _),) = shell.calls
    assert command.startswith("gh issue create --title observer: WARNING on 2026-10-25 12:00 UTC")
    assert "--repo o/r" in command and "--label agent-report" in command


def test_the_github_sink_raises_when_gh_fails() -> None:
    sink = GitHubIssueSink(FakeShell({"gh issue create": fail(1, "not logged in")}))

    with pytest.raises(RuntimeError, match="gh issue create failed"):
        sink.post(report(Severity.CRITICAL))


def test_publish_never_raises_and_names_the_sinks_that_failed(caplog: pytest.LogCaptureFixture) -> None:
    class Broken:
        name = "broken"

        def post(self, _report: AgentReport) -> None:
            raise RuntimeError("down")

    with caplog.at_level("WARNING", logger="candlegraph"):
        failed = publish(report(Severity.OK), [Broken(), LogSink()])

    assert failed == ["broken"]
    assert "Report sink 'broken' failed" in caplog.text


# --- configuration ---------------------------------------------------------


def test_build_sinks_follows_the_setting_and_rejects_unknown_names(
    fake_db: FakeDatabase, tmp_path: Path
) -> None:
    settings = make_settings(
        agent_report_sinks="log, ledger,file,webhook,github",
        agent_webhook_url="https://hooks.example/x",
        agent_report_dir=str(tmp_path),
    )

    names = [sink.name for sink in build_sinks(settings, db=fake_db, shell=FakeShell())]
    assert names == ["log", "ledger", "file", "webhook", "github"]

    with pytest.raises(ValueError, match="unknown report sink"):
        build_sinks(make_settings(agent_report_sinks="log,pager"), db=fake_db)


def test_build_sinks_refuses_a_ledger_without_a_database_and_a_webhook_without_a_url() -> None:
    with pytest.raises(ValueError, match="needs a database"):
        build_sinks(make_settings(agent_report_sinks="ledger"))
    with pytest.raises(ValueError, match="AGENT_WEBHOOK_URL"):
        build_sinks(make_settings(agent_report_sinks="webhook"))


def test_an_explicit_list_overrides_the_setting(fake_db: FakeDatabase) -> None:
    sinks = build_sinks(make_settings(agent_report_sinks="log,ledger"), db=fake_db, names=["log"])

    assert [s.name for s in sinks] == ["log"]
