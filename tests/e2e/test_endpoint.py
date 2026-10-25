"""Smoke tests against a running stack.

Skipped unless ``CANDLEGRAPH_E2E_URL`` points at a deployment, so ``pytest``
stays useful without Docker. ``make e2e`` brings the compose stack up, waits
for it to be healthy, and sets the variable.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import httpx
import pytest

pytestmark = pytest.mark.e2e

CHART_QUERY = """
query getChartData($tokenSymbol: String!, $timeUnitInHours: Int!) {
  getChartData(tokenSymbol: $tokenSymbol, timeUnitInHours: $timeUnitInHours) {
    tokenMetadata { id name symbol totalSupply volumeUsd decimals }
    candles { time priceType value }
  }
}
"""

PRICE_TYPES = ["open", "close", "high", "low", "priceUSD"]


@pytest.fixture(scope="module")
def base_url() -> str:
    url = os.environ.get("CANDLEGRAPH_E2E_URL")
    if not url:
        pytest.skip("set CANDLEGRAPH_E2E_URL to run the end-to-end tests")
    return url.rstrip("/")


@pytest.fixture(scope="module")
def api(base_url: str) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        yield client


def graphql(api: httpx.Client, query: str, **variables: object) -> dict:
    response = api.post("/graphql", json={"query": query, "variables": variables})
    response.raise_for_status()
    body = response.json()
    assert "errors" not in body, body["errors"]
    return body["data"]


def test_status_is_healthy(api: httpx.Client) -> None:
    response = api.get("/status")

    assert response.status_code == 200
    assert response.json()["database"] == "up"


def test_the_configured_tokens_are_served(api: httpx.Client) -> None:
    data = graphql(api, "{ supportedTokens }")

    assert data["supportedTokens"] == ["WBTC", "SHIB", "GNO"]


@pytest.mark.parametrize("symbol", ["WBTC", "SHIB", "GNO"])
def test_each_token_has_ingested_candles(api: httpx.Client, symbol: str) -> None:
    """Proves the live subgraph ingest actually landed data for every token."""
    payload = graphql(api, CHART_QUERY, tokenSymbol=symbol, timeUnitInHours=1)["getChartData"]

    assert payload["tokenMetadata"]["symbol"] == symbol
    assert payload["tokenMetadata"]["decimals"] != ""
    assert [series[0]["priceType"] for series in payload["candles"]] == PRICE_TYPES
    assert len(payload["candles"][0]) > 1, f"no candles ingested for {symbol}"
    assert all(point["value"] > 0 for point in payload["candles"][0])


def test_wider_candles_aggregate_the_hourly_rows(api: httpx.Client) -> None:
    hourly = graphql(api, CHART_QUERY, tokenSymbol="WBTC", timeUnitInHours=1)["getChartData"]
    four_hourly = graphql(api, CHART_QUERY, tokenSymbol="WBTC", timeUnitInHours=4)["getChartData"]

    assert len(four_hourly["candles"][0]) < len(hourly["candles"][0])


def test_high_is_never_below_low(api: httpx.Client) -> None:
    payload = graphql(api, CHART_QUERY, tokenSymbol="WBTC", timeUnitInHours=6)["getChartData"]
    _, _, highs, lows, _ = payload["candles"]

    assert all(high["value"] >= low["value"] for high, low in zip(highs, lows, strict=True))


def test_an_unknown_token_returns_the_empty_payload(api: httpx.Client) -> None:
    payload = graphql(api, CHART_QUERY, tokenSymbol="EMPTYTOKEN", timeUnitInHours=1)["getChartData"]

    assert payload["tokenMetadata"]["id"] == ""
    assert payload["candles"][0] == [{"time": "", "priceType": "open", "value": 0.0}]


def test_graphiql_is_available(api: httpx.Client) -> None:
    response = api.get("/graphql", headers={"accept": "text/html"})

    assert response.status_code == 200
