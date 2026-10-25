"""The vocabulary every agent speaks: severities, findings, and reports.

An agent's output is a list of :class:`Finding` objects and nothing else. The
narration, the Markdown, the exit code, and the ledger row are all derived
from the findings, so two agents that found the same things produce the same
report whatever they did to find them.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import IntEnum
from typing import Any


class Severity(IntEnum):
    """How much a finding needs a human. Ordered so ``max`` picks the worst."""

    OK = 0
    INFO = 1
    WARNING = 2
    CRITICAL = 3

    @property
    def label(self) -> str:
        return self.name.lower()


@dataclass(frozen=True)
class Finding:
    """One thing an agent noticed.

    Attributes:
        kind: A short machine-readable tag (``stale``, ``gap``, ``drift``,
            ``check_failed``). Reports group on it.
        severity: How urgent it is.
        subject: What it is about: a symbol, a run id, a make target.
        message: One sentence a human can act on.
        evidence: The numbers behind the message. JSON-serialisable.
    """

    kind: str
    severity: Severity
    subject: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["severity"] = self.severity.label
        return data


def overall_severity(findings: Iterable[Finding]) -> Severity:
    """The worst severity present, or ``OK`` for no findings at all."""
    return max((finding.severity for finding in findings), default=Severity.OK)


def new_run_id() -> str:
    return str(uuid.uuid4())


@dataclass
class AgentReport:
    """What one graph run produced, ready to post.

    ``error`` is set when the graph itself failed rather than finding
    something; the ledger records that as status ``error`` so a crashed
    observer is never mistaken for a quiet one.
    """

    agent: str
    run_id: str
    started_at: datetime
    finished_at: datetime
    findings: list[Finding] = field(default_factory=list)
    summary: str = ""
    markdown: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def status(self) -> Severity:
        return overall_severity(self.findings)

    @property
    def status_label(self) -> str:
        """``ok``, ``info``, ``warning``, ``critical``, or ``error``."""
        return "error" if self.error else self.status.label

    @property
    def duration_seconds(self) -> float:
        return max(0.0, (self.finished_at - self.started_at).total_seconds())

    def by_severity(self, severity: Severity) -> list[Finding]:
        return [finding for finding in self.findings if finding.severity == severity]

    def at_least(self, severity: Severity) -> bool:
        """True when the report needs attention at ``severity`` or worse."""
        return bool(self.error) or self.status >= severity

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "run_id": self.run_id,
            "status": self.status_label,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "duration_seconds": round(self.duration_seconds, 3),
            "summary": self.summary,
            "findings": [finding.to_dict() for finding in self.findings],
            "detail": self.detail,
            "error": self.error,
        }


def sort_findings(findings: Sequence[Finding]) -> list[Finding]:
    """Worst first, then by kind and subject so the order is stable."""
    return sorted(findings, key=lambda f: (-int(f.severity), f.kind, f.subject))


def now_utc() -> datetime:
    return datetime.now(tz=UTC)
