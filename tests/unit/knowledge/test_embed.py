"""The Embedder Protocol, its providers, and the resumable embed loop — offline.

No model is downloaded here (invariant 2): the local provider is driven
through an ``httpx.MockTransport`` that plays an Ollama server, and the
deterministic hash embedder stands in wherever a vector is needed. The
integration tier proves the vectors land as ``halfvec(1024)`` and that the
HNSW index answers the similarity query.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import numpy as np
import pytest

from candlegraph.knowledge.embed import (
    PROVIDERS,
    Embedder,
    EmbeddingError,
    HashEmbedder,
    OllamaEmbedder,
    OpenAIEmbedder,
    Prefixes,
    build_embedder,
    embed_pending,
    embedding_backlog,
    fit_dimensions,
    prefixes_for,
    to_vector_literal,
)
from tests.conftest import FakeDatabase, make_settings

DIMS = 8


# --- the fake, which everything else leans on --------------------------------------


def test_the_hash_embedder_is_deterministic_and_unit_length() -> None:
    embedder = HashEmbedder(dimensions=DIMS)

    first = embedder.embed(["WETH unlock next week"], kind="document")
    second = embedder.embed(["WETH unlock next week"], kind="query")

    assert first == second, "the same text always maps to the same vector"
    assert len(first[0]) == DIMS
    assert np.linalg.norm(first[0]) == pytest.approx(1.0)


def test_different_texts_get_different_vectors() -> None:
    embedder = HashEmbedder(dimensions=DIMS)

    a, b = embedder.embed(["alpha", "beta"], kind="document")

    assert a != b


def test_the_hash_embedder_satisfies_the_protocol_and_defaults_to_the_column_width() -> None:
    embedder: Embedder = HashEmbedder()

    assert embedder.dimensions == 1024
    assert embedder.name == "fake-hash"
    assert embedder.embed([], kind="document") == []


# --- query/document prefixes ---------------------------------------------------------


@pytest.mark.parametrize("model", ["bge-m3", "bge-m3:latest", "BAAI/bge-m3", "qwen3-embedding:0.6b"])
def test_bge_and_qwen_need_no_prefix(model: str) -> None:
    assert prefixes_for(model) == Prefixes()


@pytest.mark.parametrize("model", ["nomic-embed-text", "nomic-embed-text:v1.5"])
def test_nomic_uses_the_search_prefixes(model: str) -> None:
    assert prefixes_for(model) == Prefixes(document="search_document: ", query="search_query: ")


@pytest.mark.parametrize("model", ["e5-large-v2", "intfloat/multilingual-e5-large", "e5"])
def test_e5_uses_passage_and_query(model: str) -> None:
    assert prefixes_for(model) == Prefixes(document="passage: ", query="query: ")


def test_a_name_that_merely_contains_e5_is_not_an_e5_model() -> None:
    assert prefixes_for("some-e5ish-model") == Prefixes()


# --- dimensions ------------------------------------------------------------------------


def test_an_exact_vector_passes_through() -> None:
    assert fit_dimensions([1.0, 0.0, 0.0], 3, truncate=False) == [1.0, 0.0, 0.0]


def test_a_longer_vector_is_truncated_and_renormalised_when_allowed() -> None:
    fitted = fit_dimensions([3.0, 4.0, 100.0, 100.0], 2, truncate=True)

    assert fitted == pytest.approx([0.6, 0.8])


def test_a_longer_vector_is_refused_unless_truncation_is_enabled() -> None:
    with pytest.raises(EmbeddingError, match="EMBEDDING_TRUNCATE"):
        fit_dimensions([1.0, 2.0, 3.0], 2, truncate=False)


def test_a_shorter_vector_is_always_refused() -> None:
    """Padding would invent dimensions; the column width is the model's job."""
    with pytest.raises(EmbeddingError, match="produced 2"):
        fit_dimensions([1.0, 2.0], 3, truncate=True)


def test_the_vector_literal_is_what_pgvector_parses() -> None:
    assert to_vector_literal([0.5, -0.25, 1.0]) == "[0.5,-0.25,1]"


# --- the Ollama provider -------------------------------------------------------------------


class OllamaServer:
    """Plays ``POST /api/embed``: records requests, returns unit vectors."""

    def __init__(self, dims: int = DIMS, status: int = 200, body: dict[str, Any] | None = None) -> None:
        self.dims = dims
        self.status = status
        self.body = body
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.body is not None or self.status != 200:
            return httpx.Response(self.status, json=self.body or {"error": "boom"})
        payload = json.loads(request.content)
        vectors = [[1.0 / (i + 1)] + [0.0] * (self.dims - 1) for i, _ in enumerate(payload["input"])]
        return httpx.Response(200, json={"model": payload["model"], "embeddings": vectors})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


def ollama(server: OllamaServer, model: str = "bge-m3", **kwargs: Any) -> OllamaEmbedder:
    return OllamaEmbedder(
        model=model, base_url="http://ollama:11434", dimensions=DIMS, transport=server.transport, **kwargs
    )


