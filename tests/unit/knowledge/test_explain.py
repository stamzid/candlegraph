"""``signals --explain`` offline: the numeric context, the narrators, the query,
and the pipeline against the replaying fake database from the news tests.

What the unit tier can prove: the context reads nothing after the bar and
never turns a missing figure into zero; the template cites every retrieved
chunk and nothing else; an invented citation is stripped end to end; two
explanations on one asset-day make one network call; a refusal or a missing
key serves stored documents and says so; the row is upserted on the signal's
natural key. The real corpus is `tests/integration/test_knowledge.py`.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import pytest

from candlegraph.knowledge.embed import HashEmbedder
from candlegraph.knowledge.explain import (
    AnthropicExplanationNarrator,
    Citation,
    ExplainError,
    ExplainRequest,
    Explanation,
    TemplateExplanationNarrator,
    bar_of,
    build_narrator,
    explain,
    explain_query,
    format_explanation,
    numeric_context,
    request_for,
    services_for,
)
from candlegraph.knowledge.retrieve import RetrievalError, RetrievedChunk
from candlegraph.knowledge.sources.base import SearchResult
from candlegraph.strategies.base import StrategyResult
from candlegraph.tokens import Token
from tests.conftest import SHIB, WBTC, make_settings
from tests.unit.knowledge.test_news import FakeSource, NewsDatabase, document

BAR = datetime(2026, 10, 24, 14, 0, tzinfo=UTC)
NOW = datetime(2026, 11, 4, 9, 30, tzinfo=UTC)
TOKEN = Token(address=WBTC, symbol="WBTC", name="Wrapped BTC", aliases=("WBTC", "Wrapped BTC", "Bitcoin"))
REFERENCE = Token(address=SHIB, symbol="SHIB", name="SHIBA INU")


def hourly(values: list[float], end: datetime = BAR, name: str = "WBTC") -> pd.Series:
    index = pd.date_range(end=end, periods=len(values), freq="h", tz=UTC)
    return pd.Series(values, index=index, name=name, dtype="float64")


def chunk_row(n: int, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": n,
        "document_id": 10 + n,
        "ordinal": 0,
        "text": f"Document {n} says WBTC moved on an unlock.",
        "url": f"https://news.example/{n}",
        "title": f"Story {n}",
        "published_at": datetime(2026, 10, 24, 9, n, tzinfo=UTC),
        "retrieved_at": datetime(2026, 10, 24, 14, 3, tzinfo=UTC),
        "score": 1 / (60 + n),
        "dense_rank": n,
        "lexical_rank": None,
    }
    base.update(overrides)
    return base


def chunk(n: int) -> RetrievedChunk:
    row = chunk_row(n)
    return RetrievedChunk(
        chunk_id=row["id"],
        document_id=row["document_id"],
        ordinal=0,
        text=row["text"],
        url=row["url"],
        title=row["title"],
        published_at=row["published_at"],
        retrieved_at=row["retrieved_at"],
        score=row["score"],
        dense_rank=n,
        lexical_rank=None,
    )


def settings(**overrides: Any) -> Any:
    defaults: dict[str, Any] = {
        "tavily_monthly_credits": 100,
        "tavily_daily_credits": 10,
        "tavily_reserve_credits": 5,
        "retrieval_top_k": 4,
        "retrieval_candidates": 8,
    }
    defaults.update(overrides)
    return make_settings(**defaults)


class ExplainDatabase(NewsDatabase):
    """The news fake plus retrieval rows and the explanation rows it is handed."""

    def __init__(self, chunks: int = 2) -> None:
        super().__init__()
        rows = [chunk_row(n) for n in range(1, chunks + 1)]
        self.reads["hybrid_search"] = rows
        self.reads["lexical_search"] = rows
        self.explanations: list[dict[str, Any]] = []

    def execute(self, query: Any, params: dict[str, Any] | None = None) -> int:
        if self._label(query) == "upsert_explanation" and params:
            self.explanations.append(dict(params))
        return super().execute(query, params)


def request(**overrides: Any) -> ExplainRequest:
    prices = hourly([100.0] * 30 + [101.0, 103.0, 106.0, 108.0])
    defaults: dict[str, Any] = {
        "token": TOKEN,
        "strategy": "bollinger",
        "bar": BAR,
        "position": -1,
        "score": 2.31,
        "prices": prices,
        "window": 24,
        "reference": None,
    }
    defaults.update(overrides)
    return ExplainRequest(**defaults)


# --- the numeric context (pure) ----------------------------------------------------


def test_the_context_reports_the_moves_into_the_bar_and_the_window_statistics() -> None:
    prices = hourly([100.0] * 30 + [101.0, 103.0, 106.0, 108.0])

    context = numeric_context(prices, BAR, score=2.31, window=24)

    assert context["price"] == 108.0
    assert context["move_1h"] == pytest.approx(108 / 106 - 1, abs=1e-6)
    assert context["move_3h"] == pytest.approx(108 / 101 - 1, abs=1e-6)
    assert context["move_24h"] == pytest.approx(0.08, abs=1e-6)
    assert context["score"] == 2.31
    assert context["window"] == 24
    assert context["window_high"] == 108.0 and context["window_low"] == 100.0
    assert context["window_zscore"] is not None and context["window_zscore"] > 2
    assert context["bars_available"] == 34
    assert context["reference"] is None


def test_the_context_ignores_every_bar_after_the_one_explained() -> None:
    """Invariant 17's shape: appending the future must not change the past."""
    before = hourly([100.0] * 30 + [101.0, 103.0, 106.0, 108.0])
    after = pd.concat([before, hourly([150.0, 20.0], end=BAR + timedelta(hours=2))])

    assert numeric_context(after, BAR, score=1.0, window=24) == numeric_context(
        before, BAR, score=1.0, window=24
    )


