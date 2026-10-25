"""Fixtures for the agent tier: a scripted shell and finding factories.

Nothing here touches a subprocess, a database, or the network. The janitor's
whole decision logic runs against :class:`FakeShell`, and the observer runs
against the shared :class:`tests.conftest.FakeDatabase`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from candlegraph.agents.base import Finding, Severity
from candlegraph.agents.shell import CommandResult


def ok(stdout: str = "", duration: float = 0.5) -> CommandResult:
    return CommandResult(("x",), 0, stdout, "", duration)


def fail(code: int = 1, stderr: str = "boom", stdout: str = "") -> CommandResult:
    return CommandResult(("x",), code, stdout, stderr, 0.5)


def timeout() -> CommandResult:
    return CommandResult(("x",), 124, "", "timed out", 900.0, timed_out=True)


class FakeShell:
    """Replays scripted results by command string and records every call.

    ``scripts`` maps a command (``"make fmt-check"``) to a result or a list
    of results consumed in order; the last one repeats. Lookup is exact
    first, then longest-prefix, so ``"git checkout -b"`` matches whatever
    branch name follows. Anything unscripted succeeds with empty output.
    """

    def __init__(self, scripts: dict[str, CommandResult | list[CommandResult]] | None = None) -> None:
        self.scripts: dict[str, list[CommandResult]] = {
            key: list(value) if isinstance(value, list) else [value] for key, value in (scripts or {}).items()
        }
        self.calls: list[tuple[str, Path | None, float | None]] = []

    def run(
        self, args: Sequence[str], cwd: Path | None = None, timeout: float | None = None
    ) -> CommandResult:
        command = " ".join(args)
        self.calls.append((command, cwd, timeout))
        key = command if command in self.scripts else self._prefix(command)
        if key is None:
            return CommandResult(tuple(args), 0, "", "", 0.1)
        queue = self.scripts[key]
        result = queue.pop(0) if len(queue) > 1 else queue[0]
        return CommandResult(
            tuple(args),
            result.returncode,
            result.stdout,
            result.stderr,
            result.duration_seconds,
            result.timed_out,
        )

    def _prefix(self, command: str) -> str | None:
        matches = [key for key in self.scripts if command.startswith(key)]
        return max(matches, key=len) if matches else None

    def commands(self, prefix: str = "") -> list[str]:
        return [command for command, _, _ in self.calls if command.startswith(prefix)]


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def finding(
    severity: Severity = Severity.OK, kind: str = "check", subject: str = "x", message: str = "m"
) -> Finding:
    return Finding(kind, severity, subject, message)


def run_row(
    started: datetime,
    status: str = "succeeded",
    trigger: str = "scheduled",
    duration: float = 12.0,
    token_hours: int = 30,
    errors: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "run_id": f"run-{started:%Y%m%d%H%M}",
        "trigger": trigger,
        "status": status,
        "started_at": started,
        "finished_at": started,
        "duration_seconds": duration,
        "token_hours_written": token_hours,
        "pool_hours_written": 0,
        "errors": errors or {},
    }
