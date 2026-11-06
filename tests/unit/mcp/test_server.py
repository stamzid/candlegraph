"""The listing a client sees: names, schemas, annotations, resources, prompts.

These exercise the SDK's schema generation without a transport, which is
where a wrapper or a bad annotation would quietly break the surface. The
stdio round trip is the integration test.

Skipped when the MCP extra is not installed, so ``make test-unit`` still
works on a core install — the same rule the UI tier follows.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from candlegraph.mcp import tools
from candlegraph.settings import REPO_ROOT
from candlegraph.storage import set_db

pytest.importorskip("mcp", reason="the MCP extra is not installed")

from candlegraph.mcp.server import build_server

# The table in docs/knowledge/mcp/DESIGN.md, "now" and "stage 1" rows.
# `run_pipeline` is stage 2 and deliberately absent.
EXPECTED_TOOLS = {
    "status",
    "recent_ingest_runs",
    "data_quality_report",
    "candles",
    "stats",
    "compare",
    "signals",
    "backtest",
    "agent_runs",
    "latest_agent_report",
    "observe",
    "explain_signal",
    "search_knowledge",
    "knowledge_budget",
}


@pytest.fixture
def server(tokens_file: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    return build_server()


async def test_the_listing_is_the_tool_table_from_the_design(server) -> None:  # type: ignore[no-untyped-def]
    listed = await server.list_tools()

    assert {tool.name for tool in listed} == EXPECTED_TOOLS
    assert {spec.name for spec in tools.TOOLS} == EXPECTED_TOOLS


async def test_only_the_two_documented_writers_are_not_read_only(server) -> None:  # type: ignore[no-untyped-def]
    """The exit criterion's last clause: no listed tool writes market data."""
    listed = await server.list_tools()

    writers = {tool.name for tool in listed if not (tool.annotations and tool.annotations.read_only_hint)}
    assert writers == {"observe", "explain_signal"}
    assert all(tool.annotations is not None and tool.annotations.destructive_hint is False for tool in listed)


async def test_the_tool_that_can_spend_money_says_so_in_its_description(server) -> None:  # type: ignore[no-untyped-def]
    listed = {tool.name: tool for tool in await server.list_tools()}

    def flat(name: str) -> str:
        return " ".join((listed[name].description or "").split())

    assert "one metered web search per token per UTC day" in flat("explain_signal")
    assert "Default is false" in flat("explain_signal")
    # And the one that cannot spend, says it cannot.
    assert "Never calls the web" in flat("search_knowledge")


async def test_every_tool_carries_a_description_and_a_usable_schema(server) -> None:  # type: ignore[no-untyped-def]
    for tool in await server.list_tools():
        assert tool.description, f"{tool.name} has no description for a client to read"
        schema = tool.input_schema
        assert schema["type"] == "object"
        # The wrapper must not leak into the schema: a tool takes no db.
        assert "db" not in schema.get("properties", {})
        assert "kwargs" not in schema.get("properties", {})


async def test_the_argument_schema_survives_the_redaction_wrapper(server) -> None:  # type: ignore[no-untyped-def]
    """``functools.wraps`` keeps ``__wrapped__``, so the real signature is read."""
    listed = {tool.name: tool for tool in await server.list_tools()}

    schema = listed["candles"].input_schema
    assert set(schema["properties"]) == {"symbol", "hours", "start", "end"}
    assert schema["required"] == ["symbol"]
    assert schema["properties"]["hours"]["default"] == 1


async def test_the_design_documents_are_served_as_resources(server) -> None:  # type: ignore[no-untyped-def]
    listed = {str(resource.uri): resource for resource in await server.list_resources()}

    assert "candlegraph://docs/roadmap" in listed
    assert "candlegraph://docs/knowledge/mcp" in listed
    assert "candlegraph://incidents/2026-10-28-stack-would-not-start" in listed
    assert listed["candlegraph://docs/roadmap"].mime_type == "text/markdown"


async def test_a_resource_reads_the_document_off_disk(server) -> None:  # type: ignore[no-untyped-def]
    contents = await server.read_resource("candlegraph://docs/knowledge/mcp")

    text = "".join(part.content for part in contents)  # type: ignore[attr-defined]
    assert text == (REPO_ROOT / "docs" / "knowledge" / "mcp" / "DESIGN.md").read_text(encoding="utf-8")


async def test_the_prompts_package_the_reading_order_the_skills_describe(server) -> None:  # type: ignore[no-untyped-def]
    names = {prompt.name for prompt in await server.list_prompts()}

    assert names == {"judge_coverage", "triage_stack"}
    rendered = await server.get_prompt("judge_coverage", {"symbol": "GNO"})
    text = str(rendered.messages[0].content)
    assert "GNO" in text
    assert "data_quality_report" in text


def test_building_the_server_opens_no_database_connection(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 1, at the surface a client touches first."""
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    set_db(None)

    assert build_server() is not None


def test_a_checkout_without_documents_still_serves_the_tools(
    tokens_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))

    assert build_server(docs_root=tmp_path / "absent") is not None
