"""Hybrid retrieval, offline: the statement chosen, the parameters sent, the
rows mapped, and the pure pieces (RRF, the lexical query, the config)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from candlegraph.knowledge.embed import EmbeddingError, HashEmbedder
from candlegraph.knowledge.retrieve import (
    BACKENDS,
    RetrievalConfig,
    RetrievalError,
    RetrievalResult,
    RetrievedChunk,
    check_backend,
    format_results,
    lexical_query,
    retrieve,
    rrf_score,
)
from tests.conftest import FakeDatabase

BAR = datetime(2026, 10, 24, 14, 0, tzinfo=UTC)


def row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": 1,
        "document_id": 10,
        "ordinal": 0,
        "text": "Bitcoin fell as ETF outflows continued.",
        "url": "https://x.com/a",
        "title": "Outflows",
        "published_at": datetime(2026, 10, 24, 9, 12, tzinfo=UTC),
        "retrieved_at": datetime(2026, 10, 24, 14, 3, tzinfo=UTC),
        "score": 0.0325,
        "dense_rank": 1,
        "lexical_rank": 2,
    }
    base.update(overrides)
    return base


# --- pure pieces -------------------------------------------------------------


def test_rrf_sums_one_over_k_plus_rank_per_leg() -> None:
    assert rrf_score(1, None) == pytest.approx(1 / 61)
    assert rrf_score(None, 3) == pytest.approx(1 / 63)
    assert rrf_score(1, 3) == pytest.approx(1 / 61 + 1 / 63)
    assert rrf_score(None, None) == 0.0


def test_a_chunk_both_legs_found_outranks_one_found_by_either() -> None:
    assert rrf_score(5, 5) > rrf_score(1, None)


def test_the_lexical_query_quotes_phrases_and_ors_them() -> None:
    assert lexical_query(["WBTC", "Wrapped Bitcoin"]) == '"WBTC" OR "Wrapped Bitcoin"'


def test_the_lexical_query_drops_blanks_duplicates_and_stray_quotes() -> None:
    assert lexical_query(["WBTC", " ", "WBTC", 'say "hi"']) == '"WBTC" OR "say hi"'
    assert lexical_query([]) == ""


@pytest.mark.parametrize(
    "kwargs",
    [
        {"top_k": 0},
        {"candidates": 0},
        {"rrf_k": 0},
        {"mode": "backtest"},
        {"mode": "strategy", "include_undated": True},
    ],
)
def test_a_config_that_cannot_work_is_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(RetrievalError):
        RetrievalConfig(**kwargs)


def test_the_default_config_is_the_documented_one() -> None:
    config = RetrievalConfig()

    assert (config.top_k, config.candidates, config.rrf_k, config.mode, config.include_undated) == (
        8,
        40,
        60,
        "display",
        False,
    )


def test_only_the_postgres_backend_exists_and_ragflow_is_refused_with_the_evaluation() -> None:
    assert check_backend(" Postgres ") == "postgres"
    assert BACKENDS == ("postgres",)
    with pytest.raises(RetrievalError, match=r"ragflow/DESIGN\.md"):
        check_backend("ragflow")
    with pytest.raises(RetrievalError, match="RETRIEVAL_BACKEND"):
        check_backend("elastic")


# --- the statement and its parameters --------------------------------------------


def test_with_an_embedder_the_hybrid_statement_runs_with_both_legs_parameters() -> None:
    db = FakeDatabase(hybrid_search=[row()])
    embedder = HashEmbedder(dimensions=8)

    result = retrieve(
        db,
        "selling pressure",
        bar=BAR,
        embedder=embedder,
        terms=["WBTC", "Bitcoin"],
        asset_id="0xwbtc",
        config=RetrievalConfig(top_k=5, candidates=20),
    )

    ((label, params),) = db.fetches
    assert label == "hybrid_search"
    assert params["bar"] == BAR
    assert params["asset_id"] == "0xwbtc"
    assert params["tsquery"] == '"WBTC" OR "Bitcoin"'
    assert params["candidates"] == 20 and params["top_k"] == 5 and params["rrf_k"] == 60
    assert params["strategy"] is False and params["include_undated"] is False
    assert params["model"] == "fake-hash"
    assert params["query_vector"].startswith("[") and params["query_vector"].count(",") == 7
    assert result.dense_used is True and result.note == ""


def test_without_an_embedder_the_lexical_statement_runs_alone_and_says_so() -> None:
    db = FakeDatabase(lexical_search=[row(dense_rank=None, lexical_rank=1)])

    result = retrieve(db, "WBTC unlock", bar=BAR)

    ((label, params),) = db.fetches
    assert label == "lexical_search"
    assert params["tsquery"] == '"WBTC unlock"', "the query itself is the lexical term by default"
    assert result.dense_used is False
    assert "no embedder" in result.note
    assert result.chunks[0].legs == "lexical"


def test_a_failing_embedder_degrades_to_lexical_rather_than_failing() -> None:
    class Broken(HashEmbedder):
        def embed(self, texts: Any, *, kind: Any) -> list[list[float]]:
            raise EmbeddingError("cannot reach Ollama")

    db = FakeDatabase(lexical_search=[])

    result = retrieve(db, "q", bar=BAR, embedder=Broken(dimensions=8))

    assert result.dense_used is False
    assert "cannot reach Ollama" in result.note
    assert db.fetches[0][0] == "lexical_search"


def test_strategy_mode_and_undated_flags_reach_the_statement() -> None:
    db = FakeDatabase(hybrid_search=[])

    retrieve(db, "q", bar=BAR, embedder=HashEmbedder(dimensions=8), config=RetrievalConfig(mode="strategy"))
    retrieve(
        db, "q", bar=BAR, embedder=HashEmbedder(dimensions=8), config=RetrievalConfig(include_undated=True)
    )

    strategy, display = (params for _, params in db.fetches)
    assert strategy["strategy"] is True and strategy["include_undated"] is False
    assert display["strategy"] is False and display["include_undated"] is True


# --- the rows -------------------------------------------------------------------------


def test_rows_become_chunks_with_provenance_and_legs() -> None:
    db = FakeDatabase(hybrid_search=[row(), row(id=2, dense_rank=None, lexical_rank=1, published_at=None)])

    result = retrieve(db, "q", bar=BAR, embedder=HashEmbedder(dimensions=8))

    both, lexical = result.chunks
    assert isinstance(both, RetrievedChunk)
    assert both.legs == "both" and both.undated is False and both.score == pytest.approx(0.0325)
    assert lexical.legs == "lexical" and lexical.undated is True
    assert isinstance(result, RetrievalResult) and result.mode == "display" and result.bar == BAR


def test_a_reranker_sees_the_fused_list_and_decides_the_order() -> None:
    class Reverse:
        def rerank(self, query: str, chunks: Any) -> list[RetrievedChunk]:
            return list(reversed(chunks))

    db = FakeDatabase(hybrid_search=[row(id=1), row(id=2)])

    result = retrieve(db, "q", bar=BAR, embedder=HashEmbedder(dimensions=8), reranker=Reverse())

    assert [c.chunk_id for c in result.chunks] == [2, 1]


def test_the_formatted_result_names_the_legs_the_mode_and_unknown_dates() -> None:
    db = FakeDatabase(hybrid_search=[row(), row(id=2, dense_rank=None, lexical_rank=1, published_at=None)])

    text = format_results(retrieve(db, "q", bar=BAR, embedder=HashEmbedder(dimensions=8)))

    assert "2 chunks as of 2026-10-24 14:00 UTC (display; hybrid)" in text
    assert "both" in text and "lexical" in text
    assert "date unknown" in text
    assert "https://x.com/a" in text
