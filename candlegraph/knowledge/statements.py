"""Every SQL statement for the knowledge tables, built per schema.

The same pattern as :mod:`candlegraph.storage.statements`, kept separate so
the knowledge layer's whole database surface is readable in one file. The set
grows with the rollout steps in ``docs/rollout/ROLLOUT.md``; a statement lands
here in the step that first executes it.

Nothing in this module deletes market data, and nothing here is written by an
agent: the observer only reads these tables (invariant 21).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from psycopg2 import sql

# --- asset -------------------------------------------------------------------

ASSET_COLUMNS = ("id", "symbol", "kind", "aliases")

_ASSET_TEMPLATE = "(%(id)s,%(symbol)s,%(kind)s,%(aliases)s)"


def _excluded(names: tuple[str, ...], *, key: tuple[str, ...]) -> str:
    return ",\n            ".join(f"{n} = EXCLUDED.{n}" for n in names if n not in key)


@dataclass(frozen=True)
class KnowledgeStatements:
    """The knowledge layer's SQL, bound to a schema name."""

    upsert_asset: sql.Composed
    upsert_asset_template: str
    get_assets: sql.Composed


@lru_cache(maxsize=4)
def knowledge_statements_for(schema: str) -> KnowledgeStatements:
    """SQL for ``schema``, built once per schema name."""
    ns = sql.Identifier(schema)

    # `id` is the token address, so syncing twice refreshes rather than
    # duplicates — the same idempotency shape as every other upsert here.
    upsert_asset = sql.SQL(
        """
        INSERT INTO {ns}.asset ({cols})
        VALUES %s
        ON CONFLICT (id) DO UPDATE SET
            {updates}
        RETURNING id
        """
    ).format(
        ns=ns,
        cols=sql.SQL(", ".join(ASSET_COLUMNS)),
        updates=sql.SQL(_excluded(ASSET_COLUMNS, key=("id",))),
    )

    get_assets = sql.SQL("SELECT * FROM {ns}.asset ORDER BY symbol").format(ns=ns)

    return KnowledgeStatements(
        upsert_asset=upsert_asset,
        upsert_asset_template=_ASSET_TEMPLATE,
        get_assets=get_assets,
    )
