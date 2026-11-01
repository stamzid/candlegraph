"""The Tavily client against recorded responses on a MockTransport.

The fixture below is a real `/search` response with its content truncated,
so a field rename on Tavily's side breaks a test here instead of production.
The two rules the design leans on are pinned hard: 429/432 are never
retried, and the key can never appear in an error.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from candlegraph.knowledge.sources.base import SearchResult, Source
from candlegraph.knowledge.sources.tavily import (
    SEARCH_COST,
    TavilyError,
    TavilyRateLimitError,
    TavilySource,
    parse_published,
)

WINDOW = {
    "start": datetime(2026, 10, 23, 14, 0, tzinfo=UTC),
    "end": datetime(2026, 10, 24, 14, 0, tzinfo=UTC),
}

# A real response shape, content truncated. Field names must not drift.
RECORDED = {
    "query": "WBTC Wrapped BTC Bitcoin crypto news",
    "response_time": 1.31,
    "results": [
        {
            "title": "Bitcoin slips as ETF outflows continue",
            "url": "https://www.example-news.com/markets/bitcoin-etf-outflows?utm_source=rss",
            "content": "Bitcoin fell 2.3% on Thursday as spot ETF outflows...",
            "raw_content": "Bitcoin fell 2.3% on Thursday as spot ETF outflows extended into a fourth day...",
            "score": 0.97,
            "published_date": "Fri, 24 Oct 2026 09:12:34 GMT",
        },
        {
            "title": "Exchange X lists new WBTC pair",
            "url": "https://blog.example-exchange.com/wbtc-pair",
            "content": "Short announcement body.",
            "raw_content": None,
            "score": 0.81,
            "published_date": "2026-10-24T11:40:00Z",
        },
        {
            "title": "A result with no usable text",
            "url": "https://example.com/empty",
            "content": "",
            "raw_content": None,
            "score": 0.40,
            "published_date": None,
        },
    ],
}


class Server:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def client(server: Server, **kwargs: Any) -> TavilySource:
    defaults: dict[str, Any] = {"api_key": "tvly-test-key", "max_tries": 3}
    defaults.update(kwargs)
    return TavilySource(transport=httpx.MockTransport(server.handle), **defaults)


def ok_server(body: dict[str, Any] | None = None) -> Server:
    return Server([httpx.Response(200, json=body if body is not None else RECORDED)])


# --- the request -------------------------------------------------------------


def test_the_request_is_a_bounded_basic_news_search_with_raw_content() -> None:
    server = ok_server()

    client(server).search("WBTC news", max_results=8, **WINDOW)

    request = server.requests[0]
    assert request.url == "https://api.tavily.com/search"
    assert request.headers["authorization"] == "Bearer tvly-test-key"
    assert json.loads(request.content) == {
        "query": "WBTC news",
        "topic": "news",
        "search_depth": "basic",
        "max_results": 8,
        "include_raw_content": True,
        "start_date": "2026-10-23",
        "end_date": "2026-10-24",
    }


def test_the_source_satisfies_the_protocol_and_prices_itself() -> None:
    source: Source = client(ok_server())

    assert source.id == "tavily"
    assert source.kind == "search"
    assert source.cost == SEARCH_COST["basic"] == 1


def test_advanced_depth_costs_double_and_is_sent() -> None:
    server = ok_server()
    source = client(server, search_depth="advanced")

    assert source.cost == 2
    source.search("q", max_results=8, **WINDOW)
    assert json.loads(server.requests[0].content)["search_depth"] == "advanced"


def test_an_unknown_depth_is_refused_naming_the_setting() -> None:
    with pytest.raises(TavilyError, match="TAVILY_SEARCH_DEPTH"):
        client(ok_server(), search_depth="exhaustive")


# --- the response ---------------------------------------------------------------


def test_results_become_raw_documents_preferring_raw_content() -> None:
    result = client(ok_server()).search("q", max_results=8, **WINDOW)

    assert isinstance(result, SearchResult)
    first, second = result.documents
    assert first.source_id == "tavily"
    assert "fourth day" in first.raw_text, "raw_content is preferred over the snippet"
    assert second.raw_text == "Short announcement body.", "content is the fallback"
    assert first.title == "Bitcoin slips as ETF outflows continue"
    assert first.meta["score"] == 0.97
    assert first.meta["query"] == "q"


def test_an_empty_content_result_is_skipped_and_counted() -> None:
    result = client(ok_server()).search("q", max_results=8, **WINDOW)

    assert result.results == 3
    assert len(result.documents) == 2
    assert result.skipped == 1
    assert result.credits == 1


def test_published_dates_parse_in_both_shapes_and_unknown_stays_none() -> None:
    result = client(ok_server()).search("q", max_results=8, **WINDOW)

    assert result.documents[0].published_at == datetime(2026, 10, 24, 9, 12, 34, tzinfo=UTC)
    assert result.documents[1].published_at == datetime(2026, 10, 24, 11, 40, tzinfo=UTC)


@pytest.mark.parametrize("bad", [None, "", "yesterday-ish", 1234])
def test_an_unparseable_date_is_none_never_now(bad: Any) -> None:
    assert parse_published(bad) is None


def test_a_naive_date_is_read_as_utc() -> None:
    parsed = parse_published("2026-10-24T11:40:00")

    assert parsed is not None and parsed.tzinfo is not None
    assert parsed == datetime(2026, 10, 24, 11, 40, tzinfo=UTC)


def test_an_empty_result_list_is_a_valid_answer_not_an_error() -> None:
    result = client(ok_server({"results": []})).search("q", max_results=8, **WINDOW)

    assert result.documents == [] and result.results == 0
    assert result.credits == 1, "a search that found nothing still cost its credit"


# --- failure handling -------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 432])
def test_a_rate_limit_raises_and_is_never_retried(status: int) -> None:
    """Retrying the limit is how a budget gets spent on failures."""
    server = Server([httpx.Response(status, json={"detail": "limit"})])

    with pytest.raises(TavilyRateLimitError, match=str(status)):
        client(server).search("q", max_results=8, **WINDOW)
    assert len(server.requests) == 1


def test_a_5xx_is_retried_up_to_the_budget_then_raises() -> None:
    server = Server([httpx.Response(503, json={})])

    with pytest.raises(TavilyError, match="3 tries"):
        client(server, max_tries=3).search("q", max_results=8, **WINDOW)
    assert len(server.requests) == 3


def test_a_transient_5xx_recovers_within_the_budget() -> None:
    server = Server([httpx.Response(502, json={}), httpx.Response(200, json=RECORDED)])

    result = client(server).search("q", max_results=8, **WINDOW)

    assert len(server.requests) == 2
    assert result.results == 3


def test_a_connection_error_is_retried() -> None:
    calls: list[int] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError("refused")

    source = TavilySource(api_key="tvly-x", max_tries=2, transport=httpx.MockTransport(refuse))
    with pytest.raises(TavilyError, match="2 tries"):
        source.search("q", max_results=8, **WINDOW)
    assert len(calls) == 2


def test_any_other_4xx_is_an_error_without_a_retry() -> None:
    server = Server([httpx.Response(401, json={"detail": "bad key"})])

    with pytest.raises(TavilyError, match="401"):
        client(server).search("q", max_results=8, **WINDOW)
    assert len(server.requests) == 1


def test_the_key_never_appears_in_any_error() -> None:
    for responses in ([httpx.Response(401, text="no")], [httpx.Response(429)], [httpx.Response(500)]):
        server = Server(responses)
        with pytest.raises(TavilyError) as raised:
            client(server, api_key="tvly-very-secret").search("q", max_results=8, **WINDOW)
        assert "tvly-very-secret" not in str(raised.value)


def test_the_shell_redaction_masks_a_tavily_key_before_it_reaches_a_report() -> None:
    """Invariant 20/21: the same guard the janitor's output goes through."""
    from candlegraph.agents.shell import redact

    assert "tvly-abcd1234efgh" not in redact("TAVILY_API_KEY=tvly-abcd1234efgh")
    assert "tvly-abcd1234efgh" not in redact("key was tvly-abcd1234efgh in the log")
