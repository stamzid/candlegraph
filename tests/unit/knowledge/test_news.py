"""The news pipeline: cache, budget and ledger around every call — offline.

The fake database below replays the ledger it is written: a recorded call
answers the next cache and spend query, so "one search per (asset, day)"
and "the budget counts what was spent" are tested as behaviour, not as
stubbed return values.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from candlegraph.knowledge.documents import RawDocument
from candlegraph.knowledge.news import (
    NewsError,
    cache_key,
    fetch_news,
    news_query,
    run_news_fetch,
    spend_boundaries,
)
from candlegraph.knowledge.sources.base import SearchResult
from candlegraph.knowledge.sources.tavily import TavilyError, TavilyRateLimitError
from candlegraph.tokens import Token, TokenRegistry
from tests.conftest import SHIB, WBTC, FakeDatabase, make_settings

NOW = datetime(2026, 11, 1, 14, 0, tzinfo=UTC)

TOKENS = [
    Token(address=WBTC, symbol="WBTC", name="Wrapped BTC", aliases=("WBTC", "Wrapped BTC", "Bitcoin")),
    Token(address=SHIB, symbol="SHIB", name="SHIBA INU"),
]


def document(url: str = "https://example.com/a") -> RawDocument:
    return RawDocument(
        source_id="tavily", url=url, raw_text="First sentence. Second sentence.", title="A story"
    )


class FakeSource:
    """A scripted Source: returns, raises, or fails per call."""

    def __init__(self, script: list[Any] | None = None, cost: int = 1) -> None:
        self.script = list(script or [])
        self._cost = cost
        self.calls: list[dict[str, Any]] = []

    id = "tavily"
    kind = "search"

    @property
    def cost(self) -> int:
        return self._cost

    def search(self, query: str, *, start: datetime, end: datetime, max_results: int) -> SearchResult:
        self.calls.append({"query": query, "start": start, "end": end, "max_results": max_results})
        action = self.script.pop(0) if self.script else SearchResult([], self._cost, 0)
        if isinstance(action, Exception):
            raise action
        return action


class NewsDatabase(FakeDatabase):
    """Answers cache and spend queries from the source_call rows it was handed."""

    def __init__(self) -> None:
        super().__init__(get_document_by_hash=[{"id": 1}])
        self.source_calls: list[dict[str, Any]] = []

    def execute(self, query: Any, params: dict[str, Any] | None = None) -> int:
        label = self._label(query)
        if label == "insert_source_call" and params:
            self.source_calls.append(dict(params))
        return super().execute(query, params)

    def fetch_all(self, query: Any, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        label = self._label(query)
        if label == "cached_search_exists" and params:
            hits = sum(
                1
                for row in self.source_calls
                if row["status"] == "ok" and json.loads(row["params"]).get("cache_key") == params["cache_key"]
            )
            self.fetches.append((label, params))
            return [{"hits": hits}]
        if label == "source_spend" and params:
            self.fetches.append((label, params))
            month = sum(r["credits"] for r in self.source_calls if r["called_at"] >= params["month_start"])
            today = sum(r["credits"] for r in self.source_calls if r["called_at"] >= params["day_start"])
            return [{"month": month, "today": today}]
        return super().fetch_all(query, params)

    def statuses(self) -> list[str]:
        return [row["status"] for row in self.source_calls]


def settings(**overrides: Any) -> Any:
    defaults: dict[str, Any] = {
        "tavily_monthly_credits": 100,
        "tavily_daily_credits": 10,
        "tavily_reserve_credits": 5,
    }
    defaults.update(overrides)
    return make_settings(**defaults)


# --- pure helpers --------------------------------------------------------------


def test_the_query_is_derived_from_the_symbol_and_aliases() -> None:
    assert news_query(TOKENS[0]) == "WBTC Wrapped BTC Bitcoin crypto news"


def test_a_token_without_aliases_still_gets_a_query() -> None:
    assert news_query(TOKENS[1]) == "SHIB crypto news"


def test_the_cache_key_is_the_symbol_and_the_utc_day() -> None:
    late = datetime(2026, 11, 1, 23, 30, tzinfo=UTC)

    assert cache_key("WBTC", late) == "WBTC:2026-11-01"
    assert cache_key("WBTC", late) == cache_key("WBTC", NOW), "every bar of one day shares the key"


def test_spend_boundaries_are_the_utc_month_and_day_starts() -> None:
    month, day = spend_boundaries(datetime(2026, 11, 15, 9, 30, tzinfo=UTC))

    assert month == datetime(2026, 11, 1, tzinfo=UTC)
    assert day == datetime(2026, 11, 15, tzinfo=UTC)


# --- the happy path -------------------------------------------------------------


def test_a_successful_search_stores_documents_and_ledgers_the_call() -> None:
    db = NewsDatabase()
    source = FakeSource([SearchResult([document(), document("https://example.com/b")], 1, 3, skipped=1)])

    summary = fetch_news(db, settings(), source, TOKENS[:1], now=NOW)

    (outcome,) = summary.outcomes
    assert outcome.status == "ok"
    assert outcome.documents_stored == 2
    assert outcome.chunks_written > 0
    assert outcome.credits == 1 and outcome.results == 3
    (row,) = db.source_calls
    assert row["status"] == "ok" and row["credits"] == 1 and row["results"] == 3
    params = json.loads(row["params"])
    assert params["cache_key"] == "WBTC:2026-11-01"
    assert params["stored"] == 2 and params["skipped"] == 1
    assert summary.ok and summary.credits_spent == 1


def test_the_search_window_ends_at_the_bar_and_reaches_back_the_configured_hours() -> None:
    source = FakeSource()

    fetch_news(NewsDatabase(), settings(knowledge_search_window_hours=24), source, TOKENS[:1], now=NOW)

    (call,) = source.calls
    assert call["end"] == NOW
    assert (call["end"] - call["start"]).total_seconds() == 24 * 3600


def test_the_source_row_is_registered_before_any_document() -> None:
    db = NewsDatabase()

    fetch_news(db, settings(), FakeSource(), TOKENS[:1], now=NOW)

    assert next(label for label, _ in db.executed) == "ensure_source"


# --- the cache -------------------------------------------------------------------


def test_the_second_ask_on_one_day_makes_no_network_call() -> None:
    """The design's core rule: call count is per (asset, day), not per demand."""
    db = NewsDatabase()
    source = FakeSource([SearchResult([document()], 1, 1)])

    first = fetch_news(db, settings(), source, TOKENS[:1], now=NOW)
    second = fetch_news(db, settings(), source, TOKENS[:1], now=NOW.replace(hour=20))

    assert first.outcomes[0].status == "ok"
    assert second.outcomes[0].status == "cached"
    assert len(source.calls) == 1
    assert db.statuses() == ["ok"], "a cache hit is not a ledger row; nothing was called"