def test_ollama_posts_the_batch_to_the_embed_endpoint_and_parses_the_vectors() -> None:
    server = OllamaServer()

    vectors = ollama(server).embed(["one", "two"], kind="document")

    assert len(server.requests) == 1
    request = server.requests[0]
    assert request.url.path == "/api/embed"
    assert json.loads(request.content) == {"model": "bge-m3", "input": ["one", "two"]}
    assert vectors == [[1.0] + [0.0] * (DIMS - 1), [0.5] + [0.0] * (DIMS - 1)]


def test_ollama_applies_the_model_family_prefix_to_the_right_kind() -> None:
    server = OllamaServer()
    embedder = ollama(server, model="nomic-embed-text")

    embedder.embed(["a doc"], kind="document")
    embedder.embed(["a question"], kind="query")

    sent = [json.loads(r.content)["input"] for r in server.requests]
    assert sent == [["search_document: a doc"], ["search_query: a question"]]


def test_an_empty_batch_makes_no_request() -> None:
    server = OllamaServer()

    assert ollama(server).embed([], kind="document") == []
    assert server.requests == []


def test_the_name_is_the_model_and_the_dimensions_are_the_columns() -> None:
    embedder = ollama(OllamaServer(), model="bge-m3:latest")

    assert embedder.name == "bge-m3:latest"
    assert embedder.dimensions == DIMS


def test_a_server_error_is_an_embedding_error_naming_the_status() -> None:
    with pytest.raises(EmbeddingError, match="500"):
        ollama(OllamaServer(status=500)).embed(["x"], kind="document")


def test_an_unreachable_server_names_the_setting_and_the_remedy() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    embedder = OllamaEmbedder(
        model="bge-m3", base_url="http://ollama:11434", dimensions=DIMS, transport=httpx.MockTransport(refuse)
    )

    with pytest.raises(EmbeddingError, match="OLLAMA_URL") as raised:
        embedder.embed(["x"], kind="document")
    assert "make up-knowledge" in str(raised.value)


def test_a_vector_count_mismatch_is_refused_rather_than_misaligned() -> None:
    """Pairing the wrong vector with a chunk would be silent corruption."""
    server = OllamaServer(body={"embeddings": [[1.0] + [0.0] * (DIMS - 1)]})

    with pytest.raises(EmbeddingError, match="2 texts"):
        ollama(server).embed(["one", "two"], kind="document")


def test_a_model_of_the_wrong_width_is_refused() -> None:
    server = OllamaServer(dims=DIMS + 4)

    with pytest.raises(EmbeddingError, match="EMBEDDING_TRUNCATE"):
        ollama(server).embed(["x"], kind="document")


def test_truncation_can_be_opted_into_for_matryoshka_models() -> None:
    server = OllamaServer(dims=DIMS + 4)

    vectors = ollama(server, truncate=True).embed(["x"], kind="document")

    assert len(vectors[0]) == DIMS
    assert np.linalg.norm(vectors[0]) == pytest.approx(1.0)


# --- the hosted provider ------------------------------------------------------------------


def openai_server(status: int = 200) -> tuple[list[httpx.Request], httpx.MockTransport]:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "bad key"}})
        payload = json.loads(request.content)
        # Out of order on purpose: the client must sort by index.
        data = [
            {"index": i, "embedding": [float(i + 1)] + [0.0] * (payload["dimensions"] - 1)}
            for i in reversed(range(len(payload["input"])))
        ]
        return httpx.Response(200, json={"data": data, "model": payload["model"]})

    return requests, httpx.MockTransport(handle)


def test_openai_sends_the_bearer_key_and_the_dimension_and_orders_by_index() -> None:
    requests, transport = openai_server()
    embedder = OpenAIEmbedder(
        api_key="sk-test", model="text-embedding-3-large", dimensions=DIMS, transport=transport
    )

    vectors = embedder.embed(["one", "two"], kind="query")

    request = requests[0]
    assert request.headers["authorization"] == "Bearer sk-test"
    assert request.url.path.endswith("/embeddings")
    assert json.loads(request.content) == {
        "model": "text-embedding-3-large",
        "input": ["one", "two"],
        "dimensions": DIMS,
    }
    assert [v[0] for v in vectors] == [1.0, 2.0]
    assert embedder.name == "text-embedding-3-large"


def test_an_openai_rejection_is_an_embedding_error() -> None:
    _, transport = openai_server(status=401)
    embedder = OpenAIEmbedder(api_key="sk-bad", dimensions=DIMS, transport=transport)

    with pytest.raises(EmbeddingError, match="401"):
        embedder.embed(["x"], kind="document")


def test_the_key_never_appears_in_an_error() -> None:
    _, transport = openai_server(status=401)
    embedder = OpenAIEmbedder(api_key="sk-very-secret", dimensions=DIMS, transport=transport)

    with pytest.raises(EmbeddingError) as raised:
        embedder.embed(["x"], kind="document")
    assert "sk-very-secret" not in str(raised.value)


# --- choosing a provider from settings ---------------------------------------------------------


