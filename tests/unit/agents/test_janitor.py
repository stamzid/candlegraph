"""The janitor graph against a scripted shell.

Every scenario in ``docs/agents/ci-janitor.md`` is here: a green run, a
mechanical fix that is re-checked, a dirty tree that is left alone, the
draft-PR command sequence, the refusal to commit a secret, and a check that
could not run at all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from candlegraph.agents.base import Severity
from candlegraph.agents.janitor import FIXERS, build_janitor_graph, run_janitor
from candlegraph.agents.janitor.checks import JanitorDeps, pr_branch_name
from candlegraph.agents.narrator import TemplateNarrator
from candlegraph.agents.reporting import LedgerSink
from candlegraph.agents.scheduler import janitor_tick
from tests.conftest import FakeDatabase, make_settings
from tests.unit.agents.conftest import FakeShell, fail, ok, timeout

NOW = datetime(2026, 10, 25, 6, 0, tzinfo=UTC)
ROOT = Path("/repo")
CHECKS = ("fmt-check", "lint", "typecheck", "test-unit")


def clean_git() -> dict[str, list[ok] | ok]:  # type: ignore[valid-type]
    return {
        "git status --porcelain --untracked-files=no": ok(""),
        "git rev-parse --abbrev-ref HEAD": ok("main\n"),
    }


def janitor(
    shell: FakeShell, open_pr: bool = False, checks: tuple[str, ...] = CHECKS, db: FakeDatabase | None = None
):  # type: ignore[no-untyped-def]
    sinks = [LedgerSink(db)] if db else []
    return run_janitor(
        make_settings(janitor_open_pr=open_pr),
        shell=shell,
        repo_root=ROOT,
        checks=checks,
        now=NOW,
        sinks=sinks,
    )


# --- green -----------------------------------------------------------------


def test_a_green_run_is_ok_runs_no_fixer_and_touches_no_branch() -> None:
    shell = FakeShell(clean_git())

    report = janitor(shell)

    assert report.status == Severity.OK
    assert {f.subject for f in report.findings} == set(CHECKS)
    assert shell.commands("make") == [f"make {c}" for c in CHECKS]
    assert not shell.commands("git checkout") and not shell.commands("gh")
    assert all(timeout == 900.0 for command, _, timeout in shell.calls if command.startswith("make"))
    assert all(cwd == ROOT for _, cwd, _ in shell.calls)


# --- fixing ----------------------------------------------------------------


def test_a_formatting_failure_is_fixed_rechecked_and_reported_as_info() -> None:
    shell = FakeShell(
        {
            **clean_git(),
            "make fmt-check": [fail(1, "Would reformat: a.py"), ok()],
            "make lint": [fail(1, "F401 unused import"), ok()],
            "git diff --stat": ok(" 2 files changed, 4 insertions(+), 3 deletions(-)\n"),
            "git diff --name-only": ok("a.py\nb.py\n"),
        }
    )

    report = janitor(shell)

    assert report.status == Severity.INFO
    assert shell.commands("make") == [
        "make fmt-check",
        "make lint",
        "make typecheck",
        "make test-unit",
        "make fmt",
        "make fmt-check",
        "make lint",
    ]
    fixed = {f.subject: f for f in report.findings}
    assert (
        fixed["fmt-check"].message == "passed after make fmt"
        and fixed["fmt-check"].evidence["before"] == "failed"
    )
    assert "2 files changed in the working tree (no PR requested)" in fixed["make fmt"].message
    assert "| fmt-check | failed | ok | make fmt |" in report.markdown
    assert "2 files changed, 4 insertions" in report.markdown


def test_one_fixer_runs_once_even_when_two_checks_point_at_it() -> None:
    assert FIXERS["fmt-check"] == FIXERS["lint"] == "fmt"
    shell = FakeShell({**clean_git(), "make fmt-check": [fail(), ok()], "make lint": [fail(), ok()]})

    janitor(shell)

    assert shell.commands("make fmt").count("make fmt") == 1


def test_a_fix_that_does_not_make_the_check_pass_leaves_it_a_warning() -> None:
    shell = FakeShell({**clean_git(), "make lint": fail(1, "E999 SyntaxError")})

    report = janitor(shell)

    assert report.status == Severity.WARNING
    (lint,) = [f for f in report.findings if f.subject == "lint"]
    assert lint.kind == "check_failed" and "E999 SyntaxError" in lint.message
    assert "fixer ran but changed no tracked files" in report.markdown


def test_unfixable_failures_are_reported_and_no_fixer_runs() -> None:
    shell = FakeShell({**clean_git(), "make typecheck": fail(1, "error: Incompatible return type")})

    report = janitor(shell)

    assert report.status == Severity.WARNING
    assert "make fmt" not in shell.commands()
    assert "### make typecheck" in report.markdown and "Incompatible return type" in report.markdown


def test_a_dirty_tree_is_never_fixed_in_place() -> None:
    shell = FakeShell(
        {
            **clean_git(),
            "git status --porcelain --untracked-files=no": ok(" M candlegraph/cli.py\n"),
            "make fmt-check": fail(),
        }
    )

    report = janitor(shell)

    assert "make fmt" not in shell.commands()
    kinds = {f.kind for f in report.findings}
    assert "skipped_fix" in kinds and "check_failed" in kinds
    assert report.status == Severity.WARNING


def test_outside_a_git_checkout_the_checks_still_run_but_nothing_is_fixed() -> None:
    shell = FakeShell(
        {
            "git status --porcelain --untracked-files=no": fail(128, "fatal: not a git repository"),
            "make fmt-check": fail(),
        }
    )

    report = janitor(shell)

    assert "make fmt" not in shell.commands()
    assert any(f.kind == "error" and "git status failed" in f.message for f in report.findings)


# --- the PR ----------------------------------------------------------------


def pr_shell(**extra: object) -> FakeShell:
    scripts: dict = {  # type: ignore[type-arg]
        **clean_git(),
        "make fmt-check": [fail(), ok()],
        "git diff --stat": ok(" 1 file changed\n"),
        "git diff --name-only": ok("candlegraph/cli.py\n"),
        "git diff --cached --name-only": ok("candlegraph/cli.py\n"),
        "gh pr create": ok("Creating draft pull request\nhttps://github.com/o/r/pull/42\n"),
    }
    scripts.update(extra)
    return FakeShell(scripts)


def test_open_pr_branches_commits_tracked_files_pushes_drafts_and_returns() -> None:
    shell = pr_shell()

    report = janitor(shell, open_pr=True)

    branch = pr_branch_name(NOW)
    assert branch == "janitor/20261025-0600"
    git = [
        c
        for c in shell.commands()
        if c.startswith(("git checkout", "git add", "git commit", "git push", "gh pr"))
    ]
    assert git[0] == f"git checkout -b {branch}"
    assert git[1] == "git add -u", "tracked files only, so .env can never be staged"
    assert git[2].startswith("git commit -q -m janitor: fix fmt-check")
    assert git[3] == f"git push -u origin {branch}"
    assert git[4].startswith(
        f"gh pr create --draft --base main --head {branch} --title janitor: fix fmt-check"
    )
    assert git[5] == "git checkout main", "the tree is left on the branch it started on"
    (fixed,) = [f for f in report.findings if f.kind == "fixed"]
    assert fixed.message.endswith("draft PR https://github.com/o/r/pull/42")
    assert report.status == Severity.INFO


def test_without_open_pr_the_fix_stays_in_the_working_tree() -> None:
    shell = pr_shell()

    janitor(shell, open_pr=False)

    assert not shell.commands("git checkout") and not shell.commands("gh")


def test_a_staged_secret_aborts_the_pr_and_cleans_up() -> None:
    shell = pr_shell(**{"git diff --cached --name-only": ok("candlegraph/cli.py\n.env\n")})

    report = janitor(shell, open_pr=True)

    assert not shell.commands("git commit") and not shell.commands("git push") and not shell.commands("gh")
    assert (
        shell.commands("git reset -q")
        and "git checkout main" in shell.commands()
        and shell.commands("git branch -D")
    )
    (error,) = [f for f in report.findings if f.kind == "error"]
    assert "staged paths look secret: .env" in error.message
    assert report.status == Severity.WARNING


def test_a_failed_push_returns_to_the_original_branch_and_is_reported() -> None:
    shell = pr_shell(**{"git push -u origin": fail(1, "remote: permission denied")})

    report = janitor(shell, open_pr=True)

    assert not shell.commands("gh")
    assert shell.commands()[-1] == "git checkout main"
    assert any("git push failed" in f.message for f in report.findings)


def test_a_failed_gh_still_returns_and_keeps_the_branch() -> None:
    shell = pr_shell(**{"gh pr create": fail(4, "gh: not logged in")})

    report = janitor(shell, open_pr=True)

    assert shell.commands()[-1] == "git checkout main"
    assert any("gh pr create failed" in f.message for f in report.findings)
    assert "was created but no PR was opened" in report.markdown


# --- could not run ---------------------------------------------------------


def test_a_timed_out_check_is_critical() -> None:
    shell = FakeShell({**clean_git(), "make test-unit": timeout()})

    report = janitor(shell)

    assert report.status == Severity.CRITICAL
    (f,) = [f for f in report.findings if f.subject == "test-unit"]
    assert f.message.startswith("could not run")


def test_make_missing_is_critical() -> None:
    shell = FakeShell({**clean_git(), "make fmt-check": fail(127, "make: not found")})

    report = janitor(shell, checks=("fmt-check",))

    assert report.status == Severity.CRITICAL


# --- plumbing --------------------------------------------------------------


def test_the_ledger_sink_records_the_run(fake_db: FakeDatabase) -> None:
    janitor(FakeShell(clean_git()), db=fake_db)

    label, params = fake_db.executed[0]
    assert label == "insert_agent_run" and params is not None and params["agent"] == "janitor"


def test_output_is_redacted_before_it_reaches_the_report() -> None:
    shell = FakeShell({**clean_git(), "make typecheck": fail(1, "GRAPH_API_KEY=abcdef0123456789 leaked")})

    report = janitor(shell)

    assert "abcdef0123456789" not in report.markdown
    assert "GRAPH_API_KEY=***" in report.markdown


def test_the_graph_has_the_documented_shape() -> None:
    deps = JanitorDeps(
        FakeShell(), ROOT, CHECKS, 1.0, 10, False, "main", TemplateNarrator(), [], "r", NOW, NOW
    )
    drawing = build_janitor_graph(deps).get_graph()

    assert set(drawing.nodes) == {
        "__start__",
        "inspect_tree",
        "run_checks",
        "apply_fixes",
        "rerun_checks",
        "draft_pr",
        "assess",
        "publish",
        "__end__",
    }
    edges = {(e.source, e.target) for e in drawing.edges}
    assert {
        ("run_checks", "apply_fixes"),
        ("run_checks", "assess"),
        ("rerun_checks", "draft_pr"),
        ("rerun_checks", "assess"),
        ("draft_pr", "assess"),
    } <= edges


def test_the_scheduler_tick_never_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        "candlegraph.agents.janitor.run_janitor", lambda _s: (_ for _ in ()).throw(RuntimeError("x"))
    )

    with caplog.at_level("ERROR", logger="candlegraph"):
        assert janitor_tick(make_settings()) is None

    assert "Janitor run failed" in caplog.text
