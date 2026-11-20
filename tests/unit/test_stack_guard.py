"""`scripts/stack-guard.sh`: a stack is refused to every checkout but its own.

Pins docs/incidents/2026-11-20-stack-would-not-start-again/. A Compose
project name is the only thing separating two checkouts' stacks; when it was
shared, `make up` ran one checkout's migrations against the other's database,
and `make down` would have deleted it. The guard reads the owner Compose
already records on each container (`com.docker.compose.project.working_dir`).

Each test runs the real script against a fake `docker` on PATH, so none needs
a daemon.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from candlegraph.settings import REPO_ROOT

GUARD = REPO_ROOT / "scripts" / "stack-guard.sh"

FAKE_DOCKER = """#!/bin/sh
# Answers the three questions the guard asks, from the environment.
case "$1" in
  compose)
    [ -n "${FAKE_NO_DAEMON:-}" ] && exit 1
    echo "name: ${FAKE_PROJECT:-proj}"
    echo "services: {}"
    ;;
  ps)
    case "$*" in
      *-aq*) printf '%s' "${FAKE_IDS:-}" ;;
      *service=candlegraph*) printf '%s\\n' "${FAKE_LIST:-}" | sed '/^$/d' ;;
      *) printf '%s\\n' "${FAKE_OWNERS:-}" | sed '/^$/d' ;;
    esac
    ;;
  volume) printf '%s\\n' "${FAKE_VOLUMES:-}" | sed '/^$/d' ;;
esac
"""


FAKE_GIT = """#!/bin/sh
# `git status --porcelain -- alembic/versions`, answered from the environment.
[ -n "${FAKE_NOT_A_REPO:-}" ] && { echo "fatal: not a git repository" >&2; exit 128; }
printf '%s\\n' "${FAKE_GIT_STATUS:-}" | sed '/^$/d'
"""


@pytest.fixture
def bin_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "bin"
    directory.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("git", FAKE_GIT)):
        tool = directory / name
        tool.write_text(body)
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    return directory


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    directory = tmp_path / "oak"
    directory.mkdir()
    return directory


def guard(bin_dir: Path, checkout: Path, action: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(GUARD), action],
        cwd=checkout,
        env={"PATH": f"{bin_dir}:{os.environ['PATH']}", **env},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("action", ["up", "down", "reset"])
def test_a_project_with_no_containers_is_this_checkouts_to_use(
    bin_dir: Path, checkout: Path, action: str
) -> None:
    assert guard(bin_dir, checkout, action).returncode == 0


@pytest.mark.parametrize("action", ["up", "down", "reset"])
def test_this_checkouts_own_stack_passes(bin_dir: Path, checkout: Path, action: str) -> None:
    result = guard(bin_dir, checkout, action, FAKE_OWNERS=str(checkout))

    assert result.returncode == 0, result.stderr


def test_the_same_checkout_reached_through_a_symlink_is_still_its_own(
    bin_dir: Path, checkout: Path, tmp_path: Path
) -> None:
    """Compose records the path it was given; `pwd -P` resolves. Both sides
    are resolved, so `/tmp` and `/private/tmp` on a Mac are one checkout."""
    alias = tmp_path / "alias"
    alias.symlink_to(checkout)

    assert guard(bin_dir, checkout, "down", FAKE_OWNERS=str(alias)).returncode == 0


@pytest.mark.parametrize("action", ["up", "down", "reset"])
def test_another_checkouts_stack_is_refused(
    bin_dir: Path, checkout: Path, tmp_path: Path, action: str
) -> None:
    other = tmp_path / "elm"
    other.mkdir()

    result = guard(bin_dir, checkout, action, FAKE_PROJECT="elm", FAKE_OWNERS=str(other))

    assert result.returncode == 1
    assert f"Refusing to '{action}' Compose project 'elm'" in result.stderr
    assert str(other) in result.stderr and "which still exists" in result.stderr


def test_refusing_up_says_it_would_migrate_the_other_database(
    bin_dir: Path, checkout: Path, tmp_path: Path
) -> None:
    other = tmp_path / "elm"
    other.mkdir()

    result = guard(bin_dir, checkout, "up", FAKE_PROJECT="elm", FAKE_OWNERS=str(other))

    assert "run this checkout's migrations against its database" in result.stderr


@pytest.mark.parametrize("action", ["down", "reset"])
def test_refusing_down_says_it_would_delete_the_other_database(
    bin_dir: Path, checkout: Path, tmp_path: Path, action: str
) -> None:
    other = tmp_path / "elm"
    other.mkdir()

    result = guard(bin_dir, checkout, action, FAKE_PROJECT="elm", FAKE_OWNERS=str(other))

    assert "delete its database volume" in result.stderr


def test_the_refusal_gives_a_command_that_would_not_collide(
    bin_dir: Path, checkout: Path, tmp_path: Path
) -> None:
    other = tmp_path / "elm"
    other.mkdir()

    result = guard(bin_dir, checkout, "up", FAKE_PROJECT="elm", FAKE_OWNERS=str(other))

    assert "COMPOSE_PROJECT_NAME=cg-oak-" in result.stderr
    assert "OLLAMA_HOST_PORT=" in result.stderr, "a second stack fails on 11434 without its own"
    assert "make stacks" in result.stderr and "make up FORCE=1" in result.stderr


def test_a_stack_whose_checkout_was_deleted_says_so(bin_dir: Path, checkout: Path, tmp_path: Path) -> None:
    gone = tmp_path / "deleted-worktree"

    result = guard(bin_dir, checkout, "down", FAKE_PROJECT="old", FAKE_OWNERS=str(gone))

    assert result.returncode == 1
    assert "which no longer exists" in result.stderr


def test_force_lets_a_person_take_a_stack_over_out_loud(
    bin_dir: Path, checkout: Path, tmp_path: Path
) -> None:
    other = tmp_path / "elm"
    other.mkdir()

    result = guard(bin_dir, checkout, "down", FAKE_PROJECT="elm", FAKE_OWNERS=str(other), FORCE="1")

    assert result.returncode == 0
    assert "FORCE=1" in result.stderr and str(other) in result.stderr


def test_one_foreign_container_among_this_checkouts_is_enough_to_refuse(
    bin_dir: Path, checkout: Path, tmp_path: Path
) -> None:
    """Mixed ownership is the state an `up` from the wrong checkout leaves behind."""
    other = tmp_path / "elm"
    other.mkdir()

    result = guard(bin_dir, checkout, "down", FAKE_OWNERS=f"{checkout}\n{other}")

    assert result.returncode == 1


def test_without_a_daemon_the_guard_stands_aside_for_compose_to_report(bin_dir: Path, checkout: Path) -> None:
    assert guard(bin_dir, checkout, "up", FAKE_NO_DAEMON="1").returncode == 0


def test_an_unknown_action_is_a_usage_error(bin_dir: Path, checkout: Path) -> None:
    assert guard(bin_dir, checkout, "prune").returncode == 2


def test_the_list_marks_this_checkouts_stack(bin_dir: Path, checkout: Path, tmp_path: Path) -> None:
    other = tmp_path / "elm"
    other.mkdir()
    lines = f"oak|Up 2 minutes|{checkout}\nelm|Up 1 hour|{other}"

    result = guard(bin_dir, checkout, "list", FAKE_LIST=lines, FAKE_VOLUMES="old_pgdata")

    assert result.returncode == 0
    [mine] = [line for line in result.stdout.splitlines() if line.startswith("oak ")]
    assert mine.endswith("<- this checkout")
    [theirs] = [line for line in result.stdout.splitlines() if line.startswith("elm ")]
    assert "this checkout" not in theirs
    assert "old_pgdata" in result.stdout, "a volume with no containers is listed as unattributable"


@pytest.mark.parametrize("target", ["up", "down", "reset", "otel-up"])
def test_every_target_that_creates_or_destroys_a_stack_asks_the_guard_first(target: str) -> None:
    """The guard protects nothing if a target skips it."""
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    recipe = makefile.split(f"\n{target}:", 1)[1].split("\n\n", 1)[0]
    first_command = next(line.strip() for line in recipe.splitlines()[1:] if line.startswith("\t"))

    assert first_command.startswith("@$(GUARD)"), f"`make {target}` must run the guard before Compose"


# --- a draft migration must not reach the long-lived default volume ----------

DRAFT = "?? alembic/versions/1234abcd5678_draft.py"


def test_up_on_the_default_stack_with_an_uncommitted_migration_is_refused(
    bin_dir: Path, checkout: Path
) -> None:
    """The fault on record twice: a draft applied to the default volume, then
    renamed before commit, leaves a volume no checkout can start."""
    result = guard(bin_dir, checkout, "up", FAKE_GIT_STATUS=DRAFT)

    assert result.returncode == 1
    assert "1234abcd5678_draft.py" in result.stderr
    assert "COMPOSE_PROJECT_NAME=cg-oak-" in result.stderr and "make up FORCE=1" in result.stderr


def test_a_modified_committed_migration_is_refused_too(bin_dir: Path, checkout: Path) -> None:
    """Rewriting a committed revision's body is the same hazard as renaming a draft."""
    result = guard(bin_dir, checkout, "up", FAKE_GIT_STATUS=" M alembic/versions/9d4b7a2c6e15_token_daily.py")

    assert result.returncode == 1


