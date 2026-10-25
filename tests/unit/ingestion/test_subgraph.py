"""The subgraph client: auth, retry policy, and the paging loop.

The paging tests matter most. The previous client swallowed every exception
inside its loop and only stopped on an empty page, so a dead endpoint produced
a full-speed error loop that never let the server start.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from candlegraph.ingestion.subgraph import SubgraphClient, SubgraphError
from tests.conftest import SHIB, WBTC, make_settings


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep exponential backoff from actually sleeping during the tests."""
    monkeypatch.setattr("time.sleep", lambda _seconds: None)


def hour_rows(start_index: int, count: int) -> list[dict[str, Any]]:
    return [
        {
            "id": f"{WBTC}-{start_index + i}",
            "periodStartUnix": (start_index + i) * 3600,
            "open": "1",
            "close": "2",
            "high": "3",
            "low": "0",
            "priceUSD": "2",
        }
        for i in range(count)
    ]


def client_for(handler: Any, **overrides: Any) -> SubgraphClient:
    settings = make_settings(**overrides)
    return SubgraphClient(settings, client=httpx.Client(transport=httpx.MockTransport(handler)))


def responder(*payloads: Any, status: int = 200) -> tuple[Any, list[httpx.Request]]:
    """A handler replaying ``payloads`` in order, plus the recorded requests."""
    seen: list[httpx.Request] = []
    queue = list(payloads)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(body, int):
            return httpx.Response(body, json={"error": "boom"})
        return httpx.Response(status, json=body)

    return handler, seen


# --- construction ----------------------------------------------------------


def test_requires_an_api_key_or_an_explicit_endpoint() -> None:
    with pytest.raises(SubgraphError, match="GRAPH_API_KEY"):
        SubgraphClient(make_settings(graph_api_key=None, transport_url=None))


def test_an_explicit_transport_url_needs_no_api_key() -> None:
    client = SubgraphClient(make_settings(graph_api_key=None, transport_url="http://local/graphql"))

    assert client.url == "http://local/graphql"
    client.close()


def test_posts_to_the_gateway_with_a_bearer_token() -> None:
    handler, seen = responder({"data": {"tokens": []}})
    with client_for(handler, graph_api_key="key-123", subgraph_id="sub-9") as client:
        client.fetch_tokens([WBTC])

    assert str(seen[0].url) == "https://gateway.thegraph.com/api/subgraphs/id/sub-9"
    assert seen[0].headers["authorization"] == "Bearer key-123"


def test_closing_is_skipped_for_an_injected_client() -> None:
    """The caller owns a client it passed in."""
    handler, _ = responder({"data": {}})
    injected = httpx.Client(transport=httpx.MockTransport(handler))

    SubgraphClient(make_settings(), client=injected).close()

    assert not injected.is_closed
    injected.close()


# --- query construction ----------------------------------------------------


def test_queries_use_graphql_variables_not_interpolation() -> None:
    """String-built queries let a token id change the query's meaning."""
    handler, seen = responder({"data": {"tokenHourDatas": []}})
    with client_for(handler, subgraph_page_size=42) as client:
        client.fetch_token_hour_page(WBTC, 1700000000)

    body = json.loads(seen[0].content)
    assert body["variables"] == {"token": WBTC, "since": 1700000000, "first": 42}
    assert WBTC not in body["query"]
    assert "orderBy: periodStartUnix" in body["query"]


def test_fetch_tokens_sends_the_address_list() -> None:
    handler, seen = responder({"data": {"tokens": [{"id": WBTC, "symbol": "WBTC"}]}})
    with client_for(handler) as client:
        rows = client.fetch_tokens([WBTC])

    assert json.loads(seen[0].content)["variables"] == {"ids": [WBTC]}
    assert rows == [{"id": WBTC, "symbol": "WBTC"}]


def test_fetch_tokens_skips_the_request_for_an_empty_list() -> None:
    handler, seen = responder({"data": {"tokens": []}})
    with client_for(handler) as client:
        assert client.fetch_tokens([]) == []

    assert seen == []


# --- error handling --------------------------------------------------------


def test_graphql_errors_raise() -> None:
    handler, _ = responder({"errors": [{"message": "bad field"}]})
    with client_for(handler) as client, pytest.raises(SubgraphError, match="bad field"):
        client.fetch_tokens([WBTC])


def test_a_response_without_a_data_object_raises() -> None:
    handler, _ = responder({"not_data": 1})
    with client_for(handler) as client, pytest.raises(SubgraphError, match="no data object"):
        client.fetch_tokens([WBTC])


def test_a_non_list_result_raises() -> None:
    handler, _ = responder({"data": {"tokens": {"id": WBTC}}})
    with client_for(handler) as client, pytest.raises(SubgraphError, match="expected a list"):
        client.fetch_tokens([WBTC])


