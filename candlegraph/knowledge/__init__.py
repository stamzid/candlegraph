"""The knowledge layer: entities, documents, and what cites them (stage 1).

Grows step by step with ``docs/rollout/ROLLOUT.md``. So far: the schema
(``statements.py``), the asset sync (``assets.py``), documents
(``documents.py``), embeddings (``embed.py``), and the metered Tavily news
source with its budget guard and ledger (``sources/``, ``budget.py``,
``news.py``).
"""

from candlegraph.knowledge.assets import asset_rows, sync_assets
from candlegraph.knowledge.documents import (
    Chunk,
    ChunkConfig,
    RawDocument,
    StoredDocument,
    chunk_text,
    normalise_url,
    store_document,
    url_hash,
)
from candlegraph.knowledge.statements import KnowledgeStatements, knowledge_statements_for

__all__ = [
    "Chunk",
    "ChunkConfig",
    "KnowledgeStatements",
    "RawDocument",
    "StoredDocument",
    "asset_rows",
    "chunk_text",
    "knowledge_statements_for",
    "normalise_url",
    "store_document",
    "sync_assets",
    "url_hash",
]
