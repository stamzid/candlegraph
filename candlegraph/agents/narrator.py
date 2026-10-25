"""Turning findings into a paragraph.

The default is a template: deterministic, offline, and good enough for a
report that is mostly a table anyway. When ``AGENT_LLM_MODEL`` and
``ANTHROPIC_API_KEY`` are set, the model writes the paragraph instead — from
the findings only. It never sees the database, cannot add or remove a
finding, and cannot change the status or the exit code. If it fails, the
template is used and the failure is logged. Prose is the only thing a model
is allowed to change here.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Protocol

from candlegraph.agents.base import Finding, Severity, overall_severity, sort_findings
from candlegraph.utils.logging_utils import service_logger

Completion = Callable[[str], str]


class Narrator(Protocol):
    def narrate(self, agent: str, findings: Sequence[Finding]) -> str: ...


class TemplateNarrator:
    """A fixed-form summary: status, counts, and the worst few findings."""

    def __init__(self, highlights: int = 3) -> None:
        self.highlights = highlights

    def narrate(self, agent: str, findings: Sequence[Finding]) -> str:
        if not findings:
            return f"{agent}: nothing to report."
        status = overall_severity(findings)
        counts = {
            level: sum(1 for f in findings if f.severity == level)
            for level in (Severity.CRITICAL, Severity.WARNING, Severity.INFO, Severity.OK)
        }
        parts = [f"{n} {level.label}" for level, n in counts.items() if n]
        lead = f"{agent} finished {status.label.upper()}: {len(findings)} findings ({', '.join(parts)})."
        if status == Severity.OK:
            return lead
        worst = [f for f in sort_findings(findings) if f.severity >= Severity.INFO][: self.highlights]
        return " ".join([lead, *(f"{f.subject}: {f.message}" for f in worst)])


_PROMPT = """You are writing the summary paragraph of an operations report for a market-data
service. Below is the complete list of findings as JSON. Write one short paragraph
(at most four sentences) for an engineer. State the overall status first, then the
findings that need action, worst first. Use only facts present in the findings; do
not speculate about causes, do not add recommendations that are not in a finding,
and do not mention anything that is not listed. Plain text, no Markdown.

Agent: {agent}
Status: {status}
Findings:
{findings}
"""


class AnthropicNarrator:
    """Ask a Claude model to write the paragraph from the findings alone."""

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        complete: Completion | None = None,
        fallback: Narrator | None = None,
    ) -> None:
        self.model = model
        self._api_key = api_key
        self._complete = complete or self._anthropic_completion
        self._fallback = fallback or TemplateNarrator()

    def narrate(self, agent: str, findings: Sequence[Finding]) -> str:
        if not findings:
            return self._fallback.narrate(agent, findings)
        prompt = _PROMPT.format(
            agent=agent,
            status=overall_severity(findings).label,
            findings=json.dumps([f.to_dict() for f in sort_findings(findings)], indent=2, default=str),
        )
        try:
            text = self._complete(prompt).strip()
        except Exception as exc:
            service_logger.warning("Narration via %s failed; using the template: %s", self.model, exc)
            return self._fallback.narrate(agent, findings)
        return text or self._fallback.narrate(agent, findings)

    def _anthropic_completion(self, prompt: str) -> str:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on the optional extra
            raise RuntimeError("install the `llm` extra: uv sync --extra llm") from exc
        client = anthropic.Anthropic(api_key=self._api_key)
        response = client.messages.create(
            model=self.model, max_tokens=400, messages=[{"role": "user", "content": prompt}]
        )
        return "".join(getattr(block, "text", "") for block in response.content)