def test_a_named_throwaway_stack_may_try_a_draft(bin_dir: Path, checkout: Path) -> None:
    result = guard(bin_dir, checkout, "up", FAKE_GIT_STATUS=DRAFT, COMPOSE_PROJECT_NAME="cg-oak-1")

    assert result.returncode == 0, result.stderr


def test_force_applies_a_final_revision_to_the_default_stack(bin_dir: Path, checkout: Path) -> None:
    assert guard(bin_dir, checkout, "up", FAKE_GIT_STATUS=DRAFT, FORCE="1").returncode == 0


def test_a_clean_migration_directory_passes(bin_dir: Path, checkout: Path) -> None:
    assert guard(bin_dir, checkout, "up").returncode == 0


def test_bytecode_or_notes_beside_the_migrations_are_not_drafts(bin_dir: Path, checkout: Path) -> None:
    status = "?? alembic/versions/__pycache__/x.cpython-312.pyc\n?? alembic/versions/NOTES.md"

    assert guard(bin_dir, checkout, "up", FAKE_GIT_STATUS=status).returncode == 0


def test_outside_a_git_checkout_the_draft_check_stands_aside(bin_dir: Path, checkout: Path) -> None:
    assert guard(bin_dir, checkout, "up", FAKE_NOT_A_REPO="1").returncode == 0


@pytest.mark.parametrize("action", ["down", "reset"])
def test_taking_a_stack_down_is_never_blocked_by_a_draft(bin_dir: Path, checkout: Path, action: str) -> None:
    """Rebuilding the volume is the *remedy* for a draft that reached it."""
    assert guard(bin_dir, checkout, action, FAKE_GIT_STATUS=DRAFT).returncode == 0
