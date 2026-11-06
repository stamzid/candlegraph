"""The Model Context Protocol surface: candlegraph's functions as tools.

A read-only-by-default server over the functions the CLI already calls, so
an assistant already open in the terminal can ask "what state is this thing
in" and reason over typed results instead of scraping a log. The decision,
the tool table and what this is deliberately *not* (an MCP client for
Tavily) are in ``docs/knowledge/mcp/DESIGN.md``.

Importing this package pulls in no ``mcp`` dependency: the tools and their
payloads are plain Python, and only :mod:`candlegraph.mcp.server` needs the
optional extra.
"""

from candlegraph.mcp.tools import TOOLS, ToolError, ToolSpec

__all__ = ["TOOLS", "ToolError", "ToolSpec"]