def test_a_missing_figure_is_none_never_zero() -> None:
    """Invariant 11's shape, for computed numbers."""
    short = hourly([100.0, 101.0])

    context = numeric_context(short, BAR, score=float("nan"), window=24)

    assert context["move_1h"] == pytest.approx(0.01)
    assert context["move_3h"] is None and context["move_24h"] is None
    assert context["score"] is None
    assert context["window_mean"] is None and context["window_zscore"] is None


def test_a_gap_on_a_thin_token_uses_the_last_price_before_the_horizon() -> None:
    """Hours without swaps have no row (invariant 19); the move is still defined."""
    index = pd.DatetimeIndex([BAR - timedelta(hours=30), BAR - timedelta(hours=2), BAR], tz=UTC)
    prices = pd.Series([100.0, 110.0, 121.0], index=index, name="X")

    context = numeric_context(prices, BAR, score=None, window=24)

    assert context["move_1h"] == pytest.approx(0.10), "the bar before the 1h horizon is the 2h-ago bar"
    assert context["move_24h"] == pytest.approx(0.21)
    assert context["window_mean"] is None, "three bars is not a complete 24-bar window"


def test_the_reference_comparison_is_over_aligned_bars_and_names_the_symbol() -> None:
    own = hourly([100.0] * 30 + [101.0, 103.0, 106.0, 108.0])
    rng = np.random.default_rng(3)
    ref = hourly(list(50.0 * np.exp(np.cumsum(rng.normal(0, 0.001, 34)))), name="SHIB")

    context = numeric_context(own, BAR, score=2.0, window=24, reference=ref)

    reference = context["reference"]
    assert reference["symbol"] == "SHIB"
    assert reference["aligned_bars"] == 25
    assert reference["move_24h"] == pytest.approx(float(ref.iloc[-1] / ref.iloc[-25] - 1), abs=1e-6)
    assert reference["relative_move_24h"] == pytest.approx(
        context["move_24h"] - reference["move_24h"], abs=1e-6
    )
    assert reference["correlation"] is not None and reference["beta"] is not None


