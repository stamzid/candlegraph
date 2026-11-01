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
    )
