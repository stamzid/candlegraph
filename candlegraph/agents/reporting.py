"""Rendering a report and posting it.

A report is posted through *sinks*. Each sink is one destination; the set in
use comes from ``AGENT_REPORT_SINKS``. A sink that fails is logged and skipped,
never raised: the run already happened, and losing one copy of the report is
better than losing the ledger row because a webhook was down.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from candlegraph.agents.base import AgentReport, Finding, Severity, sort_findings
from candlegraph.agents.ledger import record_agent_run
from candlegraph.agents.shell import Shell, SubprocessShell
from candlegraph.settings import Settings
from candlegraph.storage import DatabaseManager
from candlegraph.utils.logging_utils import service_logger

# --- rendering -------------------------------------------------------------


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def findings_table(findings: Sequence[Finding]) -> str:
    """A Markdown table of findings, worst first."""
    if not findings:
        return "_No findings._"
    lines = ["| severity | kind | subject | message |", "|---|---|---|---|"]
    for f in sort_findings(findings):
        lines.append(f"| {f.severity.label} | {_cell(f.kind)} | {_cell(f.subject)} | {_cell(f.message)} |")
    return "\n".join(lines)


def render_markdown(report: AgentReport, sections: Sequence[tuple[str, str]] = ()) -> str:
    """The whole report as Markdown. ``sections`` are (title, body) pairs appended after the findings."""
    title = report.agent.replace("_", " ")
    lines = [
        f"# {title} — {report.finished_at:%Y-%m-%d %H:%M} UTC — {report.status_label.upper()}",
        "",
        report.summary or "_No summary._",
        "",
    ]
    if report.error:
        lines += ["## Error", "", "```", report.error, "```", ""]
    lines += ["## Findings", "", findings_table(report.findings), ""]
    for heading, body in sections:
        if body.strip():
            lines += [f"## {heading}", "", body.rstrip(), ""]
    lines += [
        "---",
        f"run `{report.run_id}` · started {report.started_at:%Y-%m-%d %H:%M:%S} UTC · "
        f"{report.duration_seconds:.1f}s · {len(report.findings)} findings",
    ]
    return "\n".join(lines)


def finish_report(
    agent: str,
    run_id: str,
    started_at: datetime,
    findings: Sequence[Finding],
    narrate: Any,
    sections: Sequence[tuple[str, str]] = (),
    detail: dict[str, Any] | None = None,
    error: str | None = None,
    now: datetime | None = None,
) -> AgentReport:
    """Assemble, narrate, and render a report in one step."""
    from candlegraph.agents.base import now_utc

    report = AgentReport(
        agent=agent,
        run_id=run_id,
        started_at=started_at,
        finished_at=now or now_utc(),
        findings=sort_findings(findings),
        detail=dict(detail or {}),
        error=error,
    )
    report.summary = narrate(agent, report.findings)
    report.markdown = render_markdown(report, sections)
    return report


# --- sinks -----------------------------------------------------------------


class Sink(Protocol):
    name: str

    def post(self, report: AgentReport) -> None: ...


class LogSink:
    """The service log, at a level that follows the status."""

    name = "log"

    def post(self, report: AgentReport) -> None:
        level = {
            "ok": service_logger.info,
            "info": service_logger.info,
            "warning": service_logger.warning,
            "critical": service_logger.error,
            "error": service_logger.error,
        }[report.status_label]
        level("[%s] %s %s", report.agent, report.status_label.upper(), report.summary)
        for finding in report.findings:
            if finding.severity >= Severity.WARNING:
                level("[%s]   %s %s: %s", report.agent, finding.kind, finding.subject, finding.message)


class LedgerSink:
    """The ``agent_run`` table."""

    name = "ledger"

    def __init__(self, db: DatabaseManager) -> None:
        self.db = db

    def post(self, report: AgentReport) -> None:
        record_agent_run(self.db, report)


class FileSink:
    """One Markdown file per report, for CI artefacts."""

    name = "file"

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def path_for(self, report: AgentReport) -> Path:
        return self.directory / f"{report.agent}-{report.finished_at:%Y%m%d-%H%M%S}.md"

    def post(self, report: AgentReport) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path_for(report).write_text(report.markdown, encoding="utf-8")


class WebhookSink:
    """POST a Slack-compatible JSON body."""

    name = "webhook"

    def __init__(self, url: str, transport: httpx.BaseTransport | None = None, timeout: float = 10.0) -> None:
        self.url = url
        self._transport = transport
        self.timeout = timeout

    def payload(self, report: AgentReport) -> dict[str, Any]:
        return {
            "text": f"[{report.agent}] {report.status_label.upper()}: {report.summary}",
            "agent": report.agent,
            "status": report.status_label,
            "run_id": report.run_id,
            "findings": [f.to_dict() for f in report.findings],
        }

    def post(self, report: AgentReport) -> None:
        with httpx.Client(transport=self._transport, timeout=self.timeout) as client:
            response = client.post(self.url, json=self.payload(report))
            response.raise_for_status()


class GitHubIssueSink:
    """Open a GitHub issue through ``gh``, but only when there is something to act on."""

    name = "github"

    def __init__(
        self,
        shell: Shell,
        repo: str | None = None,
        cwd: Path | None = None,
        min_severity: Severity = Severity.WARNING,
        label: str = "agent-report",
    ) -> None:
        self.shell = shell
        self.repo = repo
        self.cwd = cwd
        self.min_severity = min_severity
        self.label = label

    def post(self, report: AgentReport) -> None:
        if not report.at_least(self.min_severity):
            return
        title = f"{report.agent}: {report.status_label.upper()} on {report.finished_at:%Y-%m-%d %H:%M} UTC"
        args = ["gh", "issue", "create", "--title", title, "--body", report.markdown, "--label", self.label]
        if self.repo:
            args += ["--repo", self.repo]
        result = self.shell.run(args, cwd=self.cwd, timeout=60)
        if not result.ok:
            raise RuntimeError(f"gh issue create failed ({result.returncode}): {result.tail(5)}")
        service_logger.info("Opened issue for %s report: %s", report.agent, result.stdout.strip())


def build_sinks(
    settings: Settings,
    db: DatabaseManager | None = None,
    shell: Shell | None = None,
    transport: httpx.BaseTransport | None = None,
    names: Sequence[str] | None = None,
) -> list[Sink]:
    """Instantiate the configured sinks. Unknown names are an error, not a silent skip."""
    chosen = tuple(names) if names is not None else settings.report_sinks
    sinks: list[Sink] = []
    for name in chosen:
        match name:
            case "log":
                sinks.append(LogSink())
            case "ledger":
                if db is None:
                    raise ValueError("the ledger sink needs a database")
                sinks.append(LedgerSink(db))
            case "file":
                sinks.append(FileSink(settings.report_dir))
            case "webhook":
                if not settings.agent_webhook_url:
                    raise ValueError("the webhook sink needs AGENT_WEBHOOK_URL")
                sinks.append(WebhookSink(settings.agent_webhook_url, transport=transport))
            case "github":
                sinks.append(
                    GitHubIssueSink(
                        shell or SubprocessShell(), repo=settings.agent_github_repo, cwd=settings.janitor_root
                    )
                )
            case _:
                raise ValueError(f"unknown report sink {name!r}; expected log, ledger, file, webhook, github")
    return sinks


def publish(report: AgentReport, sinks: Sequence[Sink]) -> list[str]:
    """Post to every sink. Returns the names of the sinks that failed."""
    failed: list[str] = []
    for sink in sinks:
        try:
            sink.post(report)
        except Exception as exc:
            failed.append(sink.name)
            service_logger.warning(
                "Report sink %r failed for %s run %s: %s", sink.name, report.agent, report.run_id, exc
            )
    return failed


def report_json(report: AgentReport) -> str:
    return json.dumps(report.to_dict(), indent=2, default=str)