def test_a_reference_with_too_few_aligned_bars_gives_no_correlation_but_still_a_move() -> None:
    own = hourly([100.0] * 30 + [101.0, 103.0, 106.0, 108.0])
    ref = pd.Series([1.0, 1.1], index=pd.DatetimeIndex([BAR - timedelta(hours=24), BAR], tz=UTC), name="SHIB")

    reference = numeric_context(own, BAR, score=None, window=24, reference=ref)["reference"]

    assert reference["move_24h"] == pytest.approx(0.1)
    assert reference["correlation"] is None and reference["beta"] is None
    assert reference["aligned_bars"] == 2


def test_the_context_is_json_safe() -> None:
    prices = hourly([100.0] * 30 + [101.0, 103.0, 106.0, 108.0])

    reference = hourly([1.0 + 0.01 * i for i in range(34)], name="SHIB")

    json.dumps(numeric_context(prices, BAR, score=1.0, window=24, reference=reference))


# --- the query and the narrators --------------------------------------------------------


def test_the_dense_query_is_derived_from_the_asset_and_the_position() -> None:
    assert explain_query(TOKEN, -1) == "WBTC Wrapped BTC Bitcoin price rally and selling pressure news"
    assert explain_query(TOKEN, 1) == "WBTC Wrapped BTC Bitcoin price drop and buying news"
    assert explain_query(Token(address=SHIB, symbol="SHIB"), 0) == "SHIB price news"


def test_the_template_cites_every_retrieved_chunk_and_nothing_else() -> None:
    citations = [Citation.from_chunk(n, chunk(n)) for n in (1, 2, 3)]
    context = {
        "symbol": "WBTC",
        "position": -1,
        "window": 24,
        "window_zscore": 2.31,
        "move_3h": 0.06,
        "move_24h": 0.08,
    }

    text = TemplateExplanationNarrator().narrate(context, citations)

    assert "[1][2][3]" in text
    assert "[4]" not in text
    assert "2.31 standard deviations above" in text
    assert "+6.0%" in text and "+8.0%" in text
    assert "short" in text


def test_the_template_calls_a_move_idiosyncratic_when_the_reference_barely_moved() -> None:
    context = {
        "symbol": "WETH",
        "position": -1,
        "window": 24,
        "score": 2.0,
        "move_3h": 0.06,
        "move_24h": 0.08,
        "reference": {"symbol": "WBTC", "move_24h": 0.004},
    }

    text = TemplateExplanationNarrator().narrate(context, [])

    assert "WBTC moved +0.4%" in text
    assert "idiosyncratic rather than market-wide" in text
    assert "No document published before the bar" in text


def test_the_template_calls_a_move_market_wide_when_the_reference_moved_with_it() -> None:
    context = {
        "symbol": "WETH",
        "position": 1,
        "window": 24,
        "score": -2.0,
        "move_24h": -0.08,
        "reference": {"symbol": "WBTC", "move_24h": -0.07},
    }

    assert "largely market-wide" in TemplateExplanationNarrator().narrate(context, [])


def test_the_template_survives_a_context_with_no_numbers_at_all() -> None:
    text = TemplateExplanationNarrator().narrate({"symbol": "X", "position": 0}, [])

    assert "n/a" in text and "flat" in text


def test_the_model_narrator_sees_only_numbers_and_citations_and_its_text_is_used() -> None:
    seen: dict[str, str] = {}

    def complete(prompt: str) -> str:
        seen["prompt"] = prompt
        return "WBTC jumped on the unlock [1]."

    citations = [Citation.from_chunk(1, chunk(1))]
    text = AnthropicExplanationNarrator("claude-x", complete=complete).narrate(
        {"symbol": "WBTC", "move_24h": 0.08}, citations
    )

    assert text == "WBTC jumped on the unlock [1]."
    assert "0.08" in seen["prompt"] and "https://news.example/1" in seen["prompt"]
    assert "SELECT" not in seen["prompt"], "the model is handed a dict, never the database"


