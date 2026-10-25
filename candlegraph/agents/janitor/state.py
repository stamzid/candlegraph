"""The janitor's state, check results, and the one map that decides what it may fix."""

from __future__ import annotations

import operator
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

from candlegraph.agents.base import AgentReport, Finding

# Which make target repairs which failing check. Only mechanical fixes belong
# here: a formatter and a linter's own --fix. A failing type check or test is
# reported, never "fixed" — there is no mechanical fix for a wrong type, and
# a tool rewriting a test until it passes is the failure mode CLAUDE.md warns
# about.
FIXERS: dict[str, str] = {
    "fmt-check": "fmt",
    "lint": "fmt",
}

# Staged paths that abort a PR outright. `git add -u` only stages tracked
# files, so these can only appear if someone tracked a secret; refuse anyway.
FORBIDDEN_STAGED = (".env", ".env.local", ".pem", ".key", "id_rsa", "id_ed25519")


@dataclass(frozen=True)
class CheckResult:
    """One make target's outcome."""

    target: str
    returncode: int
    duration_seconds: float
    output: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def label(self) -> str:
        if self.timed_out:
            return "timed out"
        return "ok" if self.ok else "failed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "ok": self.ok,
            "returncode": self.returncode,
            "duration_seconds": round(self.duration_seconds, 2),
            "timed_out": self.timed_out,
        }


class JanitorState(TypedDict, total=False):
    git_ok: bool
    tree_clean: bool
    branch: str
    before: list[CheckResult]
    checks: list[CheckResult]
    fixes_applied: list[str]
    diff_stat: str
    changed_files: list[str]
    pr_branch: str | None
    pr_url: str | None
    findings: Annotated[list[Finding], operator.add]
    errors: Annotated[list[str], operator.add]
    report: AgentReport
