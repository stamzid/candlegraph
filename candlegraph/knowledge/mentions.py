"""Entity linking by alias: which assets does a chunk talk about?

The simplest linker that is also the most trustworthy one. An alias from
``config/tokens.yaml`` ("Ether", "WBTC", "Wrapped Bitcoin") appearing as a
whole word in a chunk is a mention with ``method = 'alias'`` and confidence
1.0 — exact and cheap, exactly as ``docs/knowledge/graph/DESIGN.md`` starts.
Named-entity extraction is a later addition that the ``method`` column
leaves room for; nothing here is asserted by a model.

Matching is pure (:func:`find_mentions`) and tested as such; the one write
(:func:`link_mentions`) upserts rows the retrieval filter joins on.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from candlegraph.knowledge.assets import default_aliases
from candlegraph.knowledge.statements import knowledge_statements_for
from candlegraph.storage import DatabaseManager
from candlegraph.tokens import TokenRegistry

ALIAS_METHOD = "alias"
ALIAS_CONFIDENCE = 1.0


def alias_dictionary(registry: TokenRegistry) -> dict[str, tuple[str, ...]]:
    """``asset id -> aliases`` from the registry, defaults filled in."""
    return {token.address: token.aliases or default_aliases(token) for token in registry}


def _pattern(aliases: Sequence[str]) -> re.Pattern[str] | None:
    """One case-insensitive, whole-word pattern for a set of aliases.

    Word boundaries keep "ETH" out of "METHOD" and "UNI" out of "UNIVERSITY";
    the longest alias is tried first so "Wrapped Ether" is one hit, not two.
    """
    cleaned = sorted({a.strip() for a in aliases if a and a.strip()}, key=len, reverse=True)
    if not cleaned:
        return None
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(a) for a in cleaned) + r")(?!\w)", re.IGNORECASE)


def find_mentions(text: str, aliases_by_asset: Mapping[str, Sequence[str]]) -> set[str]:
    """The asset ids whose aliases appear as whole words in ``text``. Pure."""
    found: set[str] = set()
    for asset_id, aliases in aliases_by_asset.items():
        pattern = _pattern(aliases)
        if pattern is not None and pattern.search(text):
            found.add(asset_id)
    return found


def mention_rows(
    chunks: Sequence[Mapping[str, Any]], aliases_by_asset: Mapping[str, Sequence[str]]
) -> list[dict[str, Any]]:
    """``mention`` rows for the given ``(id, text)`` chunk rows. Pure."""
    rows: list[dict[str, Any]] = []
    for chunk in chunks:
        for asset_id in sorted(find_mentions(str(chunk["text"]), aliases_by_asset)):
            rows.append(
                {
                    "chunk_id": int(chunk["id"]),
                    "asset_id": asset_id,
                    "method": ALIAS_METHOD,
                    "confidence": ALIAS_CONFIDENCE,
                }
            )
    return rows


def link_mentions(
    db: DatabaseManager, document_id: int, aliases_by_asset: Mapping[str, Sequence[str]]
) -> int:
    """Link one stored document's chunks to the assets they name.

    Idempotent: the primary key is ``(chunk_id, asset_id, method)`` and a
    repeat is a no-op. Returns the number of *new* mention rows.
    """
    statements = knowledge_statements_for(db.schema)
    chunks = db.fetch_all(statements.get_chunks, {"document_id": document_id})
    rows = mention_rows(chunks, aliases_by_asset)
    if not rows:
        return 0
    return db.upsert_many(statements.upsert_mentions, statements.upsert_mentions_template, rows)


def link_unlinked_documents(
    db: DatabaseManager, aliases_by_asset: Mapping[str, Sequence[str]], limit: int = 500
) -> tuple[int, int]:
    """Backfill: link every document that has chunks but no mention yet.

    Returns ``(documents linked, mention rows written)``. A document whose
    chunks name no tracked asset is counted as linked and will be picked up
    again next time — that is cheap and avoids a "checked" flag.
    """
    statements = knowledge_statements_for(db.schema)
    rows = db.fetch_all(statements.get_documents_without_mentions, {"limit": limit})
    written = 0
    for row in rows:
        written += link_mentions(db, int(row["id"]), aliases_by_asset)
    return len(rows), written
