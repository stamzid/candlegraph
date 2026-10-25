"""The janitor's nodes: run checks, apply fixers, draft the PR, assess.

Everything that touches the outside world goes through ``deps.shell``, so the
whole graph runs under test against scripted command results.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from candlegraph.agents.base import Finding, Severity
from candlegraph.agents.janitor.state import FIXERS, FORBIDDEN_STAGED, CheckResult, JanitorState
from candlegraph.agents.narrator import Narrator
from candlegraph.agents.reporting import Sink
from candlegraph.agents.shell import NOT_FOUND, TIMED_OUT, CommandResult, Shell

GIT_TIMEOUT = 120.0


@dataclass
class JanitorDeps:
    shell: Shell
    repo_root: Path
    checks: tuple[str, ...]
    timeout: float
    output_lines: int
    open_pr: bool
    base_branch: str
    narrator: Narrator
    sinks: Sequence[Sink]
    run_id: str
    started_at: datetime
    now: datetime

    def git(self, *args: str) -> CommandResult:
        return self.shell.run(["git", *args], cwd=self.repo_root, timeout=GIT_TIMEOUT)

    def make(self, target: str, timeout: float | None = None) -> CheckResult:
        result = self.shell.run(["make", target], cwd=self.repo_root, timeout=timeout or self.timeout)
        return CheckResult(
            target=target,
            returncode=result.returncode,
            duration_seconds=result.duration_seconds,
            output=result.tail(self.output_lines),
            timed_out=result.timed_out,
        )


# --- nodes -----------------------------------------------------------------


def inspect_tree(deps: JanitorDeps, state: JanitorState) -> dict[str, Any]:
    """Is this a git checkout, is it clean, and what branch is it on?"""
    status = deps.git("status", "--porcelain", "--untracked-files=no")
    if not status.ok:
        return {
            "git_ok": False,
            "tree_clean": False,
            "branch": "",
            "errors": [f"git status failed ({status.returncode}): {status.tail(3)}"],
        }
    branch = deps.git("rev-parse", "--abbrev-ref", "HEAD")
    return {
        "git_ok": True,
        "tree_clean": status.stdout.strip() == "",
        "branch": branch.stdout.strip() if branch.ok else "HEAD",
    }


def run_checks(deps: JanitorDeps, state: JanitorState) -> dict[str, Any]:
    results = [deps.make(target) for target in deps.checks]
    return {"checks": results, "before": results}


def fixable_failures(state: JanitorState) -> list[CheckResult]:
    return [c for c in state.get("checks", []) if not c.ok and c.target in FIXERS]


def route_after_checks(state: JanitorState) -> str:
    """Fix only when something fixable failed and the tree is ours to touch."""
    if fixable_failures(state) and state.get("tree_clean"):
        return "apply_fixes"
    return "assess"


def apply_fixes(deps: JanitorDeps, state: JanitorState) -> dict[str, Any]:
    fixers: list[str] = []
    for check in fixable_failures(state):
        fixer = FIXERS[check.target]
        if fixer not in fixers:
            fixers.append(fixer)
    applied: list[str] = []
    errors: list[str] = []
    for fixer in fixers:
        result = deps.make(fixer)
        if result.ok:
            applied.append(fixer)
        else:
            errors.append(f"make {fixer} failed ({result.returncode}): {result.output.splitlines()[-1:]}")
    return {"fixes_applied": applied, "errors": errors}


def rerun_checks(deps: JanitorDeps, state: JanitorState) -> dict[str, Any]:
    """Re-run what failed; a fix that does not make the check pass is not a fix."""
    previous = state.get("checks", [])
    rerun = {c.target: deps.make(c.target) for c in previous if not c.ok}
    merged = [rerun.get(c.target, c) for c in previous]
    stat = deps.git("diff", "--stat")
    names = deps.git("diff", "--name-only")
    changed = [line.strip() for line in names.stdout.splitlines() if line.strip()] if names.ok else []
    return {"checks": merged, "diff_stat": stat.stdout.strip() if stat.ok else "", "changed_files": changed}


def route_after_rerun(deps: JanitorDeps, state: JanitorState) -> str:
    return "draft_pr" if deps.open_pr and state.get("changed_files") else "assess"


def pr_branch_name(now: datetime) -> str:
    return f"janitor/{now:%Y%m%d-%H%M}"


def _fixed_targets(state: JanitorState) -> list[str]:
    before = {c.target: c for c in state.get("before", [])}
    return [
        c.target for c in state.get("checks", []) if c.ok and c.target in before and not before[c.target].ok
    ]


def draft_pr(deps: JanitorDeps, state: JanitorState) -> dict[str, Any]:
    """Branch, commit only tracked changes, push, open a draft PR, come back."""
    original = state.get("branch") or "HEAD"
    branch = pr_branch_name(deps.now)
    fixed = _fixed_targets(state)
    title = f"janitor: fix {', '.join(fixed) if fixed else 'formatting'}"

    def fail(step: str, result: CommandResult) -> dict[str, Any]:
        deps.git("checkout", original)
        return {
            "pr_branch": branch,
            "pr_url": None,
            "errors": [f"{step} failed ({result.returncode}): {result.tail(3)}"],
        }

    created = deps.git("checkout", "-b", branch)
    if not created.ok:
        return {"pr_branch": branch, "pr_url": None, "errors": [f"git checkout -b failed: {created.tail(3)}"]}
    staged = deps.git("add", "-u")
    if not staged.ok:
        return fail("git add -u", staged)
    listing = deps.git("diff", "--cached", "--name-only")
    paths = [p.strip() for p in listing.stdout.splitlines() if p.strip()]
    forbidden = [p for p in paths if any(p == f or p.endswith(f) for f in FORBIDDEN_STAGED)]
    if forbidden:
        deps.git("reset", "-q")
        deps.git("checkout", original)
        deps.git("branch", "-D", branch)
        return {
            "pr_branch": None,
            "pr_url": None,
            "errors": [f"refused to commit: staged paths look secret: {', '.join(forbidden)}"],
        }
    body = (
        f"{title}\n\nMechanical fixes applied by the candlegraph janitor "
        f"(run {deps.run_id}) on {deps.now:%Y-%m-%d %H:%M} UTC.\n\n"
        f"Fixers: {', '.join(f'make {f}' for f in state.get('fixes_applied', [])) or '-'}\n"
        f"Files: {', '.join(paths)}\n"
    )
    committed = deps.git("commit", "-q", "-m", body)
    if not committed.ok:
        return fail("git commit", committed)
    pushed = deps.git("push", "-u", "origin", branch)
    if not pushed.ok:
        return fail("git push", pushed)
    pr = deps.shell.run(
        [
            "gh",
            "pr",
            "create",
            "--draft",
            "--base",
            deps.base_branch,
            "--head",
            branch,
            "--title",
            title,
            "--body",
            body + "\n" + state.get("diff_stat", ""),
        ],
        cwd=deps.repo_root,
        timeout=GIT_TIMEOUT,
    )
    deps.git("checkout", original)
    if not pr.ok:
        return {
            "pr_branch": branch,
            "pr_url": None,
            "errors": [f"gh pr create failed ({pr.returncode}): {pr.tail(3)}"],
        }
    return {"pr_branch": branch, "pr_url": pr.stdout.strip().splitlines()[-1] if pr.stdout.strip() else ""}


def assess(state: JanitorState) -> dict[str, Any]:
    """Findings from the final picture: what passes, what still fails, what was fixed."""
    findings: list[Finding] = []
    before = {c.target: c for c in state.get("before", [])}
    for check in state.get("checks", []):
        was = before.get(check.target)
        evidence = {**check.to_dict(), "before": was.label if was else None, "output": check.output}
        if check.ok:
            fixed = was is not None and not was.ok
            findings.append(
                Finding(
                    "check",
                    Severity.INFO if fixed else Severity.OK,
                    check.target,
                    f"passed after make {FIXERS.get(check.target, '?')}"
                    if fixed
                    else f"passed in {check.duration_seconds:.0f}s",
                    evidence,
                )
            )
        else:
            fatal = check.timed_out or check.returncode in (TIMED_OUT, NOT_FOUND)
            last = check.output.strip().splitlines()[-1] if check.output.strip() else "no output"
            findings.append(
                Finding(
                    "check_failed",
                    Severity.CRITICAL if fatal else Severity.WARNING,
                    check.target,
                    f"{'could not run' if fatal else 'failed'} (exit {check.returncode}): {last}",
                    evidence,
                )
            )
    if state.get("fixes_applied"):
        changed = state.get("changed_files", [])
        if state.get("pr_url"):
            message = f"{len(changed)} files changed; draft PR {state['pr_url']}"
        elif changed:
            message = f"{len(changed)} files changed in the working tree (no PR requested)"
        else:
            message = "fixer ran but changed no tracked files"
        findings.append(
            Finding(
                "fixed",
                Severity.INFO,
                ", ".join(f"make {f}" for f in state["fixes_applied"]),
                message,
                {"files": changed, "pr_url": state.get("pr_url"), "pr_branch": state.get("pr_branch")},
            )
        )
    elif fixable_failures(state) and state.get("git_ok") and not state.get("tree_clean"):
        findings.append(
            Finding(
                "skipped_fix",
                Severity.INFO,
                "working tree",
                "uncommitted changes present; fixes were not applied so they cannot mix with someone's work",
            )
        )
    for error in state.get("errors", []):
        findings.append(Finding("error", Severity.WARNING, "janitor", error))
    return {"findings": findings}