def test_a_row_without_a_period_start_raises() -> None:
    handler, _ = responder({"data": {"tokenHourDatas": [{"id": "x", "open": "1"}]}})
    with client_for(handler) as client, pytest.raises(SubgraphError, match="periodStartUnix"):
        client.fetch_token_hour_page(WBTC, 0)


def test_a_redirect_is_reported_rather_than_followed() -> None:
    """The legacy hosted endpoint answers 301; that must surface as an error."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"location": "https://elsewhere.example/"})

    with client_for(handler) as client, pytest.raises(SubgraphError, match="failed"):
        client.fetch_tokens([WBTC])


# --- retry policy ----------------------------------------------------------


def test_retries_a_503_and_then_succeeds() -> None:
    handler, seen = responder(503, {"data": {"tokens": []}}, status=200)
    with client_for(handler, subgraph_max_tries=3) as client:
        assert client.fetch_tokens([WBTC]) == []

    assert len(seen) == 2


def test_gives_up_after_max_tries() -> None:
    handler, seen = responder(503)
    with client_for(handler, subgraph_max_tries=3) as client, pytest.raises(SubgraphError):
        client.fetch_tokens([WBTC])

    assert len(seen) == 3


def test_does_not_retry_a_client_error() -> None:
    """A 400 is our bug; retrying only burns the rate limit."""
    handler, seen = responder(400)
    with client_for(handler, subgraph_max_tries=5) as client, pytest.raises(SubgraphError):
        client.fetch_tokens([WBTC])

    assert len(seen) == 1


def test_retries_a_transport_error() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 2:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={"data": {"tokens": []}})

    with client_for(handler, subgraph_max_tries=3) as client:
        assert client.fetch_tokens([WBTC]) == []

    assert len(attempts) == 2


# --- paging ----------------------------------------------------------------


def test_iterating_an_empty_result_yields_nothing() -> None:
    handler, seen = responder({"data": {"tokenHourDatas": []}})
    with client_for(handler, subgraph_page_size=2) as client:
        assert list(client.iter_token_hour_datas(WBTC, 0)) == []

    assert len(seen) == 1


def test_iterating_stops_on_a_short_page() -> None:
    """A page smaller than the page size means the subgraph has nothing more."""
    handler, seen = responder({"data": {"tokenHourDatas": hour_rows(1, 1)}})
    with client_for(handler, subgraph_page_size=2) as client:
        pages = list(client.iter_token_hour_datas(WBTC, 0))

    assert len(pages) == 1
    assert len(seen) == 1


def test_iterating_follows_the_cursor_across_pages() -> None:
    handler, seen = responder(
        {"data": {"tokenHourDatas": hour_rows(1, 2)}},
        {"data": {"tokenHourDatas": hour_rows(3, 2)}},
        {"data": {"tokenHourDatas": []}},
    )
    with client_for(handler, subgraph_page_size=2) as client:
        pages = list(client.iter_token_hour_datas(WBTC, 0))

    assert [page.last_timestamp for page in pages] == [2 * 3600, 4 * 3600]
    assert [json.loads(r.content)["variables"]["since"] for r in seen] == [0, 2 * 3600, 4 * 3600]


def test_a_page_that_does_not_advance_the_cursor_raises() -> None:
    """Otherwise this loops forever at full speed, which is the bug being fixed."""
    stuck = {"data": {"tokenHourDatas": hour_rows(1, 2)}}
    handler, seen = responder(stuck)

    with client_for(handler, subgraph_page_size=2) as client:
        pages = client.iter_token_hour_datas(WBTC, 0)
        next(pages)
        with pytest.raises(SubgraphError, match="did not advance"):
            next(pages)

    assert len(seen) == 2


def test_paging_stops_at_the_configured_page_limit() -> None:
    counter = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        return httpx.Response(200, json={"data": {"tokenHourDatas": hour_rows(counter["n"], 2)}})

    with (
        client_for(handler, subgraph_page_size=2, subgraph_max_pages=3) as client,
        pytest.raises(SubgraphError, match="SUBGRAPH_MAX_PAGES=3"),
    ):
        list(client.iter_token_hour_datas(WBTC, 0))

    assert counter["n"] == 3


def test_a_failing_page_propagates_instead_of_being_swallowed() -> None:
    handler, _ = responder({"data": {"tokenHourDatas": hour_rows(1, 2)}}, 500)
    with client_for(handler, subgraph_page_size=2, subgraph_max_tries=1) as client:
        pages = client.iter_token_hour_datas(WBTC, 0)
        next(pages)
        with pytest.raises(SubgraphError):
            next(pages)


# --- pools -----------------------------------------------------------------

POOL_A = "0x" + "aa" * 20
POOL_B = "0x" + "bb" * 20


def pool(pool_id: str, tvl: str = "1") -> dict[str, Any]:
    return {
        "id": pool_id,
        "feeTier": "3000",
        "totalValueLockedUSD": tvl,
        "token0": {"id": WBTC, "symbol": "WBTC"},
        "token1": {"id": "0x" + "cc" * 20, "symbol": "WETH"},
    }


def test_pool_discovery_queries_both_sides_of_the_pair() -> None:
    handler, seen = responder({"data": {"asToken0": [pool(POOL_A)], "asToken1": [pool(POOL_B)]}})
    with client_for(handler) as client:
        pools = client.fetch_pools_for_tokens([WBTC], first=3)

    body = json.loads(seen[0].content)
    assert body["variables"] == {"id": WBTC, "first": 3}
    assert "token0: $id" in body["query"]
    assert "token1: $id" in body["query"]
    assert {p["id"] for p in pools} == {POOL_A, POOL_B}


def test_each_token_gets_its_own_ranking() -> None:
    """One `token0_in` query with `first: N` lets a liquid token starve the rest."""
    handler, seen = responder({"data": {"asToken0": [pool(POOL_A)], "asToken1": []}})
    with client_for(handler) as client:
        client.fetch_pools_for_tokens([WBTC, SHIB], first=3)

    assert [json.loads(r.content)["variables"]["id"] for r in seen] == [WBTC, SHIB]


def test_a_pool_shared_by_two_tokens_is_stored_once() -> None:
    handler, _ = responder({"data": {"asToken0": [pool(POOL_A)], "asToken1": []}})
    with client_for(handler) as client:
        assert len(client.fetch_pools_for_tokens([WBTC, SHIB], first=3)) == 1


def test_a_pool_matching_both_sides_is_returned_once() -> None:
    """A pool can appear in both halves of the query; storing it twice is wrong."""
    handler, _ = responder({"data": {"asToken0": [pool(POOL_A)], "asToken1": [pool(POOL_A)]}})
    with client_for(handler) as client:
        assert len(client.fetch_pools_for_tokens([WBTC], first=3)) == 1


def test_pool_discovery_is_skipped_for_no_tokens() -> None:
    handler, seen = responder({"data": {"asToken0": [], "asToken1": []}})
    with client_for(handler) as client:
        assert client.fetch_pools_for_tokens([], first=3) == []

    assert seen == []


def test_pool_discovery_is_skipped_when_disabled_by_count() -> None:
    """`POOLS_PER_TOKEN=0` must not issue a request at all."""
    handler, seen = responder({"data": {"asToken0": [], "asToken1": []}})
    with client_for(handler) as client:
        assert client.fetch_pools_for_tokens([WBTC], first=0) == []

    assert seen == []


def test_pool_hour_pages_follow_the_same_cursor_rule() -> None:
    def pool_hours(start: int, count: int) -> list[dict[str, Any]]:
        return [
            {"id": f"{POOL_A}-{start + i}", "periodStartUnix": (start + i) * 3600, "txCount": "1"}
            for i in range(count)
        ]

    handler, seen = responder(
        {"data": {"poolHourDatas": pool_hours(1, 2)}},
        {"data": {"poolHourDatas": pool_hours(3, 1)}},
    )
    with client_for(handler, subgraph_page_size=2) as client:
        pages = list(client.iter_pool_hour_datas(POOL_A, 0))

    assert [page.last_timestamp for page in pages] == [2 * 3600, 3 * 3600]
    assert [json.loads(r.content)["variables"]["pool"] for r in seen] == [POOL_A, POOL_A]


def test_a_stuck_pool_cursor_raises_too() -> None:
    """The anti-hot-loop rule is shared, so it must hold for pools as well."""
    stuck = {
        "data": {
            "poolHourDatas": [
                {"id": f"{POOL_A}-1", "periodStartUnix": 3600},
                {"id": f"{POOL_A}-2", "periodStartUnix": 7200},
            ]
        }
    }
    handler, _ = responder(stuck)

    with client_for(handler, subgraph_page_size=2) as client:
        pages = client.iter_pool_hour_datas(POOL_A, 0)
        next(pages)
        with pytest.raises(SubgraphError, match="did not advance"):
            next(pages)


def test_the_token_hour_query_asks_for_the_enrichment_fields() -> None:
    """Dropping a field here silently stops populating a column."""
    handler, seen = responder({"data": {"tokenHourDatas": []}})
    with client_for(handler) as client:
        client.fetch_token_hour_page(WBTC, 0)

    query = json.loads(seen[0].content)["query"]
    for field in ("volume", "volumeUSD", "totalValueLocked", "totalValueLockedUSD", "feesUSD"):
        assert field in query


def test_the_pool_hour_query_asks_for_tx_count() -> None:
    """Pool data is the only source of transaction counts in this subgraph."""
    handler, seen = responder({"data": {"poolHourDatas": []}})
    with client_for(handler) as client:
        client.fetch_pool_hour_page(POOL_A, 0)

    query = json.loads(seen[0].content)["query"]
    assert "txCount" in query
    assert "tvlUSD" in query