def test_the_default_provider_is_ollama_serving_bge_m3() -> None:
    embedder = build_embedder(make_settings())

    assert isinstance(embedder, OllamaEmbedder)
    assert embedder.name == "bge-m3"
    assert embedder.dimensions == 1024


def test_the_fake_provider_is_available_for_development() -> None:
    assert isinstance(build_embedder(make_settings(embedding_provider="fake")), HashEmbedder)


def test_the_hosted_provider_needs_a_key() -> None:
    with pytest.raises(EmbeddingError, match="OPENAI_API_KEY"):
        build_embedder(make_settings(embedding_provider="openai"))

    embedder = build_embedder(make_settings(embedding_provider="openai", openai_api_key="sk-x"))
    assert isinstance(embedder, OpenAIEmbedder)


def test_an_unknown_provider_is_refused_with_the_available_names() -> None:
    with pytest.raises(EmbeddingError, match="EMBEDDING_PROVIDER") as raised:
        build_embedder(make_settings(embedding_provider="voyage"))
    assert all(name in str(raised.value) for name in PROVIDERS)


# --- the resumable loop ------------------------------------------------------------------------


class BacklogDatabase(FakeDatabase):
    """A fake whose to-embed query drains as rows are written."""

    def __init__(self, pending: list[dict[str, Any]], backlog_after: dict[str, int] | None = None) -> None:
        super().__init__()
        self.pending = list(pending)
        self.backlog_after = backlog_after or {"missing": 0, "stale": 0, "total": len(pending)}

    def fetch_all(self, query: Any, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        label = self._label(query)
        self.fetches.append((label, params))
        if label == "get_chunks_to_embed":
            limit = int((params or {}).get("limit", 50))
            return self.pending[:limit]
        if label == "count_embedding_backlog":
            return [self.backlog_after]
        return []

    def upsert_many(
        self, query: Any, template: str, records: list[dict[str, Any]], page_size: int = 500
    ) -> int:
        written = super().upsert_many(query, template, records)
        ids = {r["id"] for r in records}
        self.pending = [row for row in self.pending if row["id"] not in ids]
        return written


def test_embed_pending_stamps_every_row_with_the_model_in_batches() -> None:
    db = BacklogDatabase([{"id": i, "text": f"chunk {i}"} for i in range(5)])

    summary = embed_pending(db, HashEmbedder(dimensions=DIMS), batch_size=2, dimensions=DIMS)

    assert summary.embedded == 5
    assert summary.batches == 3
    assert summary.model == "fake-hash"
    assert summary.remaining == 0
    written = [record for _, records in db.upserts for record in records]
    assert {r["model"] for r in written} == {"fake-hash"}
    assert all(r["embedding"].startswith("[") and r["embedding"].count(",") == DIMS - 1 for r in written)
    assert all(
        params == {"model": "fake-hash", "limit": 2}
        for label, params in db.fetches
        if label == "get_chunks_to_embed"
    )


def test_embed_pending_honours_a_limit_and_reports_what_is_left() -> None:
    db = BacklogDatabase(
        [{"id": i, "text": f"chunk {i}"} for i in range(5)],
        backlog_after={"missing": 3, "stale": 0, "total": 5},
    )

    summary = embed_pending(db, HashEmbedder(dimensions=DIMS), batch_size=2, limit=2, dimensions=DIMS)

    assert summary.embedded == 2
    assert summary.remaining == 3


def test_embed_pending_with_nothing_to_do_makes_no_model_call() -> None:
    calls: list[int] = []

    class Counting(HashEmbedder):
        def embed(self, texts: Any, *, kind: Any) -> list[list[float]]:
            calls.append(len(texts))
            return super().embed(texts, kind=kind)

    summary = embed_pending(BacklogDatabase([]), Counting(dimensions=DIMS), dimensions=DIMS)

    assert summary.embedded == 0
    assert calls == []


def test_a_provider_of_the_wrong_width_is_refused_before_any_row_is_touched() -> None:
    db = BacklogDatabase([{"id": 1, "text": "x"}])

    with pytest.raises(EmbeddingError, match="1024"):
        embed_pending(db, HashEmbedder(dimensions=DIMS), dimensions=1024)
    assert db.upserts == []


def test_a_write_that_lands_nothing_stops_the_loop_instead_of_spinning() -> None:
    class Stuck(BacklogDatabase):
        def upsert_many(
            self, query: Any, template: str, records: list[dict[str, Any]], page_size: int = 500
        ) -> int:
            return 0

    with pytest.raises(EmbeddingError, match="wrote 0"):
        embed_pending(Stuck([{"id": 1, "text": "x"}]), HashEmbedder(dimensions=DIMS), dimensions=DIMS)


def test_the_backlog_reads_missing_and_stale_counts() -> None:
    db = FakeDatabase(count_embedding_backlog=[{"missing": 4, "stale": 2, "total": 10}])

    backlog = embedding_backlog(db, "bge-m3")

    assert (backlog.missing, backlog.stale, backlog.total, backlog.pending) == (4, 2, 10, 6)
    assert db.fetches == [("count_embedding_backlog", {"model": "bge-m3"})]
