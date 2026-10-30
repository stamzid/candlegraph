"""The knowledge schema (rollout step 2) and the asset sync (step 3).

What is pinned here is the part the unit tier cannot see: the `vector`
extension really installs on the pinned image, the eight tables exist as
plain tables with their indexes, the revision round-trips on a populated
database, and `sync-assets` is idempotent against a real `asset` table.
"""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config

from candlegraph.knowledge.assets import sync_assets
from candlegraph.knowledge.statements import knowledge_statements_for
from candlegraph.storage import DatabaseManager
from candlegraph.tokens import TokenRegistry, load_tokens
from tests.conftest import TOKENS_YAML, WBTC
from tests.integration.conftest import _env
from tests.integration.test_timescale import (
    BASE,
    HOUR,
    KNOWLEDGE_TABLES,
    hour_record,
    hypertable_names,
    write_hours,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def kdb(db: DatabaseManager) -> DatabaseManager:
    """The migrated database with the knowledge tables emptied too."""
    db.execute(f'TRUNCATE "{db.schema}".asset, "{db.schema}".source CASCADE')
    return db


def registry_from(tmp_path_factory: pytest.TempPathFactory, body: str) -> TokenRegistry:
    path = tmp_path_factory.mktemp("tokens") / "tokens.yaml"
    path.write_text(body, encoding="utf-8")
    return load_tokens(path)


# --- the schema --------------------------------------------------------------


def test_the_vector_extension_is_installed(db: DatabaseManager) -> None:
    """Step 2's migration runs CREATE EXTENSION vector; the image must carry it."""
    rows = db.fetch_all("SELECT extversion FROM pg_extension WHERE extname = 'vector'")

    assert rows, "pgvector must be installed by the knowledge migration"


def test_every_knowledge_table_exists_and_is_plain(db: DatabaseManager) -> None:
    rows = db.fetch_all(
        "SELECT tablename FROM pg_tables WHERE schemaname = %(schema)s", {"schema": db.schema}
    )
    tables = {row["tablename"] for row in rows}

    assert tables >= KNOWLEDGE_TABLES
    assert KNOWLEDGE_TABLES.isdisjoint(hypertable_names(db))


def test_chunk_carries_the_hnsw_and_gin_indexes(db: DatabaseManager) -> None:
    """Hybrid retrieval needs both halves: ANN over halfvec, GIN over tsvector."""
    rows = db.fetch_all(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %(schema)s AND tablename = 'chunk'",
        {"schema": db.schema},
    )
    definitions = {row["indexname"]: row["indexdef"] for row in rows}

    assert "hnsw" in definitions["ix_chunk_embedding_hnsw"]
    assert "halfvec_cosine_ops" in definitions["ix_chunk_embedding_hnsw"]
    assert "gin" in definitions["ix_chunk_tsv"]


def test_a_document_url_hash_is_unique(db: DatabaseManager) -> None:
    """The idempotency key for text: one normalised URL, one row."""
    import psycopg2

    db.execute(
        f"INSERT INTO \"{db.schema}\".source (id, kind) VALUES ('manual', 'file') ON CONFLICT (id) DO NOTHING"
    )
    insert = (
        f'INSERT INTO "{db.schema}".document (source_id, url, url_hash, retrieved_at) '
        "VALUES ('manual', %(url)s, %(url_hash)s, now())"
    )
    db.execute(insert, {"url": "https://example.com/a", "url_hash": "h1"})

    with pytest.raises(psycopg2.errors.UniqueViolation):
        db.execute(insert, {"url": "https://example.com/a?utm_source=x", "url_hash": "h1"})


def test_the_knowledge_migration_round_trips_on_a_populated_database(
    kdb: DatabaseManager, migrated_dsn: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """`downgrade -1` removes only the knowledge tables; the data below survives."""
    write_hours(kdb, [hour_record(BASE), hour_record(BASE + HOUR)])
    sync_assets(kdb, registry_from(tmp_path_factory, TOKENS_YAML))
    config = Config("alembic.ini")

    with _env(DATABASE_URL=migrated_dsn, DB_SCHEMA=kdb.schema):
        command.downgrade(config, "-1")

        rows = kdb.fetch_all(
            "SELECT tablename FROM pg_tables WHERE schemaname = %(schema)s", {"schema": kdb.schema}
        )
        assert KNOWLEDGE_TABLES.isdisjoint({row["tablename"] for row in rows})
        counted = kdb.fetch_all(f'SELECT COUNT(*) AS n FROM "{kdb.schema}".token_hours_data')
        assert int(counted[0]["n"]) == 2, "the downgrade must not touch market data"

        command.upgrade(config, "head")

    # The rebuilt tables accept writes again.
    assert sync_assets(kdb, registry_from(tmp_path_factory, TOKENS_YAML)) == 2


# --- the asset sync -----------------------------------------------------------


def test_sync_assets_is_idempotent(kdb: DatabaseManager, tmp_path_factory: pytest.TempPathFactory) -> None:
    """The second run changes nothing: same rows, same aliases."""
    registry = registry_from(tmp_path_factory, TOKENS_YAML)

    statements = knowledge_statements_for(kdb.schema)

    assert sync_assets(kdb, registry) == 2
    first = kdb.fetch_all(statements.get_assets)
    assert sync_assets(kdb, registry) == 2
    second = kdb.fetch_all(statements.get_assets)

    assert first == second
    assert {row["id"] for row in first} == {WBTC, "0x95ad61b0a150d79219dcf64e1e6cc01f0b64c4ce"}


def test_a_token_without_aliases_gets_symbol_and_name_as_the_default_set(
    kdb: DatabaseManager, tmp_path_factory: pytest.TempPathFactory
) -> None:
    sync_assets(kdb, registry_from(tmp_path_factory, TOKENS_YAML))

    rows = kdb.fetch_all(f'SELECT aliases, kind FROM "{kdb.schema}".asset WHERE id = %(id)s', {"id": WBTC})

    assert rows[0]["aliases"] == ["WBTC", "Wrapped BTC"]
    assert rows[0]["kind"] == "token"


def test_configured_aliases_replace_the_default_set(
    kdb: DatabaseManager, tmp_path_factory: pytest.TempPathFactory
) -> None:
    body = (
        f'tokens:\n  - address: "{WBTC}"\n    symbol: WBTC\n    name: Wrapped BTC\n'
        "    aliases: [WBTC, Bitcoin]\n"
    )
    sync_assets(kdb, registry_from(tmp_path_factory, body))

    rows = kdb.fetch_all(f'SELECT aliases FROM "{kdb.schema}".asset WHERE id = %(id)s', {"id": WBTC})

    assert rows[0]["aliases"] == ["WBTC", "Bitcoin"]