def test_a_failing_model_falls_back_to_the_template(caplog: pytest.LogCaptureFixture) -> None:
    def complete(_prompt: str) -> str:
        raise RuntimeError("no network")

    with caplog.at_level(logging.WARNING):
        text = AnthropicExplanationNarrator("claude-x", complete=complete).narrate(
            {"symbol": "WBTC", "position": 0}, []
        )

    assert "No document published before the bar" in text
    assert "using the template" in caplog.text


def test_an_empty_model_answer_falls_back_to_the_template() -> None:
    text = AnthropicExplanationNarrator("claude-x", complete=lambda _p: "  ").narrate(
        {"symbol": "WBTC", "position": 0}, []
    )

    assert "the signal stands on the numbers alone" in text


def test_build_narrator_is_the_template_without_a_model_and_falls_back_to_the_agent_model() -> None:
    assert build_narrator(settings()).model is None
    assert build_narrator(settings(agent_llm_model="claude-a")).model == "claude-a"
    assert (
        build_narrator(settings(agent_llm_model="claude-a", knowledge_llm_model="claude-k")).model
        == "claude-k"
    )


# --- the pipeline ------------------------------------------------------------------------


def test_the_exit_criterion_holds_with_no_llm_key_and_no_news_key() -> None:
    """The stage's exit criterion in miniature: citations with a URL and a timestamp, stored."""
    db = ExplainDatabase(chunks=2)

    explanation = explain(db, settings(), request(), embedder=HashEmbedder(), now=NOW)

    assert explanation.model is None, "template mode"
    assert [c.n for c in explanation.citations] == [1, 2]
    assert explanation.citations[0].url == "https://news.example/1"
    assert explanation.citations[0].published_at == datetime(2026, 10, 24, 9, 1, tzinfo=UTC)
    assert "[1][2]" in explanation.rationale
    assert explanation.news_status == "skipped" and "stored documents only" in explanation.news_message
    assert explanation.dense_used
    (row,) = db.explanations
    assert (row["strategy"], row["symbol"], row["period_start_unix"]) == (
        "bollinger",
        "WBTC",
        int(BAR.timestamp()),
    )
    assert row["position"] == -1 and row["model"] is None
    assert json.loads(row["citations"])[0]["url"] == "https://news.example/1"
    assert json.loads(row["numeric_context"])["score"] == 2.31
    assert db.source_calls == [], "no key, no network, no ledger row"

    text = format_explanation(explanation)
    assert "https://news.example/1   2026-10-24 09:01 UTC" in text
    assert "2026-10-24 14:00 UTC  short score=+2.31" in text
    assert "model: template" in text


