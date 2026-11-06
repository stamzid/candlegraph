"""Design documents and post-mortems, addressed by URI.

The mapping is meant to be guessable from the directory layout, so these
tests state the layout rather than a list of names: a document added to
``docs/`` must be served without anyone editing the server.
"""

from __future__ import annotations

from pathlib import Path

from candlegraph.mcp.resources import document_resources, title_and_status, uri_for
from candlegraph.settings import REPO_ROOT


def test_a_design_document_is_addressed_by_its_directory() -> None:
    assert uri_for(Path("roadmap/DESIGN.md")) == "candlegraph://docs/roadmap"
    assert uri_for(Path("knowledge/graph/DESIGN.md")) == "candlegraph://docs/knowledge/graph"
    assert uri_for(Path("rollout/ROLLOUT.md")) == "candlegraph://docs/rollout"


def test_a_post_mortem_is_addressed_by_its_slug_under_incidents() -> None:
    relative = Path("incidents/2026-10-28-stack-would-not-start/POSTMORTEM.md")

    assert uri_for(relative) == "candlegraph://incidents/2026-10-28-stack-would-not-start"


def test_anything_that_is_not_a_document_has_no_uri() -> None:
    assert uri_for(Path("README.md")) is None
    assert uri_for(Path("knowledge/graph/notes.md")) is None
    assert uri_for(Path("DESIGN.md")) is None


def test_the_status_line_is_pulled_out_because_it_says_code_or_idea() -> None:
    text = "# MCP: exposing candlegraph as tools\n\n**Status:** proposed — 2026-10-29\nbody\n"

    assert title_and_status(text) == ("MCP: exposing candlegraph as tools", "proposed — 2026-10-29")


def test_a_document_with_no_heading_or_status_still_loads() -> None:
    assert title_and_status("just prose\n") == ("", "")


def test_a_tree_yields_one_resource_per_document_sorted_by_uri(tmp_path: Path) -> None:
    (tmp_path / "roadmap").mkdir()
    (tmp_path / "roadmap" / "DESIGN.md").write_text("# Roadmap\n\n**Status:** living\n", encoding="utf-8")
    (tmp_path / "incidents" / "2026-01-01-thing").mkdir(parents=True)
    (tmp_path / "incidents" / "2026-01-01-thing" / "POSTMORTEM.md").write_text("# Thing\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# index\n", encoding="utf-8")

    found = document_resources(tmp_path)

    assert [r.uri for r in found] == [
        "candlegraph://docs/roadmap",
        "candlegraph://incidents/2026-01-01-thing",
    ]
    assert found[0].description == "Roadmap (design document) — status: living"
    assert found[1].description == "Thing (post-mortem)"


def test_a_missing_docs_tree_serves_nothing_rather_than_raising(tmp_path: Path) -> None:
    assert document_resources(tmp_path / "nowhere") == []


def test_this_repositorys_own_documents_are_addressable() -> None:
    """The two URIs the design document names by hand must resolve."""
    uris = {resource.uri for resource in document_resources(REPO_ROOT / "docs")}

    assert "candlegraph://docs/roadmap" in uris
    assert "candlegraph://incidents/2026-10-28-stack-would-not-start" in uris
    assert "candlegraph://docs/knowledge/mcp" in uris
