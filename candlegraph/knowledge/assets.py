"""``config/tokens.yaml`` aliases -> ``asset`` rows. Pure transform + one upsert.

The ``asset`` table is the entity-linking dictionary: a document says "Ether"
and the mention links to the WETH address because the alias list says so.
Aliases are config, maintained next to the symbol in ``tokens.yaml``, and this
module is the only thing that writes them into the database — syncing is an
idempotent upsert on the address, so running it twice changes nothing.
"""

from __future__ import annotations

import json
from typing import Any

from candlegraph.knowledge.statements import knowledge_statements_for
from candlegraph.storage import DatabaseManager
from candlegraph.tokens import Token, TokenRegistry


def default_aliases(token: Token) -> tuple[str, ...]:
    """The alias set a token gets when the config lists none: symbol and name.

    A token with no aliases still has to be findable in text, and its symbol
    and name are the two strings a document is most likely to use.
    """
    names = [token.symbol]
    if token.name and token.name.casefold() != token.symbol.casefold():
        names.append(token.name)
    return tuple(names)


def asset_rows(registry: TokenRegistry) -> list[dict[str, Any]]:
    """The ``asset`` rows a registry maps to. Pure; aliases serialised as JSON."""
    rows: list[dict[str, Any]] = []
    for token in registry:
        aliases = token.aliases or default_aliases(token)
        rows.append(
            {
                "id": token.address,
                "symbol": token.symbol,
                "kind": "token",
                "aliases": json.dumps(list(aliases)),
            }
        )
    return rows


def sync_assets(db: DatabaseManager, registry: TokenRegistry) -> int:
    """Upsert one ``asset`` row per configured token; returns rows written."""
    statements = knowledge_statements_for(db.schema)
    rows = asset_rows(registry)
    written = db.upsert_many(statements.upsert_asset, statements.upsert_asset_template, rows)
    return int(written)
