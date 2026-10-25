"""The subprocess shell, its result object, and the redaction of secrets."""

from __future__ import annotations

import sys

from candlegraph.agents.shell import NOT_FOUND, CommandResult, SubprocessShell, redact


def test_redaction_masks_bearer_tokens_keys_and_connection_passwords() -> None:
    text = (
        "Authorization: Bearer abcdefghijklmnop\n"
        "GRAPH_API_KEY=0123456789abcdef\n"
        'postgresql://candlegraph:s3cretpass@postgres:5432/candlegraph "password": "hunter22"'
    )
    masked = redact(text)

    assert "abcdefghijklmnop" not in masked
    assert "0123456789abcdef" not in masked
    assert "s3cretpass" not in masked
    assert "hunter22" not in masked
    assert "postgresql://candlegraph:***@postgres" in masked


def test_redaction_leaves_ordinary_output_alone() -> None:
    text = "Ingested 168 hourly rows for WBTC (0x2260fac5e5542a773aa44fbcfedf7c193bc2c599)"

    assert redact(text) == text


def test_tail_keeps_the_last_lines_of_both_streams_redacted() -> None:
    result = CommandResult(("make", "lint"), 1, "a\nb\nc", "token=verysecretvalue", 1.0)

    assert result.tail(2) == "c\ntoken=***"
    assert result.command == "make lint"
    assert not result.ok


def test_the_real_shell_reports_exit_codes_without_raising() -> None:
    shell = SubprocessShell()

    passed = shell.run([sys.executable, "-c", "print('hi')"])
    failed = shell.run([sys.executable, "-c", "import sys; sys.exit(3)"])

    assert passed.ok and passed.stdout.strip() == "hi"
    assert failed.returncode == 3 and not failed.ok


def test_a_missing_executable_is_a_result_not_an_exception() -> None:
    result = SubprocessShell().run(["definitely-not-a-real-command-xyz"])

    assert result.returncode == NOT_FOUND
    assert "not found" in result.stderr


def test_a_timeout_is_a_result_not_an_exception() -> None:
    result = SubprocessShell().run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2)

    assert result.timed_out
    assert not result.ok
