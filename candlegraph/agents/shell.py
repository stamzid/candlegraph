"""Running commands from a graph, behind a Protocol.

Graphs that call ``make``, ``git`` or ``gh`` never touch :mod:`subprocess`
directly. They take a :class:`Shell`, production hands them
:class:`SubprocessShell`, and the tests hand them a fake that replays scripted
results. That is what makes the janitor's decision logic a unit test instead
of a CI run.

Output that goes into a report is passed through :func:`redact` first. The
checks run with the developer's environment, and a traceback can quote an
environment variable.
"""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

# Exit codes for the two failures that are not the command's own.
TIMED_OUT = 124
NOT_FOUND = 127

_SECRET_PATTERNS = (
    # "Authorization: Bearer abc..." and "bearer abc..."
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"),
    # KEY=value, token: value, "password": "value" and friends.
    re.compile(
        r"(?i)((?:api[_-]?key|token|secret|password|passwd|pwd)\w*[\"']?\s*[=:]\s*[\"']?)[^\s\"',;]{4,}"
    ),
    # postgresql://user:password@host
    re.compile(r"(?i)((?:postgres(?:ql)?|https?)://[^:/\s]+:)[^@\s]+(@)"),
    # A bare Tavily key ("tvly-...") pasted without any KEY= prefix.
    re.compile(r"(tvly-)[A-Za-z0-9._\-]{4,}"),
)


def redact(text: str) -> str:
    """Mask anything shaped like a credential. Cheap, and errs towards masking."""
    out = text
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(
            lambda m: m.group(1) + "***" + (m.group(2) if m.lastindex and m.lastindex > 1 else ""), out
        )
    return out


@dataclass(frozen=True)
class CommandResult:
    """What one command did."""

    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def command(self) -> str:
        return " ".join(self.args)

    def tail(self, lines: int = 40) -> str:
        """The last ``lines`` lines of combined output, redacted."""
        combined = (self.stdout + ("\n" if self.stdout and self.stderr else "") + self.stderr).strip()
        kept = combined.splitlines()[-lines:] if lines > 0 else []
        return redact("\n".join(kept))


class Shell(Protocol):
    """Run a command and report what happened. Never raises for a failing command."""

    def run(
        self, args: Sequence[str], cwd: Path | None = None, timeout: float | None = None
    ) -> CommandResult: ...


class SubprocessShell:
    """The real thing."""

    def run(
        self, args: Sequence[str], cwd: Path | None = None, timeout: float | None = None
    ) -> CommandResult:
        started = time.monotonic()
        argv = tuple(args)
        try:
            completed = subprocess.run(
                list(argv),
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                argv,
                TIMED_OUT,
                _text(exc.stdout),
                _text(exc.stderr) + f"\ntimed out after {timeout}s",
                time.monotonic() - started,
                timed_out=True,
            )
        except FileNotFoundError as exc:
            return CommandResult(
                argv, NOT_FOUND, "", f"{argv[0]}: not found ({exc})", time.monotonic() - started
            )
        return CommandResult(
            argv, completed.returncode, completed.stdout, completed.stderr, time.monotonic() - started
        )


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value
