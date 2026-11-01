"""Every SQL statement for the knowledge tables, built per schema.

The same pattern as :mod:`candlegraph.storage.statements`, kept separate so
the knowledge layer's whole database surface is readable in one file. The set
grows with the rollout steps in ``docs/rollout/ROLLOUT.md``; a statement lands
here in the step that first executes it.

Nothing in this module touches market data, and nothing here is written by an
agent: the observer only reads these tables (invariant 21). The one DELETE
removes a re-chunked document's superseded tail chunks — text the pipeline
itself wrote a moment earlier, not history.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from psycopg2 import sql

# --- asset -------------------------------------------------------------------

ASSET_COLUMNS = ("id", "symbol", "kind", "aliases")

_ASSET_TEMPLATE = "(%(id)s,%(symbol)s,%(kind)s,%(aliases)s)"

# --- document / chunk ----------------------------------------------------------

DOCUMENT_COLUMNS = (
    "source_id",
    "url",
    "url_hash",
    "title",
    "published_at",
    "retrieved_at",
    "language",
    "raw_text",
    "meta",
)

CHUNK_COLUMNS = ("document_id", "ordinal", "text", "token_count")

_CHUNK_TEMPLATE = "(%(document_id)s,%(ordinal)s,%(text)s,%(token_count)s)"

# --- embeddings (step 5) ---------------------------------------------------------

_EMBEDDING_TEMPLATE = "(%(id)s,%(embedding)s,%(model)s)"

# --- mentions and retrieval (step 7) -------------------------------------------------

_MENTION_TEMPLATE = "(%(chunk_id)s,%(asset_id)s,%(method)s,%(confidence)s)"

# The time bound every retrieval leg carries. `published_at <= bar` is the
# no-look-ahead rule; an undated document is excluded unless display mode
# asks for it; strategy mode also requires the text to have been *fetched*
# by the bar. The asset filter joins the alias mentions.
_RETRIEVAL_FILTER = """
          d.id = c.document_id
          AND (d.published_at <= %(bar)s OR (%(include_undated)s AND d.published_at IS NULL))
          AND (NOT %(strategy)s OR d.retrieved_at <= %(bar)s)
          AND (%(asset_id)s::text IS NULL OR EXISTS (
                SELECT 1 FROM {ns}.mention m WHERE m.chunk_id = c.id AND m.asset_id = %(asset_id)s))
"""

# Each leg is an inner ORDER BY ... LIMIT (the shape its index answers) and
# an outer ROW_NUMBER, so ranking never forces a scan of every filtered row.
_DENSE_LEG = (
    """
    dense AS (
        SELECT id, ROW_NUMBER() OVER (ORDER BY distance, id) AS rnk
        FROM (
            SELECT c.id, c.embedding <=> %(query_vector)s::halfvec AS distance
            FROM {ns}.chunk c JOIN {ns}.document d ON d.id = c.document_id
            WHERE c.embedding_model = %(model)s AND
"""
    + _RETRIEVAL_FILTER
    + """
            ORDER BY c.embedding <=> %(query_vector)s::halfvec, c.id
            LIMIT %(candidates)s
        ) nearest
    )"""
)

_LEXICAL_LEG = (
    """
    lexical AS (
        SELECT id, ROW_NUMBER() OVER (ORDER BY rank DESC, id) AS rnk
        FROM (
            SELECT c.id, ts_rank_cd(c.tsv, websearch_to_tsquery('english', %(tsquery)s)) AS rank
            FROM {ns}.chunk c JOIN {ns}.document d ON d.id = c.document_id
            WHERE %(tsquery)s <> ''
              AND c.tsv @@ websearch_to_tsquery('english', %(tsquery)s) AND
"""
    + _RETRIEVAL_FILTER
    + """
            ORDER BY rank DESC, c.id
            LIMIT %(candidates)s
        ) matched
    )"""
)

_RESULT_SELECT = """
    SELECT c.id, c.document_id, c.ordinal, c.text, d.url, d.title, d.published_at, d.retrieved_at,
           f.score, f.dense_rank, f.lexical_rank
    FROM fused f
    JOIN {ns}.chunk c ON c.id = f.id
    JOIN {ns}.document d ON d.id = c.document_id
    ORDER BY f.score DESC, c.id
    LIMIT %(top_k)s
