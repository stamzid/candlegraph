"""The knowledge-layer schema: eight plain tables and the vector extension.

Stage 1 (``docs/knowledge/graph/DESIGN.md``) stores text and its links to the
assets whose prices are already here: ``asset`` is the thin entity supertype,
``source``/``document``/``chunk`` the corpus, ``mention`` and ``edge`` the
links, ``explanation`` the per-signal rationale, and ``source_call`` the
metered-source ledger in the ``ingest_run`` mould.

Every table is deliberately plain, not a hypertable — the placement table in
``docs/storage/table-placement/DESIGN.md`` records why each. The one worth
repeating: an ANN index must be global, so ``chunk`` must never be chunked by
time; a hypertable would give every monthly chunk its own HNSW index and turn
one search into a union of partial searches.

``chunk.embedding`` is ``halfvec(1024)``: half the storage of ``vector`` at
negligible recall cost, dimension fixed by the column because there is one
active embedding model per deployment (``docs/knowledge/embeddings/DESIGN.md``).
``document.published_at`` is nullable and never defaulted: an unknown date
must not become "now", because the no-look-ahead rule reads this column.

Revision ID: f399ca0770a0
Revises: e4b9c7a05f13
Create Date: 2026-10-30 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from candlegraph.settings import get_settings

revision: str = "f399ca0770a0"
down_revision: str | None = "e4b9c7a05f13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = get_settings().db_schema

# Dropped in reverse dependency order by the downgrade.
TABLES = ("asset", "source", "document", "chunk", "mention", "edge", "explanation", "source_call")


def _require_extension() -> None:
    """Refuse a server that cannot ``CREATE EXTENSION vector``, and say why.

    The same shape as the TimescaleDB preflight in ``e4b9c7a05f13``: a plain
    PostgreSQL image, or the Alpine ``timescale/timescaledb`` image the stack
    ran before stage 1, has no pgvector at all, and the error PostgreSQL gives
    does not name the fix. pgvector needs no ``shared_preload_libraries``
    entry, so availability is the whole check.
    """
    available = (
        op.get_bind().execute(sa.text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")).scalar()
    )
    if not available:
        raise RuntimeError(
            "the vector (pgvector) extension is not available on this server. "
            "candlegraph runs on the timescale/timescaledb-ha image, which "
            "bundles it; see docs/knowledge/vector-store/DESIGN.md. Point "
            "DB_HOST at the compose database, or `make reset` to rebuild it "
            "on the pinned image."
        )


def upgrade() -> None:
    _require_extension()
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # The entity supertype. `id` is the token address today so joins to
    # `token` are free; stage 3's instruments get a new `kind`, not a table.
    op.create_table(
        "asset",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("symbol", sa.Text, nullable=False),
        # token | pool | instrument | index
        sa.Column("kind", sa.Text, nullable=False, server_default="token"),
        # The entity-linking dictionary: names a document might use.
        sa.Column("aliases", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        schema=SCHEMA,
    )

    op.create_table(
        "source",
        sa.Column("id", sa.Text, primary_key=True),
        # search | feed | file
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("config", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        schema=SCHEMA,
    )

    op.create_table(
        "document",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("source_id", sa.Text, sa.ForeignKey(f"{SCHEMA}.source.id"), nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        # sha256 of the normalised URL: the idempotency key, so searching the
        # same window twice stores one row. Invariant 4's shape, for text.
        sa.Column("url_hash", sa.Text, nullable=False, unique=True),
        sa.Column("title", sa.Text, nullable=True),
        # Nullable and never defaulted: unknown must stay distinguishable from
        # a real date, because the no-look-ahead bound reads this column.
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("language", sa.Text, nullable=True),
        sa.Column("raw_text", sa.Text, nullable=False, server_default=""),
        sa.Column("meta", postgresql.JSONB, nullable=False, server_default="{}"),
        schema=SCHEMA,
    )

    # `embedding` and `tsv` need pgvector's type and a generated column, which
    # SQLAlchemy's DDL does not speak natively — raw SQL keeps it exact.
    op.execute(
        f"""
        CREATE TABLE "{SCHEMA}".chunk (
            id BIGSERIAL PRIMARY KEY,
            document_id BIGINT NOT NULL REFERENCES "{SCHEMA}".document (id) ON DELETE CASCADE,
            ordinal INT NOT NULL,
            text TEXT NOT NULL,
            token_count INT,
            embedding HALFVEC(1024),
            embedding_model TEXT,
            tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
            CONSTRAINT uix_chunk_document_ordinal UNIQUE (document_id, ordinal)
        )
        """
    )
    # HNSW rather than IVFFlat: no training set that goes stale as the corpus
    # grows. Cosine over halfvec; one global index, which is why the table is
    # plain. m and ef_construction per docs/knowledge/vector-store/DESIGN.md.
    op.execute(
        f'CREATE INDEX ix_chunk_embedding_hnsw ON "{SCHEMA}".chunk '
        "USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )
    # The lexical half of hybrid retrieval.
    op.execute(f'CREATE INDEX ix_chunk_tsv ON "{SCHEMA}".chunk USING gin (tsv)')

    op.create_table(
        "mention",
        sa.Column(
            "chunk_id",
            sa.BigInteger,
            sa.ForeignKey(f"{SCHEMA}.chunk.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("asset_id", sa.Text, sa.ForeignKey(f"{SCHEMA}.asset.id"), primary_key=True),
        # alias | ner | model. Alias matches are exact and carry 1.0.
        sa.Column("method", sa.Text, primary_key=True),
        sa.Column("confidence", sa.Numeric, nullable=False),
        schema=SCHEMA,
    )

    # Typed, time-bounded, and always derived from stored data — never
    # asserted by a model (docs/knowledge/graph/DESIGN.md).
    op.create_table(
        "edge",
        sa.Column("from_id", sa.Text, primary_key=True),
        sa.Column("to_id", sa.Text, primary_key=True),
        # trades_in | co_mentioned | correlated_with | derived_from
        sa.Column("relation", sa.Text, primary_key=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("weight", sa.Numeric, nullable=True),
        sa.Column("evidence", postgresql.JSONB, nullable=False, server_default="{}"),
        schema=SCHEMA,
    )
    op.create_index("ix_edge_relation_valid_from", "edge", ["relation", "valid_from"], schema=SCHEMA)

    # Keyed by the signal's natural key, so re-explaining a bar is an upsert
    # and one bar's citation set is one row. Plain for now; the placement
    # document records the revisit condition (explanations for every bar).
    op.create_table(
        "explanation",
        sa.Column("strategy", sa.Text, primary_key=True),
        sa.Column("symbol", sa.Text, primary_key=True),
        sa.Column("period_start_unix", sa.BigInteger, primary_key=True),
        sa.Column("position", sa.Integer, nullable=False),
        # What the narrator was shown — the numbers that make the prose
        # auditable after the rolling statistics have moved on.
        sa.Column("numeric_context", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("citations", postgresql.JSONB, nullable=False, server_default="[]"),
        # Empty when no model is configured; the citations still stand.
        sa.Column("rationale", sa.Text, nullable=False, server_default=""),
        sa.Column("model", sa.Text, nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("position IN (-1, 0, 1)", name="ck_explanation_position"),
        schema=SCHEMA,
    )

    # Every external call, its cost, and its outcome — refusals included.
    # The budget guard reads this; the observer reports on it.
    op.create_table(
        "source_call",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("source_id", sa.Text, sa.ForeignKey(f"{SCHEMA}.source.id"), nullable=False),
        sa.Column("called_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("query", sa.Text, nullable=True),
        sa.Column("params", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("credits", sa.Integer, nullable=False, server_default="0"),
        sa.Column("results", sa.Integer, nullable=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("error", sa.Text, nullable=True),
        sa.CheckConstraint("status IN ('ok', 'error', 'budget_refused')", name="ck_source_call_status"),
        schema=SCHEMA,
    )
    # The budget guard's read: this source's calls over a recent window.
    op.create_index("ix_source_call_source_called", "source_call", ["source_id", "called_at"], schema=SCHEMA)


def downgrade() -> None:
    for table in reversed(TABLES):
        op.execute(f'DROP TABLE IF EXISTS "{SCHEMA}".{table} CASCADE')
    # The extension is left installed, as the TimescaleDB revision leaves
    # timescaledb: dropping it is destruction beyond undoing this revision.
