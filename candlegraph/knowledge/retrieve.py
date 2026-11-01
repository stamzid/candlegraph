"""Hybrid retrieval: dense + lexical, fused by reciprocal rank, bounded by time.

Rollout step 7 (``docs/rollout/ROLLOUT.md``); the decisions are in
``docs/knowledge/graph/DESIGN.md``. Read-only: nothing here writes.

Two legs, one statement. The **dense** leg orders by cosine distance over
the active model's vectors (the HNSW index's shape); the **lexical** leg
ranks a ``tsvector`` match (the GIN index's shape). Each returns its top
candidates with a rank; reciprocal rank fusion scores every chunk
``1/(k + rank)`` per leg it appeared in and sums — a chunk both legs found
outranks one either found alone, with no tuning constant worth arguing
about (``k = 60``). Dense finds "a large transfer to an exchange" for a
query about selling pressure; lexical finds the exact ticker. Together they
miss less than either.

**Nothing looks ahead** (invariant 17's analogue). Every leg is filtered by
``document.published_at <= bar``; a chunk whose document has no date is
excluded unless the caller asks for display mode with undated included, and
then it is flagged. In ``strategy`` mode ``retrieved_at <= bar`` applies
too: text we only fetched today was not available to a strategy yesterday.
The test the whole stage leans on appends a later document and asserts the
earlier bar's results do not move.

When no embedder is available (Ollama down, no key) the lexical leg runs
alone and the result says so, rather than failing the explanation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from candlegraph.knowledge.embed import Embedder, EmbeddingError, to_vector_literal
from candlegraph.knowledge.statements import knowledge_statements_for
from candlegraph.storage import DatabaseManager
from candlegraph.utils.logging_utils import service_logger

Mode = Literal["display", "strategy"]
RRF_K = 60


class RetrievalError(ValueError):
    """The retrieval configuration cannot work."""


# What `RETRIEVAL_BACKEND` may name. `ragflow` was evaluated
# (docs/knowledge/ragflow/DESIGN.md) and is deliberately not a backend yet.
BACKENDS: tuple[str, ...] = ("postgres",)


def check_backend(name: str) -> str:
    """The configured backend, or a refusal that names the evaluation.

    Raises:
        RetrievalError: the name is not a built backend.
    """
    backend = name.strip().lower()
    if backend in BACKENDS:
        return backend
    hint = (
        " RAGFlow was evaluated and judged overkill for this corpus; see docs/knowledge/ragflow/DESIGN.md"
        if backend == "ragflow"
        else ""
    )
    raise RetrievalError(f"unknown RETRIEVAL_BACKEND {name!r}; available: {', '.join(BACKENDS)}.{hint}")


@dataclass(frozen=True)
class RetrievalConfig:
    """Every knob that changes what comes back.

    Attributes:
        top_k: How many fused chunks to return.
        candidates: Candidates each leg contributes before fusion. More
            widens recall at the cost of two bigger index scans. The fused
            list is a union, so it may hold up to twice this many.
        rrf_k: The fusion constant; 60 is the literature's default.
        mode: ``display`` bounds by ``published_at`` only; ``strategy`` also
            requires ``retrieved_at <= bar`` — what a backtest may read.
        include_undated: Display mode may include chunks whose document has
            no publication date, flagged. Never in strategy mode.
    """

    top_k: int = 8
    candidates: int = 40
    rrf_k: int = RRF_K
    mode: Mode = "display"
    include_undated: bool = False

    def __post_init__(self) -> None:
        if self.top_k < 1:
            raise RetrievalError("top_k must be at least 1")
        if self.candidates < 1:
            raise RetrievalError("candidates must be at least 1")
        if self.rrf_k < 1:
            raise RetrievalError("rrf_k must be at least 1")
        if self.mode not in ("display", "strategy"):
            raise RetrievalError(f"unknown mode {self.mode!r}")
        if self.mode == "strategy" and self.include_undated:
            raise RetrievalError("a strategy may never read an undated document")


@dataclass(frozen=True)
class RetrievedChunk:
    """One fused result with its provenance."""

    chunk_id: int
    document_id: int
    ordinal: int
    text: str
    url: str
    title: str | None
    published_at: datetime | None
    retrieved_at: datetime
    score: float
    dense_rank: int | None
    lexical_rank: int | None

    @property
    def legs(self) -> str:
        """``both``, ``dense`` or ``lexical`` — which leg(s) found it."""
        if self.dense_rank is not None and self.lexical_rank is not None:
            return "both"
        return "dense" if self.dense_rank is not None else "lexical"

    @property
    def undated(self) -> bool:
        """No publication date: shown with a flag, never fed to a strategy."""
        return self.published_at is None


@dataclass(frozen=True)
class RetrievalResult:
    """What one query returned and how it was run."""

    chunks: list[RetrievedChunk]
    bar: datetime
    mode: Mode
    dense_used: bool
    """False when the lexical leg ran alone (no embedder, or it failed)."""
    note: str = ""


class Reranker(Protocol):
    """Re-scores a fused list. The default does nothing; a cross-encoder is optional."""

    def rerank(self, query: str, chunks: Sequence[RetrievedChunk]) -> list[RetrievedChunk]: ...


class NoReranker:
    """Keeps the fused order. The pipeline is correct without a model here."""

    def rerank(self, query: str, chunks: Sequence[RetrievedChunk]) -> list[RetrievedChunk]:
        return list(chunks)


def rrf_score(dense_rank: int | None, lexical_rank: int | None, k: int = RRF_K) -> float:
    """``Σ 1/(k + rank)`` over the legs a chunk appeared in. Pure."""
    score = 0.0
    if dense_rank is not None:
        score += 1.0 / (k + dense_rank)
    if lexical_rank is not None:
        score += 1.0 / (k + lexical_rank)
    return score


def lexical_query(terms: Sequence[str]) -> str:
    """A ``websearch_to_tsquery`` string: each term a quoted phrase, OR-ed.

    Quoting keeps "Wrapped Ether" a phrase; OR means any alias is enough,
    which is what an asset query wants. Empty input gives an empty query,
    which matches nothing — the lexical leg then contributes no rows.
    """
    cleaned = [" ".join(t.replace('"', " ").split()) for t in terms if t and t.strip()]
    cleaned = [t for t in cleaned if t]
    return " OR ".join(f'"{t}"' for t in dict.fromkeys(cleaned))


def retrieve(
    db: DatabaseManager,
    query: str,
    *,
    bar: datetime,
    embedder: Embedder | None = None,
    terms: Sequence[str] = (),
    asset_id: str | None = None,
    config: RetrievalConfig | None = None,
    reranker: Reranker | None = None,
) -> RetrievalResult:
    """Hybrid retrieval for ``query`` as of ``bar``.

    Args:
        db: The database; read only.
        query: The natural-language query the dense leg embeds.
        bar: The moment being explained. Nothing published after it is
            considered; in strategy mode nothing retrieved after it either.
        embedder: The active model. ``None`` runs the lexical leg alone.
        terms: Phrases for the lexical leg (aliases, a ticker, a pool
            address). Defaults to the query itself.
        asset_id: Restrict to chunks linked to this asset by a ``mention``.
        config: Depth, mode, undated handling.
        reranker: Optional re-scorer over the fused list.

    Returns:
        A :class:`RetrievalResult`; ``dense_used`` is False when the dense
        leg could not run, and ``note`` says why.
    """
    config = config or RetrievalConfig()
    statements = knowledge_statements_for(db.schema)
    tsquery = lexical_query(terms or [query])

    params: dict[str, Any] = {
        "bar": bar,
        "asset_id": asset_id,
        "tsquery": tsquery,
        "candidates": config.candidates,
        "top_k": config.top_k,
        "rrf_k": config.rrf_k,
        "strategy": config.mode == "strategy",
        "include_undated": config.include_undated and config.mode == "display",
    }

    dense_used = False
    note = ""
    if embedder is not None:
        try:
            vector = embedder.embed([query], kind="query")[0]
        except EmbeddingError as exc:
            note = f"dense leg unavailable ({exc}); lexical only"
            service_logger.warning("retrieval: %s", note)
        else:
            params["query_vector"] = to_vector_literal(vector)
            params["model"] = embedder.name
            dense_used = True
    else:
        note = "no embedder configured; lexical only"

    statement = statements.hybrid_search if dense_used else statements.lexical_search
    rows = db.fetch_all(statement, params)

    chunks = [
        RetrievedChunk(
            chunk_id=int(row["id"]),
            document_id=int(row["document_id"]),
            ordinal=int(row["ordinal"]),
            text=str(row["text"]),
            url=str(row["url"]),
            title=row.get("title"),
            published_at=row.get("published_at"),
            retrieved_at=row["retrieved_at"],
            score=float(row["score"]),
            dense_rank=int(row["dense_rank"]) if row.get("dense_rank") is not None else None,
            lexical_rank=int(row["lexical_rank"]) if row.get("lexical_rank") is not None else None,
        )
        for row in rows
    ]
    chunks = (reranker or NoReranker()).rerank(query, chunks)
    return RetrievalResult(chunks=chunks, bar=bar, mode=config.mode, dense_used=dense_used, note=note)


def format_results(result: RetrievalResult) -> str:
    """Render a retrieval the way the CLI prints it."""
    lines = [
        f"{len(result.chunks)} chunks as of {result.bar:%Y-%m-%d %H:%M} UTC "
        f"({result.mode}; {'hybrid' if result.dense_used else 'lexical only'})"
        + (f" — {result.note}" if result.note else "")
    ]
    for n, c in enumerate(result.chunks, 1):
        when = f"{c.published_at:%Y-%m-%d %H:%M}" if c.published_at else "date unknown"
        lines.append(f"[{n}] {c.score:.4f} {c.legs:<7} {when}  {c.title or c.url}")
        lines.append(f"    {c.url}")
        lines.append(f"    {c.text[:160]}{'…' if len(c.text) > 160 else ''}")
    return "\n".join(lines)