def test_a_failed_search_is_not_a_cache_hit_so_the_next_run_retries() -> None:
    db = NewsDatabase()
    source = FakeSource([TavilyError("boom"), SearchResult([], 1, 0)])

    fetch_news(db, settings(), source, TOKENS[:1], now=NOW)
    second = fetch_news(db, settings(), source, TOKENS[:1], now=NOW)

    assert second.outcomes[0].status == "ok"
    assert len(source.calls) == 2


# --- the budget --------------------------------------------------------------------


def test_a_refusal_is_a_ledger_row_with_zero_credits_and_no_network() -> None:
    db = NewsDatabase()
    # 9 credits already spent today against a cap of 10 with cost... make cost 2.
    source = FakeSource(cost=2)
    db.source_calls.append(
        {
            "status": "ok",
            "credits": 9,
            "called_at": NOW.replace(hour=1),
            "params": json.dumps({"cache_key": "X:other"}),
        }
    )

    summary = fetch_news(db, settings(), source, TOKENS[:1], now=NOW)

    (outcome,) = summary.outcomes
    assert outcome.status == "budget_refused" and "daily" in outcome.message
    assert source.calls == []
    refusal = db.source_calls[-1]
    assert refusal["status"] == "budget_refused" and refusal["credits"] == 0
    assert "daily" in refusal["error"]
    assert summary.ok, "a refusal is expected behaviour, not a failure"


def test_spending_accumulates_within_one_run_until_the_cap_refuses() -> None:
    """Ten one-credit tokens against a daily cap of 3: three searches, seven refusals."""
    tokens = [Token(address=f"0x{i:040x}", symbol=f"T{i}") for i in range(10)]
    db = NewsDatabase()
    source = FakeSource([SearchResult([], 1, 0)] * 10)

    summary = fetch_news(db, settings(tavily_daily_credits=3), source, tokens, now=NOW)

    assert len(source.calls) == 3
    assert [o.status for o in summary.outcomes].count("budget_refused") == 7
    assert summary.credits_spent == 3


def test_the_reserve_refusal_names_the_reserve() -> None:
    db = NewsDatabase()
    db.source_calls.append(
        {"status": "ok", "credits": 95, "called_at": NOW.replace(day=1, hour=0, minute=1), "params": "{}"}
    )

    summary = fetch_news(db, settings(tavily_daily_credits=100), FakeSource(), TOKENS[:1], now=NOW)

    assert summary.outcomes[0].status == "budget_refused"
    assert "reserve" in summary.outcomes[0].message


# --- failures ------------------------------------------------------------------------


def test_an_error_is_ledgered_and_the_run_continues_to_the_next_token() -> None:
    db = NewsDatabase()
    source = FakeSource([TavilyError("HTTP 500 after retries"), SearchResult([], 1, 0)])

    summary = fetch_news(db, settings(), source, TOKENS, now=NOW)

    assert [o.status for o in summary.outcomes] == ["error", "ok"]
    assert db.statuses() == ["error", "ok"]
    assert not summary.ok
    assert summary.failed[0].symbol == "WBTC"


def test_a_rate_limit_is_ledgered_and_halts_the_remaining_tokens() -> None:
    """One more call would spend budget on the same failure."""
    db = NewsDatabase()
    source = FakeSource([TavilyRateLimitError("HTTP 429")])

    summary = fetch_news(db, settings(), source, TOKENS, now=NOW)

    assert [o.status for o in summary.outcomes] == ["rate_limited", "rate_limited"]
    assert len(source.calls) == 1, "the second token was skipped, not attempted"
    assert db.statuses() == ["rate_limited"], "only the real call is a ledger row"
    assert "skipped" in summary.outcomes[1].message
    assert not summary.ok


# --- the operator entry point -----------------------------------------------------------


def test_run_news_fetch_without_a_key_is_a_readable_error() -> None:
    with pytest.raises(NewsError, match="TAVILY_API_KEY"):
        run_news_fetch(NewsDatabase(), make_settings(), registry=TokenRegistry(tokens=tuple(TOKENS)))


def test_run_news_fetch_refuses_an_untracked_symbol() -> None:
    registry = TokenRegistry(tokens=tuple(TOKENS))

    with pytest.raises(NewsError, match="tracked: WBTC, SHIB"):
        run_news_fetch(
            NewsDatabase(),
            make_settings(tavily_api_key="tvly-x"),
            symbols=["DOGE"],
            registry=registry,
        )
