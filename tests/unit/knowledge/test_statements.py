"""Knowledge statement construction, with no database involved.

The offline half; `tests/integration/test_knowledge.py` executes them for real.
"""

from __future__ import annotations

from candlegraph.knowledge.statements import knowledge_statements_for
from tests.unit.storage.test_statements import render


def test_statements_are_bound_to_the_requested_schema() -> None:
    statements = knowledge_statements_for("other_schema")

    assert '"other_schema".asset' in render(statements.upsert_asset)
    assert '"other_schema".asset' in render(statements.get_assets)


def test_statements_are_cached_per_schema() -> None:
    assert knowledge_statements_for("candlegraph") is knowledge_statements_for("candlegraph")


def test_the_asset_upsert_conflicts_on_the_address_and_updates_the_rest() -> None:
    """Syncing twice must refresh, not duplicate or fail."""
    text = render(knowledge_statements_for("candlegraph").upsert_asset)

    assert "ON CONFLICT (id) DO UPDATE" in text
    assert "aliases = EXCLUDED.aliases" in text
    assert "id = EXCLUDED.id" not in text


def test_nothing_in_the_knowledge_statements_deletes() -> None:
    statements = knowledge_statements_for("candlegraph")

    for name in ("upsert_asset", "get_assets"):
        assert "DELETE" not in render(getattr(statements, name)).upper()
