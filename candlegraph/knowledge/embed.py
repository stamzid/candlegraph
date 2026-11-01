"""Embeddings: the ``Embedder`` Protocol, its providers, and the resumable loop.

Rollout step 5 (``docs/rollout/ROLLOUT.md``); the decision is in
``docs/knowledge/embeddings/DESIGN.md``. Three things live here:

* **The Protocol.** ``name`` (stamped on every chunk row), ``dimensions``
  (must equal the column's 1024) and ``embed(texts, kind=...)``. ``kind``
  matters: some model families want a different prefix for a query than
  for a document, and forgetting it costs recall silently, so the caller
  has to say which.
* **Providers.** ``OllamaEmbedder`` is the default — ``BAAI/bge-m3`` served
  by the ``ollama`` compose service, local and free. ``OpenAIEmbedder`` is
  the hosted option (``text-embedding-3-large`` truncated to 1024 by the
  API). ``HashEmbedder`` is a deterministic fake for tests and development:
  real vectors of the right shape with no meaning in them.
* **The loop.** ``embed_pending`` finds chunks with no vector or a vector
  from another model, embeds them in batches and stamps the model name.
  It is resumable by construction: the query it runs skips rows already
  stamped with the configured model, so a crash costs one batch.

One active model per deployment. The HNSW index is only meaningful over one
model's vectors; changing ``EMBEDDING_MODEL`` makes every existing row
"stale" and the next ``candlegraph knowledge reembed`` re-encodes them.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx
import numpy as np

from candlegraph.knowledge.statements import knowledge_statements_for
from candlegraph.settings import Settings
from candlegraph.storage import DatabaseManager
from candlegraph.utils.logging_utils import service_logger

Kind = Literal["document", "query"]

# What `EMBEDDING_PROVIDER` may name.
PROVIDERS: tuple[str, ...] = ("ollama", "openai", "fake")

# The column is halfvec(1024); the migration fixes it and the design says why.
COLUMN_DIMENSIONS = 1024


class EmbeddingError(RuntimeError):
    """A provider could not be built or did not return what was asked for."""


class Embedder(Protocol):
    """Anything that turns text into vectors of one fixed width."""

    @property
    def name(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    def embed(self, texts: Sequence[str], *, kind: Kind) -> list[list[float]]: ...


# --- prefixes ------------------------------------------------------------------------


@dataclass(frozen=True)
class Prefixes:
    """What a model family wants prepended to a document and to a query."""

    document: str = ""
    query: str = ""

    def apply(self, texts: Sequence[str], kind: Kind) -> list[str]:
        prefix = self.document if kind == "document" else self.query
        return [prefix + text for text in texts]


# Keyed by a family token that must appear as a whole word in the model
# name (`e5` in `multilingual-e5-large`, not in `some-e5ish`). bge-m3 and
# Qwen3-Embedding take plain text, so they are absent on purpose.
_FAMILY_PREFIXES: dict[str, Prefixes] = {
    "nomic-embed-text": Prefixes(document="search_document: ", query="search_query: "),
    "e5": Prefixes(document="passage: ", query="query: "),
    "mxbai-embed-large": Prefixes(query="Represent this sentence for searching relevant passages: "),
}


def prefixes_for(model: str) -> Prefixes:
    """The prefixes a model family needs; none for bge-m3 and Qwen3."""
    lowered = model.lower()
    for family, prefixes in _FAMILY_PREFIXES.items():
        if re.search(rf"(^|[/\-_:]){re.escape(family)}($|[/\-_:.])", lowered):
            return prefixes
    return Prefixes()


# --- vectors ---------------------------------------------------------------------------


def fit_dimensions(vector: Sequence[float], dimensions: int, *, truncate: bool) -> list[float]:
    """Make a model's vector fit the column, or refuse.

    A longer vector is truncated and renormalised only when ``truncate`` is
    set — correct for Matryoshka-trained models, meaningless for others.
    A shorter one is always refused: padding would invent dimensions.
    """
    values = [float(v) for v in vector]
    if len(values) == dimensions:
        return values
    if len(values) > dimensions and truncate:
        head = np.asarray(values[:dimensions], dtype="float64")
        norm = float(np.linalg.norm(head))
        return [float(v) for v in (head / norm if norm > 0 else head)]
    raise EmbeddingError(
        f"the model produced {len(values)} dimensions but the column holds {dimensions}; "
        + (
            "set EMBEDDING_TRUNCATE=true only for a Matryoshka model, or pick a model of the right width"
            if len(values) > dimensions
            else "pick a model that produces at least that many"
        )
    )


def to_vector_literal(vector: Sequence[float]) -> str:
    """``[0.1,0.2,...]`` — the text form pgvector casts to ``halfvec``."""
    return "[" + ",".join(f"{float(v):.7g}" for v in vector) + "]"


# --- providers -----------------------------------------------------------------------------


class HashEmbedder:
    """Deterministic vectors from a hash of the text. For tests and development.

    Not semantic: "ETH unlock" and "Ether unlock" land nowhere near each
    other. What it gives is the right shape, unit length, and the same
    vector for the same text every time, which is all the write path, the
    index and the tests need.
    """

    def __init__(self, dimensions: int = COLUMN_DIMENSIONS, name: str = "fake-hash") -> None:
        self._dimensions = dimensions
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, texts: Sequence[str], *, kind: Kind) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
            vector = np.random.default_rng(seed).normal(size=self._dimensions)
            vector /= np.linalg.norm(vector)
            out.append([float(v) for v in vector])
        return out


class OllamaEmbedder:
    """``POST /api/embed`` against an Ollama server — the local default."""

    def __init__(
        self,
        model: str,
        base_url: str,
        dimensions: int = COLUMN_DIMENSIONS,
        timeout: float = 120.0,
        truncate: bool = False,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._dimensions = dimensions
        self._truncate = truncate
        self._prefixes = prefixes_for(model)
        self._client = httpx.Client(timeout=timeout, transport=transport)

    @property
    def name(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, texts: Sequence[str], *, kind: Kind) -> list[list[float]]:
        if not texts:
            return []
        payload = {"model": self._model, "input": self._prefixes.apply(texts, kind)}
        try:
            response = self._client.post(f"{self._base_url}/api/embed", json=payload)
        except httpx.HTTPError as exc:
            raise EmbeddingError(
                f"cannot reach Ollama at {self._base_url} ({type(exc).__name__}). Check OLLAMA_URL, "
                "and start the embedding service with `make up-knowledge`."
            ) from exc
        if response.status_code != 200:
            raise EmbeddingError(f"Ollama returned HTTP {response.status_code}: {response.text[:200]}")
        vectors = response.json().get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            got = len(vectors) if isinstance(vectors, list) else "no"
            raise EmbeddingError(f"Ollama returned {got} vectors for {len(texts)} texts")
        return [fit_dimensions(v, self._dimensions, truncate=self._truncate) for v in vectors]


class OpenAIEmbedder:
    """The hosted option: ``text-embedding-3-large`` with ``dimensions`` set by the API."""

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-3-large",
        dimensions: int = COLUMN_DIMENSIONS,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._model = model
        self._dimensions = dimensions
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            timeout=timeout, transport=transport, headers={"Authorization": f"Bearer {api_key}"}
        )

    @property
    def name(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, texts: Sequence[str], *, kind: Kind) -> list[list[float]]:
        if not texts:
            return []
        payload = {"model": self._model, "input": list(texts), "dimensions": self._dimensions}
        try:
            response = self._client.post(f"{self._base_url}/embeddings", json=payload)
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"cannot reach the OpenAI API ({type(exc).__name__})") from exc
        if response.status_code != 200:
            # The body never carries the key; the header did, and it is not echoed.
            raise EmbeddingError(f"OpenAI returned HTTP {response.status_code}: {response.text[:200]}")
        data = sorted(response.json().get("data", []), key=lambda item: int(item["index"]))
        if len(data) != len(texts):
            raise EmbeddingError(f"OpenAI returned {len(data)} vectors for {len(texts)} texts")
        return [fit_dimensions(item["embedding"], self._dimensions, truncate=False) for item in data]


def build_embedder(settings: Settings, transport: httpx.BaseTransport | None = None) -> Embedder:
    """The configured provider, or an error naming what is wrong."""
    match settings.embedding_provider.strip().lower():
        case "ollama":
            return OllamaEmbedder(
                model=settings.embedding_model,
                base_url=settings.ollama_url,
                dimensions=settings.embedding_dimensions,
                timeout=settings.embedding_timeout_seconds,
                truncate=settings.embedding_truncate,
                transport=transport,
            )
        case "openai":
            if settings.openai_api_key is None:
                raise EmbeddingError("EMBEDDING_PROVIDER=openai needs OPENAI_API_KEY in .env")
            return OpenAIEmbedder(
                api_key=settings.openai_api_key.get_secret_value(),
                model=settings.embedding_model,
                dimensions=settings.embedding_dimensions,
                timeout=settings.embedding_timeout_seconds,
                transport=transport,
            )
        case "fake":
            return HashEmbedder(dimensions=settings.embedding_dimensions)
        case other:
            raise EmbeddingError(f"unknown EMBEDDING_PROVIDER {other!r}; available: {', '.join(PROVIDERS)}")


# --- the loop -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Backlog:
    """Chunks that need the configured model's vector."""

    missing: int
    """No embedding at all: new chunks, or chunks whose text changed."""
    stale: int
    """Embedded by another model."""
    total: int

    @property
    def pending(self) -> int:
        return self.missing + self.stale


