"""The MCP server over a real stdio transport, against a real database.

``docs/knowledge/mcp/DESIGN.md``: *"a single integration test starts the
server over stdio and lists tools, so the schema generation is exercised"*.
The unit tier proves what each tool returns; what only this can prove is
that the process starts, speaks the protocol, and answers a tool call with
the same numbers a direct read gives.

It is the stage's exit criterion for step 10, in three assertions: the tools
list, ``data_quality_report()`` matches ``report --json``, and no listed tool
can write market data.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from candlegraph.ingestion.quality import build_report, report_dict
from candlegraph.settings import REPO_ROOT
from candlegraph.storage import DatabaseManager
from candlegraph.tokens import load_tokens
from tests.conftest import TOKENS_YAML
from tests.integration.test_timescale import BASE, HOUR, hour_record, write_hours

pytestmark = pytest.mark.integration

pytest.importorskip("mcp", reason="the MCP extra is not installed")

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

# Long enough for a cold interpreter start plus pandas; short enough that a
# hung server fails the run rather than stalling it.
TIMEOUT_SECONDS = 120


@pytest.fixture
def seeded(db: DatabaseManager) -> DatabaseManager:
    """A database with one token's worth of contiguous hours stored."""
    write_hours(db, [hour_record(BASE + i * HOUR) for i in range(48)])
    return db


@pytest.fixture
def tokens_path(tmp_path: Path) -> Path:
    path = tmp_path / "tokens.yaml"
    path.write_text(TOKENS_YAML, encoding="utf-8")
    return path


async def _session(dsn: str, tokens_path: Path) -> AsyncIterator[ClientSession]:
    """A client talking to `candlegraph mcp` over its own pipes."""
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "candlegraph", "mcp"],
        cwd=str(REPO_ROOT),
        env={
            **os.environ,
            "DATABASE_URL": dsn,
            "DB_SCHEMA": "candlegraph",
            "TOKENS_FILE": str(tokens_path),
            "INGEST_ENABLED": "false",
            # No model server is reachable from a test runner.
            "EMBEDDING_PROVIDER": "fake",
        },
    )
    async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        yield session


def _structured(result: Any) -> dict[str, Any]:
    """The tool's JSON body, whichever way the SDK carried it."""
    if getattr(result, "structured_content", None):
        return dict(result.structured_content)
    return dict(json.loads(result.content[0].text))


async def test_a_client_lists_the_tools_over_stdio(seeded: DatabaseManager, tokens_path: Path) -> None:
    """Starting the process and generating every schema, for real."""
    async for session in _session(seeded.dsn, tokens_path):
        listed = (await session.list_tools()).tools

        names = {tool.name for tool in listed}
        assert {"status", "data_quality_report", "candles", "observe", "explain_signal"} <= names
        assert len(names) == 14
        assert all(tool.description for tool in listed)


async def test_no_listed_tool_can_write_market_data(seeded: DatabaseManager, tokens_path: Path) -> None:
    """The last clause of the exit criterion, read off the live listing."""
    async for session in _session(seeded.dsn, tokens_path):
        listed = (await session.list_tools()).tools

        writers = {tool.name for tool in listed if not (tool.annotations and tool.annotations.read_only_hint)}
        assert writers == {"observe", "explain_signal"}


async def test_the_data_quality_tool_answers_with_what_report_json_prints(
    seeded: DatabaseManager, tokens_path: Path
) -> None:
    """Same database, same structure, over the protocol."""
    expected = report_dict(
        build_report(seeded, expected_symbols=load_tokens(tokens_path).symbols, min_expected_coverage=0.8)
    )

    async for session in _session(seeded.dsn, tokens_path):
        result = await session.call_tool("data_quality_report", {})

    payload = _structured(result)
    assert payload.pop("generated_at")
    expected.pop("generated_at")
    assert payload == expected
    assert payload["tokens"][0]["symbol"] == "WBTC"
    assert payload["tokens"][0]["stored_hours"] == 48


async def test_a_tool_reads_the_seeded_rows_through_the_protocol(
    seeded: DatabaseManager, tokens_path: Path
) -> None:
    async for session in _session(seeded.dsn, tokens_path):
        result = await session.call_tool("candles", {"symbol": "WBTC", "hours": 24})

    payload = _structured(result)
    assert payload["symbol"] == "WBTC"
    # Buckets are aligned to the epoch, not to the first row, so 48 hours
    # starting mid-day fall into three midnight-UTC buckets.
    assert [bar["time"] for bar in payload["candles"]] == [
        "2023-11-14T00:00:00",
        "2023-11-15T00:00:00",
        "2023-11-16T00:00:00",
    ]
    assert payload["candles"][0]["close"] == 100.0
    # Hours are stored but the `token` row is not: metadata is empty rather
    # than an error, which is what the GraphQL resolver does too.
    assert payload["metadata"] == {}


async def test_an_unknown_symbol_comes_back_as_an_error_not_a_crash(
    seeded: DatabaseManager, tokens_path: Path
) -> None:
    async for session in _session(seeded.dsn, tokens_path):
        result = await session.call_tool("candles", {"symbol": "DOGE"})

    assert result.is_error
    assert "unknown token" in result.content[0].text  # type: ignore[union-attr]


async def test_the_design_documents_are_readable_over_the_protocol(
    seeded: DatabaseManager, tokens_path: Path
) -> None:
    async for session in _session(seeded.dsn, tokens_path):
        listed = {str(resource.uri) for resource in (await session.list_resources()).resources}
        assert "candlegraph://docs/knowledge/mcp" in listed

        contents = await session.read_resource("candlegraph://docs/roadmap")  # type: ignore[arg-type]

    text = contents.contents[0].text  # type: ignore[union-attr]
    assert text.startswith("# Roadmap")
