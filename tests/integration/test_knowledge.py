"""The knowledge schema (rollout step 2) and the asset sync (step 3).

What is pinned here is the part the unit tier cannot see: the `vector`
extension really installs on the pinned image, the eight tables exist as
plain tables with their indexes, the revision round-trips on a populated
database, and `sync-assets` is idempotent against a real `asset` table.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config

from candlegraph.knowledge.assets import sync_assets
from candlegraph.knowledge.documents import ChunkConfig, RawDocument, ensure_source, store_document, url_hash
from candlegraph.knowledge.embed import HashEmbedder, embed_pending, embedding_backlog, to_vector_literal
from candlegraph.knowledge.mentions import link_mentions, link_unlinked_documents
from candlegraph.knowledge.news import fetch_news, source_spend
from candlegraph.knowledge.retrieve import RetrievalConfig, retrieve, rrf_score
from candlegraph.knowledge.sources.base import SearchResult
from candlegraph.knowledge.sources.tavily import TavilyRateLimitError
from candlegraph.knowledge.statements import knowledge_statements_for
from candlegraph.settings import Settings
from candlegraph.storage import DatabaseManager
from candlegraph.tokens import Token, TokenRegistry, load_tokens
from tests.conftest import SHIB, TOKENS_YAML, WBTC, make_settings
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
        # To just below the knowledge schema — "-1" would only undo whatever
        # sits at head (the rate_limited constraint, since step 6).
        command.downgrade(config, "e4b9c7a05f13")

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


# --- documents (step 4) -------------------------------------------------------


def _doc(text: str = "First sentence. Second sentence.", **overrides: object) -> RawDocument:
    fields: dict[str, object] = {
        "source_id": "manual",
        "url": "https://www.example.com/post?utm_source=x",
        "raw_text": text,
        "title": "A post",
    }
    fields.update(overrides)
    return RawDocument(**fields)  # type: ignore[arg-type]


def _documents(db: DatabaseManager) -> list[dict[str, object]]:
    return db.fetch_all(f'SELECT * FROM "{db.schema}".document ORDER BY id')


def _chunks(db: DatabaseManager, document_id: int) -> list[dict[str, object]]:
    return db.fetch_all(knowledge_statements_for(db.schema).get_chunks, {"document_id": document_id})


def test_upserting_the_same_document_twice_stores_it_once(kdb: DatabaseManager) -> None:
    """The step-4 exit condition: two spellings of one URL are one row."""
    ensure_source(kdb, "manual", "file")

    first = store_document(kdb, _doc(url="https://www.example.com/post?utm_source=x"))
    second = store_document(kdb, _doc(url="http://example.com/post/"))

    assert first.document_id == second.document_id
    rows = _documents(kdb)
    assert len(rows) == 1
    assert rows[0]["url_hash"] == url_hash("https://example.com/post")


def test_an_unknown_publication_date_stays_null_and_a_known_one_is_kept(kdb: DatabaseManager) -> None:
    """NULL never becomes now(); a later copy without a date does not erase a known one."""
    ensure_source(kdb, "manual", "file")
    when = datetime(2026, 10, 24, 9, 12, tzinfo=UTC)

    store_document(kdb, _doc())
    assert _documents(kdb)[0]["published_at"] is None

    store_document(kdb, _doc(published_at=when))
    assert _documents(kdb)[0]["published_at"] == when

    store_document(kdb, _doc())
    assert _documents(kdb)[0]["published_at"] == when


def test_chunks_are_written_with_stable_ordinals(kdb: DatabaseManager) -> None:
    ensure_source(kdb, "manual", "file")
    text = "\n\n".join(
        " ".join(f"Sentence {p}-{s} says something useful." for s in range(8)) for p in range(6)
    )
    config = ChunkConfig(target_tokens=60, max_tokens=80, min_tokens=40, overlap_tokens=10)

    stored = store_document(kdb, _doc(text), config)

    rows = _chunks(kdb, stored.document_id)
    assert stored.chunks_written == len(rows) > 1
    assert [row["ordinal"] for row in rows] == list(range(len(rows)))
    assert all(row["embedding"] is None for row in rows), "nothing is embedded yet (step 5)"
    assert all(row["tsv"] for row in rows), "the generated tsvector column is populated"


def test_a_shortened_document_loses_its_tail_chunks(kdb: DatabaseManager) -> None:
    ensure_source(kdb, "manual", "file")
    config = ChunkConfig(target_tokens=20, max_tokens=30, min_tokens=10, overlap_tokens=0)
    long_text = " ".join(f"Sentence number {i} is here." for i in range(20))

    first = store_document(kdb, _doc(long_text), config)
    assert first.chunks_written > 2

    second = store_document(kdb, _doc("Just one short sentence."), config)

    assert second.document_id == first.document_id
    assert second.chunks_written == 1
    assert second.chunks_removed == first.chunks_written - 1
    assert len(_chunks(kdb, first.document_id)) == 1


def test_a_chunk_whose_text_changed_loses_its_embedding(kdb: DatabaseManager) -> None:
    """A stale vector must never describe new text; unchanged chunks keep theirs."""
    ensure_source(kdb, "manual", "file")
    # Each sentence is four tokens (three words and a full stop); a six-token
    # ceiling forces one chunk per sentence.
    config = ChunkConfig(target_tokens=5, max_tokens=6, min_tokens=3, overlap_tokens=0)
    stored = store_document(kdb, _doc("Alpha beta gamma. Delta epsilon zeta."), config)
    assert stored.chunks_written == 2
    kdb.execute(
        f'UPDATE "{kdb.schema}".chunk SET embedding = %(v)s::halfvec, embedding_model = %(m)s '
        "WHERE document_id = %(id)s",
        {"v": "[" + ",".join(["0.1"] * 1024) + "]", "m": "test-model", "id": stored.document_id},
    )

    store_document(kdb, _doc("Alpha beta gamma. Delta epsilon CHANGED."), config)

    rows = _chunks(kdb, stored.document_id)
    assert rows[0]["embedding"] is not None and rows[0]["embedding_model"] == "test-model"
    assert rows[1]["embedding"] is None and rows[1]["embedding_model"] is None


def test_metadata_is_merged_and_the_title_is_not_erased_by_a_copy_without_one(kdb: DatabaseManager) -> None:
    ensure_source(kdb, "manual", "file")

    store_document(kdb, _doc(meta={"score": 0.9}))
    store_document(kdb, _doc(title=None, meta={"query": "WETH unlock"}))

    row = _documents(kdb)[0]
    assert row["title"] == "A post"
    assert row["meta"] == {"score": 0.9, "query": "WETH unlock"}


def test_a_document_for_an_unregistered_source_is_refused_not_silently_stored(kdb: DatabaseManager) -> None:
    import psycopg2

    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        store_document(kdb, _doc(source_id="nobody"))


def test_ensure_source_is_idempotent(kdb: DatabaseManager) -> None:
    ensure_source(kdb, "tavily", "search", {"depth": "basic"})
    ensure_source(kdb, "tavily", "search", {"depth": "advanced"})

    rows = kdb.fetch_all(f'SELECT * FROM "{kdb.schema}".source WHERE id = %(id)s', {"id": "tavily"})
    assert len(rows) == 1
    assert rows[0]["config"] == {"depth": "basic"}, "the first registration wins; nothing is overwritten"


# --- embeddings (step 5) -------------------------------------------------------


def _seed_corpus(kdb: DatabaseManager, sentences: int = 12) -> int:
    ensure_source(kdb, "manual", "file")
    config = ChunkConfig(target_tokens=6, max_tokens=8, min_tokens=3, overlap_tokens=0)
    text = " ".join(f"Token {i} did something notable." for i in range(sentences))
    return store_document(kdb, _doc(text), config).document_id


def test_embeddings_land_as_halfvec_1024_with_the_model_stamped(kdb: DatabaseManager) -> None:
    document_id = _seed_corpus(kdb)

    summary = embed_pending(kdb, HashEmbedder(), batch_size=5)

    rows = _chunks(kdb, document_id)
    assert summary.embedded == len(rows) > 1
    assert summary.batches == -(-len(rows) // 5)
    assert all(row["embedding_model"] == "fake-hash" for row in rows)
    dims = kdb.fetch_all(
        f'SELECT vector_dims(embedding) AS n FROM "{kdb.schema}".chunk WHERE id = %(id)s',
        {"id": rows[0]["id"]},
    )
    assert int(dims[0]["n"]) == 1024


def test_reembed_skips_rows_already_stamped_with_the_configured_model(kdb: DatabaseManager) -> None:
    """Resumable by construction: a second run finds nothing to do."""
    _seed_corpus(kdb)
    embed_pending(kdb, HashEmbedder())

    again = embed_pending(kdb, HashEmbedder())

    assert again.embedded == 0
    assert again.remaining == 0
    assert embedding_backlog(kdb, "fake-hash").pending == 0


def test_changing_the_model_puts_every_row_on_the_backlog_and_reembeds_it(kdb: DatabaseManager) -> None:
    document_id = _seed_corpus(kdb)
    embed_pending(kdb, HashEmbedder(name="fake-hash-v1"))

    backlog = embedding_backlog(kdb, "fake-hash-v2")
    assert backlog.stale == len(_chunks(kdb, document_id)) and backlog.missing == 0

    summary = embed_pending(kdb, HashEmbedder(name="fake-hash-v2"))

    assert summary.embedded == backlog.stale
    assert {row["embedding_model"] for row in _chunks(kdb, document_id)} == {"fake-hash-v2"}


def test_a_chunk_whose_text_changed_rejoins_the_backlog(kdb: DatabaseManager) -> None:
    ensure_source(kdb, "manual", "file")
    config = ChunkConfig(target_tokens=5, max_tokens=6, min_tokens=3, overlap_tokens=0)
    store_document(kdb, _doc("Alpha beta gamma. Delta epsilon zeta."), config)
    embed_pending(kdb, HashEmbedder())
    assert embedding_backlog(kdb, "fake-hash").pending == 0

    store_document(kdb, _doc("Alpha beta gamma. Delta epsilon CHANGED."), config)

    assert embedding_backlog(kdb, "fake-hash").missing == 1


def test_a_similarity_query_uses_the_hnsw_index_and_finds_the_chunk_itself(kdb: DatabaseManager) -> None:
    """The reason `chunk` is a plain table: one global ANN index, and the planner picks it."""
    document_id = _seed_corpus(kdb, sentences=20)
    embedder = HashEmbedder()
    embed_pending(kdb, embedder)
    rows = _chunks(kdb, document_id)
    target = rows[3]
    query = to_vector_literal(embedder.embed([str(target["text"])], kind="query")[0])
    statements = knowledge_statements_for(kdb.schema)

    nearest = kdb.fetch_all(statements.similar_chunks, {"query": query, "model": "fake-hash", "limit": 3})

    assert nearest[0]["id"] == target["id"]
    assert float(nearest[0]["distance"]) == pytest.approx(0.0, abs=1e-3)

    # A tiny table seq-scans by cost; turn that off (for this transaction
    # only) to see that the index answers the ORDER BY <=> ... LIMIT shape.
    plan = kdb.fetch_all(
        f"""
        SET LOCAL enable_seqscan = off;
        EXPLAIN SELECT id FROM "{kdb.schema}".chunk
        WHERE embedding_model = 'fake-hash'
        ORDER BY embedding <=> '{query}'::halfvec
        LIMIT 3
        """
    )
    text = "\n".join(str(row["QUERY PLAN"]) for row in plan)
    assert "ix_chunk_embedding_hnsw" in text, text


# --- the source_call ledger (step 6) --------------------------------------------


class OneShotSource:
    """A Source that returns one scripted answer and counts its calls."""

    def __init__(self, action: object) -> None:
        self.action = action
        self.calls = 0

    id = "tavily"
    kind = "search"
    cost = 1

    def search(self, query: str, *, start: datetime, end: datetime, max_results: int) -> SearchResult:
        self.calls += 1
        if isinstance(self.action, Exception):
            raise self.action
        assert isinstance(self.action, SearchResult)
        return self.action


NEWS_NOW = datetime(2026, 11, 1, 14, 0, tzinfo=UTC)
WBTC_TOKEN = Token(address=WBTC, symbol="WBTC", name="Wrapped BTC", aliases=("WBTC", "Bitcoin"))


def news_settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "tavily_monthly_credits": 100,
        "tavily_daily_credits": 10,
        "tavily_reserve_credits": 5,
    }
    defaults.update(overrides)
    return make_settings(**defaults)


def _calls(kdb: DatabaseManager) -> list[dict[str, object]]:
    return kdb.fetch_all(f'SELECT * FROM "{kdb.schema}".source_call ORDER BY id')


def test_a_successful_fetch_lands_documents_and_one_ok_ledger_row(kdb: DatabaseManager) -> None:
    raw = RawDocument(source_id="tavily", url="https://x.com/story", raw_text="One. Two.", title="T")
    source = OneShotSource(SearchResult([raw], credits=1, results=2, skipped=1, params={"q": "x"}))

    summary = fetch_news(kdb, news_settings(), source, [WBTC_TOKEN], now=NEWS_NOW)

    assert summary.outcomes[0].status == "ok"
    (row,) = _calls(kdb)
    assert row["status"] == "ok" and row["credits"] == 1 and row["results"] == 2
    assert row["params"]["cache_key"] == "WBTC:2026-11-01"
    assert len(_documents(kdb)) == 1


def test_one_search_per_asset_and_day_survives_processes_via_the_ledger(kdb: DatabaseManager) -> None:
    """The cache is in the database: a second run — any process — finds the first."""
    source = OneShotSource(SearchResult([], credits=1, results=0))

    fetch_news(kdb, news_settings(), source, [WBTC_TOKEN], now=NEWS_NOW)
    second = fetch_news(kdb, news_settings(), source, [WBTC_TOKEN], now=NEWS_NOW.replace(hour=22))

    assert source.calls == 1
    assert second.outcomes[0].status == "cached"
    assert len(_calls(kdb)) == 1


def test_a_refusal_lands_as_budget_refused_with_zero_credits(kdb: DatabaseManager) -> None:
    """The step-6 exit condition, against the real schema."""
    source = OneShotSource(SearchResult([], credits=1, results=0))
    exhausted = news_settings(tavily_monthly_credits=10, tavily_daily_credits=10, tavily_reserve_credits=0)
    ensure_source(kdb, "tavily", "search")
    kdb.execute(
        f'INSERT INTO "{kdb.schema}".source_call (source_id, called_at, credits, status) '
        "VALUES ('tavily', %(at)s, 10, 'ok')",
        {"at": NEWS_NOW.replace(day=1, hour=0)},
    )

    summary = fetch_news(kdb, exhausted, source, [WBTC_TOKEN], now=NEWS_NOW)

    assert source.calls == 0
    assert summary.outcomes[0].status == "budget_refused"
    refusal = _calls(kdb)[-1]
    assert refusal["status"] == "budget_refused"
    assert refusal["credits"] == 0
    assert "monthly" in str(refusal["error"])


def test_the_widened_constraint_accepts_a_rate_limited_row(kdb: DatabaseManager) -> None:
    source = OneShotSource(TavilyRateLimitError("Tavily returned HTTP 429: rate or plan limit"))

    summary = fetch_news(kdb, news_settings(), source, [WBTC_TOKEN], now=NEWS_NOW)

    assert summary.outcomes[0].status == "rate_limited"
    (row,) = _calls(kdb)
    assert row["status"] == "rate_limited"
    assert "429" in str(row["error"])


def test_spend_is_summed_from_the_ledger_in_utc_month_and_day(kdb: DatabaseManager) -> None:
    ensure_source(kdb, "tavily", "search")
    insert = (
        f'INSERT INTO "{kdb.schema}".source_call (source_id, called_at, credits, status) '
        "VALUES ('tavily', %(at)s, %(credits)s, 'ok')"
    )
    kdb.execute(insert, {"at": datetime(2026, 10, 31, 23, 0, tzinfo=UTC), "credits": 7})  # last month
    kdb.execute(insert, {"at": datetime(2026, 11, 1, 1, 0, tzinfo=UTC), "credits": 3})  # today
    kdb.execute(insert, {"at": NEWS_NOW, "credits": 2})  # today

    month, today = source_spend(kdb, "tavily", NEWS_NOW)

    assert (month, today) == (5, 5)


def test_the_rate_limited_migration_round_trips_folding_rows_into_error(
    kdb: DatabaseManager, migrated_dsn: str
) -> None:
    """The downgrade must work on a populated table: rate_limited becomes error."""
    fetch_news(
        kdb, news_settings(), OneShotSource(TavilyRateLimitError("HTTP 429")), [WBTC_TOKEN], now=NEWS_NOW
    )
    config = Config("alembic.ini")

    with _env(DATABASE_URL=migrated_dsn, DB_SCHEMA=kdb.schema):
        command.downgrade(config, "f399ca0770a0")
        (row,) = _calls(kdb)
        assert row["status"] == "error", "the old constraint cannot hold rate_limited"
        command.upgrade(config, "head")

    fetch_news(
        kdb,
        news_settings(),
        OneShotSource(TavilyRateLimitError("HTTP 429")),
        [Token(address=SHIB, symbol="SHIB")],
        now=NEWS_NOW,
    )
    assert _calls(kdb)[-1]["status"] == "rate_limited"


# --- hybrid retrieval (step 7) ---------------------------------------------------


BAR = datetime(2026, 10, 24, 14, 0, tzinfo=UTC)
BEFORE = datetime(2026, 10, 24, 9, 12, tzinfo=UTC)
FETCHED = datetime(2026, 10, 24, 14, 3, tzinfo=UTC)
ALIASES = {WBTC: ("WBTC", "Bitcoin"), SHIB: ("SHIB",)}
TICKER_TEXT = "WBTC slipped as Bitcoin ETF outflows continued for a fourth day."
TRANSFER_TEXT = "A large transfer moved to an exchange overnight."


def _seed_doc(
    kdb: DatabaseManager,
    url: str,
    text: str,
    published_at: datetime | None = BEFORE,
    retrieved_at: datetime = FETCHED,
) -> int:
    ensure_source(kdb, "manual", "file")
    stored = store_document(
        kdb,
        RawDocument(
            source_id="manual",
            url=url,
            raw_text=text,
            title=url.rsplit("/", 1)[-1],
            published_at=published_at,
        ),
        retrieved_at=retrieved_at,
    )
    return stored.document_id


def _seed_news_corpus(kdb: DatabaseManager) -> dict[str, int]:
    """Two dated documents before the bar, embedded and alias-linked."""
    sync_assets(kdb, registry_from_tokens())
    ids = {
        "ticker": _seed_doc(kdb, "https://news.example/ticker", TICKER_TEXT),
        "transfer": _seed_doc(kdb, "https://news.example/transfer", TRANSFER_TEXT),
    }
    embed_pending(kdb, HashEmbedder())
    for document_id in ids.values():
        link_mentions(kdb, document_id, ALIASES)
    return ids


def registry_from_tokens() -> TokenRegistry:
    return TokenRegistry(
        tokens=(
            Token(address=WBTC, symbol="WBTC", name="Wrapped BTC", aliases=("WBTC", "Bitcoin")),
            Token(address=SHIB, symbol="SHIB", name="SHIBA INU"),
        )
    )


def _chunk_ids(kdb: DatabaseManager, document_id: int) -> set[int]:
    return {int(r["id"]) for r in _chunks(kdb, document_id)}


def test_the_lexical_leg_finds_the_ticker_the_dense_leg_misses_and_vice_versa(kdb: DatabaseManager) -> None:
    """The reason for two legs, proven on a corpus where each leg is blind to one document."""
    ids = _seed_news_corpus(kdb)

    # The dense query is the transfer sentence verbatim (distance 0 for its chunk);
    # the lexical term is the ticker the transfer sentence never says. One
    # candidate per leg makes each leg's blind spot visible; the fused list
    # is their union.
    result = retrieve(
        kdb,
        TRANSFER_TEXT,
        bar=BAR,
        embedder=HashEmbedder(),
        terms=["WBTC"],
        config=RetrievalConfig(top_k=2, candidates=1),
    )

    by_doc = {c.document_id: c for c in result.chunks}
    assert by_doc[ids["transfer"]].legs == "dense"
    assert by_doc[ids["ticker"]].legs == "lexical"
    assert result.dense_used


def test_a_chunk_both_legs_find_is_scored_by_reciprocal_rank_fusion(kdb: DatabaseManager) -> None:
    ids = _seed_news_corpus(kdb)

    result = retrieve(kdb, TICKER_TEXT, bar=BAR, embedder=HashEmbedder(), terms=["WBTC", "Bitcoin"])

    top = result.chunks[0]
    assert top.document_id == ids["ticker"]
    assert top.legs == "both"
    assert top.score == pytest.approx(rrf_score(top.dense_rank, top.lexical_rank), rel=1e-6)
    assert all(top.score > other.score for other in result.chunks[1:])


def test_appending_a_document_published_after_the_bar_does_not_change_the_bar(kdb: DatabaseManager) -> None:
    """The invariant-17 analogue and the test the whole stage leans on."""
    _seed_news_corpus(kdb)
    query = retrieve(kdb, TICKER_TEXT, bar=BAR, embedder=HashEmbedder(), terms=["WBTC"])
    before = [(c.chunk_id, round(c.score, 9)) for c in query.chunks]

    later = _seed_doc(
        kdb,
        "https://news.example/later",
        "WBTC surged after the bar on fresh Bitcoin ETF inflows.",
        published_at=datetime(2026, 10, 25, 9, 0, tzinfo=UTC),
        retrieved_at=datetime(2026, 10, 25, 9, 5, tzinfo=UTC),
    )
    embed_pending(kdb, HashEmbedder())
    link_mentions(kdb, later, ALIASES)

    after = retrieve(kdb, TICKER_TEXT, bar=BAR, embedder=HashEmbedder(), terms=["WBTC"])

    assert [(c.chunk_id, round(c.score, 9)) for c in after.chunks] == before
    assert later not in {c.document_id for c in after.chunks}
    # And the same document is visible to a bar after its publication.
    next_day = retrieve(
        kdb,
        TICKER_TEXT,
        bar=datetime(2026, 10, 25, 12, 0, tzinfo=UTC),
        embedder=HashEmbedder(),
        terms=["WBTC"],
    )
    assert later in {c.document_id for c in next_day.chunks}


def test_an_undated_document_is_excluded_by_default_and_flagged_when_asked_for(kdb: DatabaseManager) -> None:
    _seed_news_corpus(kdb)
    undated = _seed_doc(kdb, "https://news.example/undated", "WBTC rumour with no date.", published_at=None)
    embed_pending(kdb, HashEmbedder())

    strict = retrieve(kdb, "WBTC rumour", bar=BAR, embedder=HashEmbedder(), terms=["WBTC"])
    assert undated not in {c.document_id for c in strict.chunks}

    shown = retrieve(
        kdb,
        "WBTC rumour",
        bar=BAR,
        embedder=HashEmbedder(),
        terms=["WBTC"],
        config=RetrievalConfig(include_undated=True),
    )
    flagged = next(c for c in shown.chunks if c.document_id == undated)
    assert flagged.undated is True


def test_strategy_mode_refuses_text_that_was_fetched_after_the_bar(kdb: DatabaseManager) -> None:
    """Published yesterday, retrieved today: a strategy running yesterday could not have read it."""
    _seed_news_corpus(kdb)
    late_fetch = _seed_doc(
        kdb,
        "https://news.example/late-fetch",
        "WBTC commentary published early, fetched late.",
        published_at=BEFORE,
        retrieved_at=datetime(2026, 10, 25, 8, 0, tzinfo=UTC),
    )
    embed_pending(kdb, HashEmbedder())

    display = retrieve(kdb, "WBTC commentary", bar=BAR, embedder=HashEmbedder(), terms=["WBTC"])
    strategy = retrieve(
        kdb,
        "WBTC commentary",
        bar=BAR,
        embedder=HashEmbedder(),
        terms=["WBTC"],
        config=RetrievalConfig(mode="strategy"),
    )

    assert late_fetch in {c.document_id for c in display.chunks}
    assert late_fetch not in {c.document_id for c in strategy.chunks}


def test_the_asset_filter_keeps_only_chunks_the_alias_linker_tied_to_the_asset(kdb: DatabaseManager) -> None:
    ids = _seed_news_corpus(kdb)

    unfiltered = retrieve(kdb, TRANSFER_TEXT, bar=BAR, embedder=HashEmbedder(), terms=["WBTC"])
    filtered = retrieve(kdb, TRANSFER_TEXT, bar=BAR, embedder=HashEmbedder(), terms=["WBTC"], asset_id=WBTC)

    assert ids["transfer"] in {c.document_id for c in unfiltered.chunks}, "dense finds it without the filter"
    assert {c.document_id for c in filtered.chunks} == {ids["ticker"]}, (
        "it names no asset, so the filter drops it"
    )


def test_without_an_embedder_the_lexical_leg_still_answers(kdb: DatabaseManager) -> None:
    ids = _seed_news_corpus(kdb)

    result = retrieve(kdb, "outflows", bar=BAR, terms=["Bitcoin"])

    assert result.dense_used is False
    assert {c.document_id for c in result.chunks} == {ids["ticker"]}
    assert result.chunks[0].legs == "lexical"


def test_link_mentions_is_idempotent_and_the_backfill_finds_unlinked_documents(kdb: DatabaseManager) -> None:
    sync_assets(kdb, registry_from_tokens())
    document_id = _seed_doc(kdb, "https://news.example/link", TICKER_TEXT)

    documents, written = link_unlinked_documents(kdb, ALIASES)
    assert (documents, written) == (1, len(_chunk_ids(kdb, document_id)))

    assert link_mentions(kdb, document_id, ALIASES) == 0, "a repeat writes nothing"
    rows = kdb.fetch_all(f'SELECT asset_id, method, confidence FROM "{kdb.schema}".mention')
    assert {r["asset_id"] for r in rows} == {WBTC}
    assert all(r["method"] == "alias" and float(r["confidence"]) == 1.0 for r in rows)


def test_fetch_news_links_the_documents_it_stores(kdb: DatabaseManager) -> None:
    raw = RawDocument(source_id="tavily", url="https://x.com/story", raw_text=TICKER_TEXT, title="T")
    source = OneShotSource(SearchResult([raw], credits=1, results=1))

    summary = fetch_news(kdb, news_settings(), source, [WBTC_TOKEN], now=NEWS_NOW)

    assert summary.outcomes[0].mentions_linked >= 1
    rows = kdb.fetch_all(f'SELECT DISTINCT asset_id FROM "{kdb.schema}".mention')
    assert {r["asset_id"] for r in rows} == {WBTC}