@dataclass(frozen=True)
class EmbedSummary:
    """What one ``embed_pending`` call did."""

    model: str
    embedded: int
    batches: int
    remaining: int


def embedding_backlog(db: DatabaseManager, model: str) -> Backlog:
    """How many chunks still need ``model``'s vector."""
    statements = knowledge_statements_for(db.schema)
    (row,) = db.fetch_all(statements.count_embedding_backlog, {"model": model})
    return Backlog(missing=int(row["missing"]), stale=int(row["stale"]), total=int(row["total"]))


def embed_pending(
    db: DatabaseManager,
    embedder: Embedder,
    *,
    batch_size: int = 32,
    limit: int | None = None,
    dimensions: int = COLUMN_DIMENSIONS,
) -> EmbedSummary:
    """Embed every chunk that lacks ``embedder``'s vector, in batches.

    Args:
        db: The database.
        embedder: The configured provider.
        batch_size: Texts per model call.
        limit: Stop after about this many rows (whole batches); ``None`` runs
            to the end of the backlog.
        dimensions: The column width; the provider must match it exactly.

    Raises:
        EmbeddingError: the provider's width is wrong, the model call failed,
            or a batch wrote nothing (which would otherwise loop forever).
    """
    if embedder.dimensions != dimensions:
        raise EmbeddingError(
            f"{embedder.name} produces {embedder.dimensions} dimensions "
            f"but chunk.embedding holds {dimensions}"
        )
    if batch_size < 1:
        raise EmbeddingError("batch_size must be at least 1")

    statements = knowledge_statements_for(db.schema)
    embedded = batches = 0
    while limit is None or embedded < limit:
        rows = db.fetch_all(statements.get_chunks_to_embed, {"model": embedder.name, "limit": batch_size})
        if not rows:
            break
        vectors = embedder.embed([str(row["text"]) for row in rows], kind="document")
        records = [
            {"id": int(row["id"]), "embedding": to_vector_literal(vector), "model": embedder.name}
            for row, vector in zip(rows, vectors, strict=True)
        ]
        written = db.upsert_many(
            statements.update_chunk_embeddings, statements.update_chunk_embeddings_template, records
        )
        if written == 0:
            raise EmbeddingError(
                f"a batch of {len(records)} embeddings wrote 0 rows; stopping rather than looping"
            )
        embedded += written
        batches += 1
        service_logger.info("embedded %d chunks with %s (%d so far)", written, embedder.name, embedded)

    return EmbedSummary(
        model=embedder.name,
        embedded=embedded,
        batches=batches,
        remaining=embedding_backlog(db, embedder.name).pending,
    )