def test_a_rationale_citing_an_id_outside_the_retrieved_set_is_stripped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The invented-citation rejection test, through the whole pipeline."""
    db = ExplainDatabase(chunks=2)
    narrator = AnthropicExplanationNarrator(
        "claude-x", complete=lambda _p: "WBTC rose on an unlock [1]. A hack was reported [7]. WBTC led [2]."
    )

    with caplog.at_level(logging.WARNING):
        explanation = explain(db, settings(), request(), narrator=narrator, now=NOW)

    assert explanation.rationale == "WBTC rose on an unlock [1]. WBTC led [2]."
    assert explanation.rejected_citations == (7,)
    assert explanation.stripped_sentences == ("A hack was reported [7].",)
    assert explanation.model == "claude-x"
    assert db.explanations[0]["rationale"] == explanation.rationale, "the stored row is the accepted text"
    assert "outside the retrieved set" in caplog.text
    assert "stripped 1 sentence(s) citing [7]" in format_explanation(explanation)


def test_two_explanations_on_one_asset_day_make_one_network_call() -> None:
    """The cache key is (asset, UTC day), not the signal."""
    db = ExplainDatabase()
    source = FakeSource([SearchResult([document()], 1, 1), SearchResult([document("https://x/b")], 1, 1)])

    first = explain(db, settings(), request(bar=BAR, strategy="bollinger"), source=source, now=NOW)
    second = explain(
        db, settings(), request(bar=BAR + timedelta(hours=5), strategy="crossover"), source=source, now=NOW
    )

    assert len(source.calls) == 1
    assert first.news_status == "ok" and second.news_status == "cached"
    assert db.statuses() == ["ok"]
    assert len(db.explanations) == 2, "two signals, two rows, one search"


def test_the_search_window_follows_the_bar_while_the_ledger_follows_the_clock() -> None:
    """Explaining last month's bar searches last month's news but spends this month's credit."""
    db = ExplainDatabase()
    source = FakeSource([SearchResult([document()], 1, 1)])

    explain(db, settings(), request(bar=BAR), source=source, now=NOW)

    (call,) = source.calls
    assert call["end"] == BAR and call["start"] == BAR - timedelta(hours=24)
    (row,) = db.source_calls
    assert row["called_at"] == NOW
    assert json.loads(row["params"])["cache_key"] == "WBTC:2026-10-24"


def test_with_the_budget_exhausted_explain_serves_stored_documents_and_says_so() -> None:
    db = ExplainDatabase(chunks=1)
    db.source_calls.append(
        {
            "status": "ok",
            "credits": 10,
            "called_at": NOW.replace(hour=1),
            "params": json.dumps({"cache_key": "X:y"}),
        }
    )
    source = FakeSource()

    explanation = explain(db, settings(), request(), source=source, now=NOW)

    assert source.calls == []
    assert explanation.news_status == "budget_refused"
    assert "serving stored documents" in explanation.news_message
    assert db.statuses()[-1] == "budget_refused"
    assert len(explanation.citations) == 1, "the stored corpus still answers"
    assert "news: budget_refused" in format_explanation(explanation)


def test_without_an_embedder_the_lexical_leg_answers_and_the_output_says_so() -> None:
    db = ExplainDatabase(chunks=1)

    explanation = explain(db, settings(), request(), embedder=None, now=NOW)

    assert not explanation.dense_used
    assert "lexical only" in explanation.retrieval_note
    assert "retrieval: lexical only" in format_explanation(explanation)
    (label, params) = next((name, p) for name, p in db.fetches if name in ("hybrid_search", "lexical_search"))
    assert label == "lexical_search"
    assert params["asset_id"] == WBTC and params["bar"] == BAR
    assert params["strategy"] is False, "an explanation is display, not a strategy input"


def test_retrieval_is_filtered_by_the_asset_and_queries_with_its_aliases() -> None:
    db = ExplainDatabase(chunks=1)

    explain(db, settings(), request(), embedder=HashEmbedder(), now=NOW)

    params = next(p for name, p in db.fetches if name == "hybrid_search")
    assert params["tsquery"] == '"WBTC" OR "Wrapped BTC" OR "Bitcoin"'
    assert params["asset_id"] == WBTC
    assert params["top_k"] == 4 and params["candidates"] == 8


def test_a_token_is_not_compared_against_itself() -> None:
    db = ExplainDatabase(chunks=0)

    explanation = explain(db, settings(), request(reference=hourly([1.0] * 34, name="WBTC")), now=NOW)

    assert explanation.numeric_context["reference"] is None


def test_no_citations_is_a_stored_explanation_that_says_so_not_an_error() -> None:
    db = ExplainDatabase(chunks=0)

    explanation = explain(db, settings(), request(), embedder=HashEmbedder(), now=NOW)

    assert explanation.citations == () and not explanation.cited
    assert "numbers alone" in explanation.rationale
    assert "no documents published before the bar were retrieved" in format_explanation(explanation)
    assert db.explanations[0]["citations"] == "[]"