"""


def _excluded(names: tuple[str, ...], *, key: tuple[str, ...]) -> str:
    return ",\n            ".join(f"{n} = EXCLUDED.{n}" for n in names if n not in key)


@dataclass(frozen=True)
class KnowledgeStatements:
    """The knowledge layer's SQL, bound to a schema name."""

    # assets (step 3)
    upsert_asset: sql.Composed
    upsert_asset_template: str
    get_assets: sql.Composed
    # sources and documents (step 4)
    ensure_source: sql.Composed
    upsert_document: sql.Composed
    get_document_by_hash: sql.Composed
    upsert_chunks: sql.Composed
    upsert_chunks_template: str
    delete_stale_chunks: sql.Composed
    get_chunks: sql.Composed
    # embeddings (step 5)
    get_chunks_to_embed: sql.Composed
    update_chunk_embeddings: sql.Composed
    update_chunk_embeddings_template: str
    count_embedding_backlog: sql.Composed
    similar_chunks: sql.Composed
    # the source_call ledger (step 6)
    insert_source_call: sql.Composed
    source_spend: sql.Composed
    cached_search_exists: sql.Composed
    get_recent_source_calls: sql.Composed
    source_call_daily_stats: sql.Composed
    count_source_documents: sql.Composed
    get_recent_source_documents: sql.Composed
    # mentions and retrieval (step 7)
    upsert_mentions: sql.Composed
    upsert_mentions_template: str
    get_documents_without_mentions: sql.Composed
    hybrid_search: sql.Composed
    lexical_search: sql.Composed


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

    # A source is registered once; re-registering is a no-op so a pipeline
    # can call it on every run without checking first.
    ensure_source = sql.SQL(
        """
        INSERT INTO {ns}.source (id, kind, config)
        VALUES (%(id)s, %(kind)s, %(config)s)
        ON CONFLICT (id) DO NOTHING
        """
    ).format(ns=ns)

    # The idempotency key for text. On a repeat: keep a known publication
    # date when the new copy lacks one (never default it), merge metadata,
    # refresh everything else.
    upsert_document = sql.SQL(
        """
        INSERT INTO {ns}.document ({cols})
        VALUES (%(source_id)s, %(url)s, %(url_hash)s, %(title)s, %(published_at)s,
                %(retrieved_at)s, %(language)s, %(raw_text)s, %(meta)s)
        ON CONFLICT (url_hash) DO UPDATE SET
            source_id = EXCLUDED.source_id,
            url = EXCLUDED.url,
            title = COALESCE(EXCLUDED.title, document.title),
            published_at = COALESCE(EXCLUDED.published_at, document.published_at),
            retrieved_at = EXCLUDED.retrieved_at,
            language = COALESCE(EXCLUDED.language, document.language),
            raw_text = EXCLUDED.raw_text,
            meta = document.meta || EXCLUDED.meta
        """
    ).format(ns=ns, cols=sql.SQL(", ".join(DOCUMENT_COLUMNS)))

    get_document_by_hash = sql.SQL("SELECT * FROM {ns}.document WHERE url_hash = %(url_hash)s").format(ns=ns)

    # A chunk whose text changed cannot keep its vector: the embedding
    # described the old text. Clearing it puts the chunk on the re-embed
    # backlog the observer reports.
    upsert_chunks = sql.SQL(
        """
        INSERT INTO {ns}.chunk ({cols})
        VALUES %s
        ON CONFLICT (document_id, ordinal) DO UPDATE SET
            text = EXCLUDED.text,
            token_count = EXCLUDED.token_count,
            embedding = CASE WHEN chunk.text IS DISTINCT FROM EXCLUDED.text
                             THEN NULL ELSE chunk.embedding END,
            embedding_model = CASE WHEN chunk.text IS DISTINCT FROM EXCLUDED.text
                                   THEN NULL ELSE chunk.embedding_model END
        RETURNING id
        """
    ).format(ns=ns, cols=sql.SQL(", ".join(CHUNK_COLUMNS)))

    # A re-chunked document that got shorter must not keep its old tail.
    delete_stale_chunks = sql.SQL(
        "DELETE FROM {ns}.chunk WHERE document_id = %(document_id)s AND ordinal >= %(count)s"
    ).format(ns=ns)

    get_chunks = sql.SQL(
        "SELECT * FROM {ns}.chunk WHERE document_id = %(document_id)s ORDER BY ordinal"
    ).format(ns=ns)

    # --- embeddings --------------------------------------------------------

    # The backlog for one model: never embedded, or embedded by another.
    # Ordered by id so a resumed run continues where the last one stopped.
    get_chunks_to_embed = sql.SQL(
        """
        SELECT id, text FROM {ns}.chunk
        WHERE embedding IS NULL OR embedding_model IS DISTINCT FROM %(model)s
        ORDER BY id
        LIMIT %(limit)s
        """
    ).format(ns=ns)

    # One statement per batch. The vector arrives as pgvector's text form
    # and is cast to the column type; the model name is stamped beside it
    # because vectors from two models are not comparable.
    update_chunk_embeddings = sql.SQL(
        """
        UPDATE {ns}.chunk AS c
        SET embedding = v.embedding::halfvec, embedding_model = v.model
        FROM (VALUES %s) AS v(id, embedding, model)
        WHERE c.id = v.id
        RETURNING c.id
        """
    ).format(ns=ns)

    count_embedding_backlog = sql.SQL(
        """
        SELECT
            COUNT(*) FILTER (WHERE embedding IS NULL) AS missing,
            COUNT(*) FILTER (WHERE embedding IS NOT NULL
                               AND embedding_model IS DISTINCT FROM %(model)s) AS stale,
            COUNT(*) AS total
        FROM {ns}.chunk
        """
    ).format(ns=ns)

    # Cosine distance, nearest first, over the active model's vectors only.
    # ORDER BY <=> ... LIMIT is the shape the HNSW index answers.
    similar_chunks = sql.SQL(
        """
        SELECT id, document_id, ordinal, text, embedding <=> %(query)s::halfvec AS distance
        FROM {ns}.chunk
        WHERE embedding_model = %(model)s
        ORDER BY embedding <=> %(query)s::halfvec
        LIMIT %(limit)s
        """
    ).format(ns=ns)

    # --- the source_call ledger (step 6) -------------------------------------

    # One row per external call, refusals included, in the ingest_run mould.
    # This is the budget guard's input and the observer's evidence; nothing
    # else in the system reads a source's state.
    insert_source_call = sql.SQL(
        """
        INSERT INTO {ns}.source_call (source_id, called_at, query, params, credits, results, status, error)
        VALUES (%(source_id)s, %(called_at)s, %(query)s, %(params)s, %(credits)s, %(results)s,
                %(status)s, %(error)s)
        RETURNING id
        """
    ).format(ns=ns)

    # Credits spent this month and today. The caller computes the UTC
    # boundaries so a test can own the clock; refusals carry zero credits
    # and so do not need filtering out.
    source_spend = sql.SQL(
        """
        SELECT
            COALESCE(SUM(credits) FILTER (WHERE called_at >= %(month_start)s), 0) AS month,
            COALESCE(SUM(credits) FILTER (WHERE called_at >= %(day_start)s), 0) AS today
        FROM {ns}.source_call
        WHERE source_id = %(source_id)s
        """
    ).format(ns=ns)

    # The (asset, UTC day) cache: has a successful search for this intent
    # already run? In the database, not in memory, so it survives restarts
    # and is shared by every process.
    cached_search_exists = sql.SQL(
        """
        SELECT COUNT(*) AS hits FROM {ns}.source_call
        WHERE source_id = %(source_id)s AND status = 'ok' AND params->>'cache_key' = %(cache_key)s
        """
    ).format(ns=ns)

    get_recent_source_calls = sql.SQL(
        """
        SELECT * FROM {ns}.source_call
        WHERE (%(source_id)s::text IS NULL OR source_id = %(source_id)s)
        ORDER BY called_at DESC
        LIMIT %(limit)s
        """
    ).format(ns=ns)

    # The News dashboard's burn-down: one row per UTC day in the window.
    source_call_daily_stats = sql.SQL(
        """
        SELECT
            (called_at AT TIME ZONE 'UTC')::date AS day,
            COUNT(*) AS calls,
            COALESCE(SUM(credits), 0) AS credits,
            COALESCE(SUM(results), 0) AS results,
            COUNT(*) FILTER (WHERE status = 'ok') AS ok,
            COUNT(*) FILTER (WHERE status = 'error') AS errors,
            COUNT(*) FILTER (WHERE status = 'rate_limited') AS rate_limited,
            COUNT(*) FILTER (WHERE status = 'budget_refused') AS refused
        FROM {ns}.source_call
        WHERE source_id = %(source_id)s AND called_at >= %(since)s
        GROUP BY 1
        ORDER BY 1
        """
    ).format(ns=ns)

    count_source_documents = sql.SQL(
        """
        SELECT
            COUNT(DISTINCT d.id) AS documents,
            COUNT(c.id) AS chunks,
            COUNT(c.embedding) AS embedded,
            COUNT(DISTINCT d.id) FILTER (WHERE d.published_at IS NULL) AS undated,
            MAX(d.retrieved_at) AS last_retrieved_at
        FROM {ns}.document d
        LEFT JOIN {ns}.chunk c ON c.document_id = d.id
        WHERE d.source_id = %(source_id)s
        """
    ).format(ns=ns)

    get_recent_source_documents = sql.SQL(
        """
        SELECT id, title, url, published_at, retrieved_at
        FROM {ns}.document
        WHERE source_id = %(source_id)s
        ORDER BY retrieved_at DESC
        LIMIT %(limit)s
        """
    ).format(ns=ns)

    # --- mentions and retrieval (step 7) ----------------------------------------

    # Alias matches are exact and cheap; the primary key makes a re-link a no-op.
    upsert_mentions = sql.SQL(
        """
        INSERT INTO {ns}.mention (chunk_id, asset_id, method, confidence)
        VALUES %s
        ON CONFLICT (chunk_id, asset_id, method) DO NOTHING
        RETURNING chunk_id
        """
    ).format(ns=ns)

    get_documents_without_mentions = sql.SQL(
        """
        SELECT d.id FROM {ns}.document d
        WHERE EXISTS (SELECT 1 FROM {ns}.chunk c WHERE c.document_id = d.id)
          AND NOT EXISTS (
            SELECT 1 FROM {ns}.mention m JOIN {ns}.chunk c ON c.id = m.chunk_id WHERE c.document_id = d.id)
        ORDER BY d.id
        LIMIT %(limit)s
        """
    ).format(ns=ns)

    # Reciprocal rank fusion of the two legs: 1/(k + rank) per leg a chunk
    # appeared in, summed. A chunk both legs found outranks one found by one.
    hybrid_search = sql.SQL(
        "WITH"
        + _DENSE_LEG
        + ","
        + _LEXICAL_LEG
        + """,
    fused AS (
        SELECT COALESCE(dn.id, lx.id) AS id, dn.rnk AS dense_rank, lx.rnk AS lexical_rank,
               COALESCE(1.0 / (%(rrf_k)s + dn.rnk), 0) + COALESCE(1.0 / (%(rrf_k)s + lx.rnk), 0) AS score
        FROM dense dn FULL OUTER JOIN lexical lx ON dn.id = lx.id
    )"""
        + _RESULT_SELECT
    ).format(ns=ns)

    # The lexical leg alone: when no embedder is available the explanation
    # still has a retrieval, and the result says which legs ran.
    lexical_search = sql.SQL(
        "WITH"
        + _LEXICAL_LEG
        + """,
    fused AS (
        SELECT lx.id, NULL::bigint AS dense_rank, lx.rnk AS lexical_rank,
               1.0 / (%(rrf_k)s + lx.rnk) AS score
        FROM lexical lx
    )"""
        + _RESULT_SELECT
    ).format(ns=ns)

    return KnowledgeStatements(
        upsert_asset=upsert_asset,
        upsert_asset_template=_ASSET_TEMPLATE,
        get_assets=get_assets,
        ensure_source=ensure_source,
        upsert_document=upsert_document,
        get_document_by_hash=get_document_by_hash,
        upsert_chunks=upsert_chunks,
        upsert_chunks_template=_CHUNK_TEMPLATE,
        delete_stale_chunks=delete_stale_chunks,
        get_chunks=get_chunks,
        get_chunks_to_embed=get_chunks_to_embed,
        update_chunk_embeddings=update_chunk_embeddings,
        update_chunk_embeddings_template=_EMBEDDING_TEMPLATE,
        count_embedding_backlog=count_embedding_backlog,
        similar_chunks=similar_chunks,
        insert_source_call=insert_source_call,
        source_spend=source_spend,
        cached_search_exists=cached_search_exists,
        get_recent_source_calls=get_recent_source_calls,
        source_call_daily_stats=source_call_daily_stats,
        count_source_documents=count_source_documents,
        get_recent_source_documents=get_recent_source_documents,
        upsert_mentions=upsert_mentions,
        upsert_mentions_template=_MENTION_TEMPLATE,
        get_documents_without_mentions=get_documents_without_mentions,
        hybrid_search=hybrid_search,
        lexical_search=lexical_search,
    )
