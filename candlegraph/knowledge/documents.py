"""Documents: URL normalisation, the ``url_hash`` key, chunking, and the upserts.

Rollout step 4 (``docs/rollout/ROLLOUT.md``). Three pure transforms and one
write path:

* :func:`normalise_url` turns the many spellings of one address into one
  string, and :func:`url_hash` hashes it. That hash is the idempotency key
  for text — searching the same window twice must not store the same
  article twice (invariant 4's shape, applied to documents).
* :func:`chunk_text` splits a document into retrieval-sized pieces on
  paragraph and sentence boundaries, never mid-sentence, with a small
  overlap so a fact straddling a boundary is still retrievable whole.
* :func:`store_document` upserts the document on its hash and its chunks
  on ``(document_id, ordinal)``. Re-storing a changed document replaces
  its chunks and clears the embedding of any chunk whose text changed, so
  a stale vector can never describe new text.

``published_at`` is never defaulted here or anywhere: an unknown date stays
``NULL`` (invariant 11), because the no-look-ahead rule reads that column.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from candlegraph.knowledge.statements import knowledge_statements_for
from candlegraph.storage import DatabaseManager


class DocumentError(ValueError):
    """A URL that cannot identify a document, or a chunking config that cannot work."""


# --- URL normalisation ---------------------------------------------------------

# Query parameters that identify a *visit*, not a document. Any `utm_*` key
# plus the usual click and campaign identifiers from ad and mail platforms.
TRACKING_PREFIXES = ("utm_",)
TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "gclsrc",
        "dclid",
        "msclkid",
        "yclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "_ga",
        "_gl",
        "_hsenc",
        "_hsmi",
        "oly_anon_id",
        "oly_enc_id",
        "ref",
        "ref_src",
        "ref_url",
        "referrer",
        "source",
        "s",
        "spm",
        "cmpid",
        "campaign_id",
        "mkt_tok",
        "trk",
        "trkcampaign",
    }
)
DEFAULT_PORTS = {"http": 80, "https": 443}
_FOREIGN_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+\-]*:(?!\d)")


def _is_tracking(name: str) -> bool:
    lowered = name.lower()
    return lowered in TRACKING_PARAMS or lowered.startswith(TRACKING_PREFIXES)


def normalise_url(url: str) -> str:
    """One canonical spelling for a web address.

    What is folded, and why each is the same document:

    * whitespace around the URL, and a missing scheme (``example.com/a``);
    * ``http`` and ``https`` → ``https`` — the same article served both ways;
    * scheme and host case, a leading ``www.``, and the scheme's default port;
    * credentials in the authority (they identify a session, not a page);
    * repeated slashes and a trailing slash in the path;
    * tracking query parameters (``utm_*``, ``fbclid``, ``gclid``, …), and
      the order of the parameters that remain;
    * the fragment (``#section``), which never reaches the server.

    Everything else — the path's letter case, the remaining query
    parameters and their values — is kept, because on many sites they do
    select different documents.

    Raises:
        DocumentError: empty input, a non-HTTP scheme, or no host.
    """
    raw = url.strip()
    if not raw:
        raise DocumentError("a document URL must not be empty")
    if "://" not in raw:
        # `mailto:x`, `javascript:...`: a scheme with no authority. A bare
        # `host:8080/path` is not — the colon is followed by a port.
        if _FOREIGN_SCHEME.match(raw):
            raise DocumentError(f"unsupported URL scheme in {url!r}; only http and https identify documents")
        raw = "https://" + raw

    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS:
        raise DocumentError(
            f"unsupported URL scheme {parts.scheme!r}; only http and https identify documents"
        )

    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise DocumentError(f"URL {url!r} has no host")
    host = host.removeprefix("www.")

    try:
        port = parts.port
    except ValueError as exc:
        raise DocumentError(f"URL {url!r} has an invalid port") from exc
    netloc = host if port is None or port == DEFAULT_PORTS[scheme] else f"{host}:{port}"

    path = re.sub(r"/{2,}", "/", parts.path) or "/"
    if len(path) > 1:
        path = path.rstrip("/")

    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking(k)]
    query = urlencode(sorted(kept))

    return urlunsplit(("https", netloc, path, query, ""))


def url_hash(url: str) -> str:
    """``sha256`` of the normalised URL, hex: the ``document.url_hash`` key."""
    return hashlib.sha256(normalise_url(url).encode("utf-8")).hexdigest()


# --- chunking ------------------------------------------------------------------

# A "token" here is a word or a punctuation mark. It is a proxy for the
# subword tokens an embedding model counts — close enough for sizing, and
# it needs no model download (invariant 2 for the unit tier).
_TOKEN_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)
# A sentence ends at . ! or ?, optionally followed by a closing quote or
# bracket, then whitespace. Two fixed-width lookbehinds because Python's
# regex engine allows no variable-width ones.
_SENTENCE_BREAK = re.compile(r"(?:(?<=[.!?])|(?<=[.!?][\"'\)\]]))\s+")
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")


def count_tokens(text: str) -> int:
    """Words plus punctuation marks."""
    return len(_TOKEN_RE.findall(text))


def split_paragraphs(text: str) -> list[str]:
    """Blank-line separated blocks, whitespace collapsed, empties dropped."""
    return [" ".join(block.split()) for block in _PARAGRAPH_BREAK.split(text) if block.strip()]


def split_sentences(paragraph: str) -> list[str]:
    """Sentences of one paragraph, terminators kept, empties dropped."""
    return [piece.strip() for piece in _SENTENCE_BREAK.split(paragraph) if piece.strip()]


@dataclass(frozen=True)
class ChunkConfig:
    """How big a chunk is, and how much neighbours share.

    Attributes:
        target_tokens: Close a chunk once it reaches this many tokens.
        max_tokens: Never let a chunk grow past this by adding a sentence.
            A single sentence longer than this becomes a chunk on its own;
            a sentence is never split.
        min_tokens: A paragraph end closes the chunk once it holds at least
            this much, so chunks prefer paragraph boundaries.
        overlap_tokens: Trailing sentences of one chunk, up to this many
            tokens, repeated at the start of the next.
    """

    target_tokens: int = 400
    max_tokens: int = 500
    min_tokens: int = 300
    overlap_tokens: int = 50

    def __post_init__(self) -> None:
        if not 0 < self.min_tokens <= self.target_tokens <= self.max_tokens:
            raise DocumentError("need 0 < min_tokens <= target_tokens <= max_tokens")
        if not 0 <= self.overlap_tokens < self.min_tokens:
            raise DocumentError("overlap_tokens must be non-negative and below min_tokens")


@dataclass(frozen=True)
class Chunk:
    """One retrieval unit of a document."""

    ordinal: int
    text: str
    token_count: int


def _overlap_tail(sentences: list[str], budget: int) -> list[str]:
    """The last sentences of a chunk that fit in ``budget`` tokens.

    Never the whole chunk: an overlap that repeats everything would make the
    next chunk a copy of this one and the loop would never advance.
    """
    tail: list[str] = []
    used = 0
    for sentence in reversed(sentences[1:]):
        tokens = count_tokens(sentence)
        if used + tokens > budget:
            break
        tail.insert(0, sentence)
        used += tokens
    return tail


def chunk_text(text: str, config: ChunkConfig | None = None) -> list[Chunk]:
    """Split ``text`` into chunks on paragraph and sentence boundaries.

    Sentences are packed in order. A chunk closes when it reaches
    ``target_tokens``, when the next sentence would push it past
    ``max_tokens``, or at a paragraph end once it holds ``min_tokens``. The
    next chunk opens with the previous one's trailing sentences up to
    ``overlap_tokens``. Ordinals run from 0 in document order and are
    stable: the same text and config always give the same chunks.

    Returns:
        An empty list for empty or whitespace-only text.
    """
    config = config or ChunkConfig()
    chunks: list[Chunk] = []
    current: list[str] = []
    carried = 0  # how many sentences at the front of `current` are overlap

    def flush() -> None:
        nonlocal current, carried
        fresh = current[carried:]
        if not fresh:
            # Nothing but overlap: the previous chunk already holds it.
            current, carried = [], 0
            return
        body = " ".join(current)
        chunks.append(Chunk(ordinal=len(chunks), text=body, token_count=count_tokens(body)))
        overlap = _overlap_tail(current, config.overlap_tokens)
        current, carried = overlap, len(overlap)

    def tokens_in(sentences: list[str]) -> int:
        return sum(count_tokens(s) for s in sentences)

    for paragraph in split_paragraphs(text):
        for sentence in split_sentences(paragraph):
            size = count_tokens(sentence)
            if current and tokens_in(current) + size > config.max_tokens:
                flush()
                # The overlap plus a long sentence may still not fit; a
                # sentence is never split, so drop the overlap instead.
                if current and tokens_in(current) + size > config.max_tokens:
                    current, carried = [], 0
            current.append(sentence)
            if tokens_in(current) >= config.target_tokens:
                flush()
        if current and tokens_in(current) >= config.min_tokens:
            flush()

    flush()
    return chunks


# --- rows and the write path -----------------------------------------------------


@dataclass(frozen=True)
class RawDocument:
    """What a source returns: text and provenance, not yet stored.

    ``published_at`` is ``None`` when the source did not say. It is never
    replaced with "now"; the no-look-ahead rule depends on that.
    """

    source_id: str
    url: str
    raw_text: str
    title: str | None = None
    published_at: datetime | None = None
    language: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StoredDocument:
    """What :func:`store_document` did."""

    document_id: int
    url_hash: str
    chunks_written: int
    chunks_removed: int


def document_row(raw: RawDocument, retrieved_at: datetime | None = None) -> dict[str, Any]:
    """The ``document`` row for a raw document; pure."""
    return {
        "source_id": raw.source_id,
        "url": raw.url.strip(),
        "url_hash": url_hash(raw.url),
        "title": raw.title,
        "published_at": raw.published_at,
        "retrieved_at": retrieved_at or datetime.now(tz=UTC),
        "language": raw.language,
        "raw_text": raw.raw_text,
        "meta": json.dumps(raw.meta, default=str),
    }


def chunk_rows(document_id: int, chunks: list[Chunk]) -> list[dict[str, Any]]:
    """The ``chunk`` rows for a stored document; pure."""
    return [
        {"document_id": document_id, "ordinal": c.ordinal, "text": c.text, "token_count": c.token_count}
        for c in chunks
    ]


def ensure_source(
    db: DatabaseManager, source_id: str, kind: str, config: dict[str, Any] | None = None
) -> None:
    """Register a source if it is not already there. ``kind``: search | feed | file."""
    statements = knowledge_statements_for(db.schema)
    db.execute(
        statements.ensure_source,
        {"id": source_id, "kind": kind, "config": json.dumps(config or {}, default=str)},
    )


def store_document(
    db: DatabaseManager,
    raw: RawDocument,
    config: ChunkConfig | None = None,
    retrieved_at: datetime | None = None,
) -> StoredDocument:
    """Upsert one document and its chunks; idempotent on the URL hash.

    The document row conflicts on ``url_hash``: a known date is kept when the
    new copy has none, ``meta`` is merged, text and title are refreshed.
    Chunks conflict on ``(document_id, ordinal)``; a chunk whose text changed
    loses its embedding, and chunks past the new count are removed so a
    shortened document does not keep tail chunks of its old self.

    Raises:
        psycopg2.Error: the write failed (invariant 3 — never swallowed).
    """
    statements = knowledge_statements_for(db.schema)
    row = document_row(raw, retrieved_at)
    db.execute(statements.upsert_document, row)
    found = db.fetch_all(statements.get_document_by_hash, {"url_hash": row["url_hash"]})
    if not found:
        raise RuntimeError(
            f"document {row['url_hash']} was not stored"
        )  # pragma: no cover - the upsert raised first
    document_id = int(found[0]["id"])

    chunks = chunk_text(raw.raw_text, config)
    written = db.upsert_many(
        statements.upsert_chunks, statements.upsert_chunks_template, chunk_rows(document_id, chunks)
    )
    removed = db.execute(statements.delete_stale_chunks, {"document_id": document_id, "count": len(chunks)})
    return StoredDocument(
        document_id=document_id, url_hash=row["url_hash"], chunks_written=written, chunks_removed=removed
    )
