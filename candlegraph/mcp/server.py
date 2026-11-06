"""The MCP server: registration and nothing else.

``candlegraph mcp`` starts this over stdio, which is how Claude Code (or any
other client) reaches the tools in :mod:`candlegraph.mcp.tools`. The whole
file is a list of things to register — every behaviour lives in the module
being registered, so the only way this file can be wrong is by registering
the wrong thing, which the tests check against the table in
``docs/knowledge/mcp/DESIGN.md``.

This is the one module that imports ``mcp``, and it is an optional extra
(``uv sync --extra mcp``). Nothing else in the package imports this module
at import time, so a core install is unaffected.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from candlegraph.mcp import tools
from candlegraph.mcp.resources import DocumentResource, document_resources
from candlegraph.settings import REPO_ROOT
from candlegraph.utils.logging_utils import service_logger

if TYPE_CHECKING:  # pragma: no cover - import-time cost only when serving
    from mcp.server.mcpserver import MCPServer

Transport = Literal["stdio", "http"]

SERVER_NAME = "candlegraph"
INSTRUCTIONS = """\
candlegraph: hourly Uniswap v3 market data, deterministic analytics, and a
news corpus a signal can cite. These tools read the same functions the
`candlegraph` CLI calls, so what you see here is what an operator sees.

Two things to hold on to when reading results:

- A missing hour is not a fault. Uniswap emits no row for an hour with no
  swaps, so a thin token is legitimately sparse; judge coverage against the
  configured threshold, not by the presence of a gap.
- A losing backtest is usually the honest answer. Costs are charged on every
  position change and execution lags one bar; compare with buy-and-hold
  before reading a number as edge.

Every tool is read-only except `observe` (writes one agent-run ledger row)
and `explain_signal` (writes an explanation, and may spend one metered web
search credit per token per UTC day when `search` is true).
"""

MISSING_EXTRA = (
    "the MCP server needs the `mcp` extra. Install it with `uv sync --extra mcp`, or run it with `make mcp`."
)


def _mcp_server_class() -> type[MCPServer[Any]]:
    """Import the SDK, or explain how to install it. The ``ui`` command's pattern."""
    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError as exc:  # pragma: no cover - exercised by hand, not in CI
        raise SystemExit(MISSING_EXTRA) from exc
    return MCPServer


def build_server(docs_root: Path | None = None) -> MCPServer[Any]:
    """The configured server: every tool, the documents, and two prompts.

    Args:
        docs_root: Where the design documents live. Defaults to this
            checkout's ``docs/``; an installation without them simply
            serves no resources.

    Returns:
        An ``MCPServer`` ready to ``run()``. Building it opens no
        connection — the tools resolve the database on first call
        (invariant 1).
    """
    from mcp.types import ToolAnnotations

    server = _mcp_server_class()(name=SERVER_NAME, instructions=INSTRUCTIONS, version=_version())

    for spec in tools.TOOLS:
        server.add_tool(
            _protocol_errors(spec.run),
            name=spec.name,
            annotations=ToolAnnotations(
                read_only_hint=spec.read_only,
                # Nothing here deletes or overwrites market data; the two
                # writers upsert their own rows, which re-running repeats.
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=spec.name == "explain_signal",
            ),
        )

    for resource in document_resources(docs_root or REPO_ROOT / "docs"):
        _add_document(server, resource)

    _add_prompts(server)
    return server


def _protocol_errors(run: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Re-raise a tool's refusal as the SDK's anticipated-failure type.

    The SDK distinguishes an error the tool saw coming — the client gets the
    message — from a crash, where it returns "Error executing tool X" and
    withholds the text. An unknown symbol is the first kind, and
    :mod:`candlegraph.mcp.tools` has already redacted the message by the
    time it arrives here. Translating at this layer is what keeps the tools
    free of any ``mcp`` import.
    """
    from mcp.server.mcpserver.exceptions import ToolError

    @functools.wraps(run)
    def translated(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return run(*args, **kwargs)
        except tools.ToolError as exc:
            raise ToolError(str(exc)) from exc

    return translated


def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("candlegraph")
    except PackageNotFoundError:  # pragma: no cover - only outside an install
        return "0"


def _add_document(server: MCPServer[Any], resource: DocumentResource) -> None:
    """Serve one Markdown file at its URI.

    The reader is bound to this one path, so there is no parameter to
    traverse with and no way to address a file outside ``docs/``.
    """
    path = resource.path

    def read() -> str:
        return path.read_text(encoding="utf-8")

    from mcp.server.mcpserver.resources import FunctionResource

    server.add_resource(
        FunctionResource.from_function(
            read,
            uri=resource.uri,
            name=resource.uri.removeprefix("candlegraph://"),
            title=resource.title,
            description=resource.description,
            mime_type="text/markdown",
        )
    )


def _add_prompts(server: MCPServer[Any]) -> None:
    """The reading orders the skills describe, packaged as prompts.

    Both are copies of judgement written down elsewhere in the repository
    (``.claude/skills/run-stack``, ``run-agents``, ``backfill-and-verify``);
    they are here so a client can follow it without the skill files.
    """

    @server.prompt(name="judge_coverage", title="Judge a coverage or gap finding")
    def judge_coverage(symbol: str) -> str:
        """Decide whether a token's low coverage is a fault or just a thin market."""
        return (
            f"Decide whether {symbol}'s coverage is a real problem.\n"
            f"1. Call data_quality_report() and read {symbol}'s coverage, gap count and longest gap.\n"
            f"2. Call candles(symbol='{symbol}', hours=1) and work out its average hourly volume. "
            "Uniswap v3 emits no row for an hour with no swaps, so a thin token is legitimately "
            "sparse and low coverage there is the market, not a bug.\n"
            "3. Call recent_ingest_runs() — a failed or partial run at the time of the gap is the "
            "fault; a clean run through a quiet hour is not.\n"
            "4. Say which of the two it is, and name the evidence. Do not propose lowering "
            "MIN_EXPECTED_COVERAGE to make a finding go away."
        )

    @server.prompt(name="triage_stack", title="Work out what state the stack is in")
    def triage_stack() -> str:
        """The order to read things in when something looks wrong."""
        return (
            "Work out what state candlegraph is in, in this order, and stop as soon as the "
            "answer is clear:\n"
            "1. status() — is the database reachable and the data fresh?\n"
            "2. recent_ingest_runs() — did the recent passes succeed? `partial` means some "
            "tokens failed and some did not.\n"
            "3. data_quality_report() — coverage per token and impossible rows. Judge a gap "
            "with the judge_coverage prompt rather than by eye.\n"
            "4. latest_agent_report('observer') — what the observer already concluded, and "
            "when. If it disagrees with the three reads above, trust the reads and say so.\n"
            "Report what is wrong and what you checked. Suggest a fix only if the evidence "
            "names one."
        )

    service_logger.debug("Registered prompts: %s, %s", judge_coverage.__name__, triage_stack.__name__)


def run_server(
    transport: Transport = "stdio", host: str = "127.0.0.1", port: int = 8900
) -> None:  # pragma: no cover - a blocking server loop
    """Serve until stopped.

    ``stdio`` is the local case: the client launches this process and talks
    over its pipes, which is how Claude Code reaches it. ``http`` serves the
    same tools over streamable HTTP for a client in another container.
    """
    server = build_server()
    if transport == "stdio":
        # Nothing may be printed to stdout: it is the protocol channel.
        server.run(transport="stdio")
        return
    service_logger.info("Serving candlegraph MCP over streamable HTTP on %s:%s", host, port)
    server.run(transport="streamable-http", host=host, port=port)