def test_an_unbuilt_retrieval_backend_is_refused_before_any_work() -> None:
    db = ExplainDatabase()

    with pytest.raises(RetrievalError, match="ragflow"):
        explain(db, settings(retrieval_backend="ragflow"), request(), now=NOW)

    assert db.explanations == [] and db.fetches == []


def test_the_quote_is_whitespace_folded_and_bounded() -> None:
    long = chunk(1)
    long = RetrievedChunk(**{**long.__dict__, "text": "word " * 200 + "\n\n end"})

    citation = Citation.from_chunk(1, long)

    assert len(citation.quote) <= 280 and citation.quote.endswith("…")
    assert "\n" not in citation.quote


# --- picking the bar from a strategy result ----------------------------------------------


def strategy_result(n: int = 30) -> StrategyResult:
    index = pd.date_range(end=BAR, periods=n, freq="h", tz=UTC)
    frame = pd.DataFrame(
        {"price": np.linspace(100, 110, n), "score": np.linspace(-1, 2.5, n), "signal": [0] * (n - 1) + [-1]},
        index=index,
    )
    return StrategyResult(name="bollinger", symbol="WBTC", frame=frame, params={"window": 20})


def test_the_latest_bar_is_explained_by_default() -> None:
    result = strategy_result()

    req = request_for(result, TOKEN)

    assert req.bar == BAR and req.position == -1 and req.score == 2.5
    assert req.window == 20 and req.strategy == "bollinger"
    assert req.params == {"window": 20}
    assert len(req.prices) == 30


def test_a_specific_bar_can_be_asked_for_and_an_absent_one_is_refused() -> None:
    result = strategy_result()
    earlier = BAR - timedelta(hours=3)

    req = request_for(result, TOKEN, at=earlier)
    assert req.bar == earlier and req.position == 0

    with pytest.raises(ExplainError, match="no bollinger bar at"):
        bar_of(result, BAR + timedelta(hours=1))
    with pytest.raises(ExplainError, match="no bollinger bar at"):
        bar_of(result, BAR - timedelta(minutes=30))


def test_a_result_without_bars_cannot_be_explained() -> None:
    empty = StrategyResult(
        name="bollinger", symbol="WBTC", frame=pd.DataFrame({"price": [], "score": [], "signal": []})
    )

    with pytest.raises(ExplainError, match="no bars"):
        bar_of(empty)


def test_a_strategy_without_a_window_parameter_gets_the_default() -> None:
    result = strategy_result()
    result.params = {"order": [1, 0, 0]}

    assert request_for(result, TOKEN).window == 24


# --- services from settings -----------------------------------------------------------------


def test_services_without_keys_are_absences_with_notes_not_errors() -> None:
    source, embedder, notes = services_for(settings(embedding_provider="openai"))

    assert source is None and embedder is None
    assert any("TAVILY_API_KEY" in n for n in notes)
    assert any("OPENAI_API_KEY" in n for n in notes)


def test_services_are_built_when_configured() -> None:
    source, embedder, notes = services_for(settings(tavily_api_key="tvly-x", embedding_provider="fake"))

    assert source is not None and source.id == "tavily"
    assert isinstance(embedder, HashEmbedder)
    assert notes == []


def test_the_row_carries_the_natural_key_and_json_columns() -> None:
    explanation = Explanation(
        strategy="bollinger",
        symbol="WBTC",
        bar=BAR,
        position=1,
        score=None,
        numeric_context={"a": 1},
        citations=(Citation.from_chunk(1, chunk(1)),),
        rationale="x [1].",
        model=None,
        generated_at=NOW,
        news_status="skipped",
    )

    row = explanation.to_row()

    assert row["period_start_unix"] == 1792850400 == int(BAR.timestamp())
    assert json.loads(row["numeric_context"]) == {"a": 1}
    assert json.loads(row["citations"])[0]["published_at"] == "2026-10-24T09:01:00+00:00"
    assert row["generated_at"] == NOW
