"""Subgraph response in, GraphQL candles out, through a real database.

The HTTP layer is stubbed so the test is offline and deterministic; everything
below it — paging, upsert, bucketing, serialisation — is the real code path.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from candlegraph.api.app import create_app
from candlegraph.ingestion.runner import ingest_once
from candlegraph.ingestion.subgraph import SubgraphClient
from candlegraph.settings import Settings
from candlegraph.storage import DatabaseManager
from candlegraph.tokens import TokenRegistry
from tests.conftest import SHIB, WBTC, make_settings

pytestmark = pytest.mark.integration

HOUR = 3600
BASE = 1_700_000_000 // (24 * HOUR) * (24 * HOUR)

CHART_QUERY = """
query getChartData($tokenSymbol: String!, $timeUnitInHours: Int!) {
  getChartData(tokenSymbol: $tokenSymbol, timeUnitInHours: $timeUnitInHours) {
    tokenMetadata { id name symbol totalSupply volumeUsd decimals }
    candles { time priceType value }
  }
}
"""

TOKEN_METADATA = [
    {
        "id": WBTC,
        "name": "Wrapped BTC",
        "symbol": "WBTC",
        "totalSupply": "18240",
        "volumeUSD": "238648011451.15",
        "decimals": "8",
    },
    {
        "id": SHIB,
        "name": "SHIBA INU",
        "symbol": "SHIB",
        "totalSupply": "589735030408323",
        "volumeUSD": "12345.67",
        "decimals": "18",
    },
]


def hour_row(token: str, index: int, open_: str, high: str, low: str, close: str, price: str) -> dict:
    period_start = BASE + index * HOUR
    return {
        "id": f"{token}-{period_start // HOUR}",
        "periodStartUnix": period_start,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "priceUSD": price,
    }


# Four hours of WBTC: open 100 on the first hour, close 190 on the last,
# high 200 and low 60 across the window.
WBTC_HOURS = [
    hour_row(WBTC, 0, "100", "150", "80", "120", "110"),
    hour_row(WBTC, 1, "120", "130", "60", "125", "100"),
    hour_row(WBTC, 2, "125", "200", "115", "180", "150"),
    hour_row(WBTC, 3, "180", "185", "170", "190", "180"),
]
SHIB_HOURS = [hour_row(SHIB, 0, "1", "2", "0.5", "1.5", "1.2")]


class FakeSubgraph:
    """An httpx handler that serves the canned rows, with real cursor paging."""

    def __init__(
        self,
        rows: dict[str, list[dict]],
        page_size: int,
        pools: dict[str, list[dict]] | None = None,
        pool_hours: dict[str, list[dict]] | None = None,
    ) -> None:
        self.rows = rows
        self.page_size = page_size
        self.pools = pools or {}
        self.pool_hours = pool_hours or {}
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        variables = body["variables"]
        self.requests.append(variables)

        if "ids" in variables:
            wanted = set(variables["ids"])
            return httpx.Response(
                200, json={"data": {"tokens": [t for t in TOKEN_METADATA if t["id"] in wanted]}}
            )

        # Pool discovery: one token per request.
        if "id" in variables:
            return httpx.Response(
                200, json={"data": {"asToken0": self.pools.get(variables["id"], []), "asToken1": []}}
            )

        if "pool" in variables:
            rows = self.pool_hours.get(variables["pool"], [])
            newer = sorted(
                (r for r in rows if r["periodStartUnix"] > variables["since"]),
                key=lambda r: r["periodStartUnix"],
            )
            return httpx.Response(200, json={"data": {"poolHourDatas": newer[: self.page_size]}})

        token_rows = self.rows.get(variables["token"], [])
        newer = [r for r in token_rows if r["periodStartUnix"] > variables["since"]]
        newer.sort(key=lambda r: r["periodStartUnix"])
        return httpx.Response(200, json={"data": {"tokenHourDatas": newer[: self.page_size]}})


@pytest.fixture
def subgraph() -> FakeSubgraph:
    # Copy the canned rows: tests that append a new hour must not leak it into
    # the next test.
    return FakeSubgraph({WBTC: list(WBTC_HOURS), SHIB: list(SHIB_HOURS)}, page_size=2)


@pytest.fixture
def ingest_settings(tokens_file: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    return make_settings(tokens_file=str(tokens_file), subgraph_page_size=2, lookback_days=3650)


@pytest.fixture
def client(subgraph: FakeSubgraph, ingest_settings: Settings) -> Iterator[SubgraphClient]:
    with SubgraphClient(
        ingest_settings, client=httpx.Client(transport=httpx.MockTransport(subgraph))
    ) as subgraph_client:
        yield subgraph_client


@pytest.fixture
def api(ingest_settings: Settings, db: DatabaseManager) -> Iterator[TestClient]:
    with TestClient(create_app(ingest_settings)) as test_client:
        yield test_client


def run(db: DatabaseManager, client: SubgraphClient, registry: TokenRegistry, settings: Settings) -> Any:
    return ingest_once(db, client, registry, settings, now=datetime(2026, 10, 25, tzinfo=UTC))


# --- ingestion -------------------------------------------------------------


def test_a_run_stores_metadata_and_every_hour(
    db: DatabaseManager, client: SubgraphClient, registry: TokenRegistry, ingest_settings: Settings
) -> None:
    result = run(db, client, registry, ingest_settings)

    assert result.ok
    assert result.tokens_written == 2
    assert result.hours_by_symbol == {"WBTC": 4, "SHIB": 1}

    [row] = db.fetch_all(f'SELECT COUNT(*) AS n FROM "{db.schema}".token_hours_data')
    assert row["n"] == 5


def test_paging_fetches_everything_past_the_page_size(
    db: DatabaseManager,
    client: SubgraphClient,
    registry: TokenRegistry,
    ingest_settings: Settings,
    subgraph: FakeSubgraph,
) -> None:
    """Four WBTC hours at a page size of two must take three requests to drain."""
    run(db, client, registry, ingest_settings)

    wbtc_requests = [v for v in subgraph.requests if v.get("token") == WBTC]
    assert [v["since"] for v in wbtc_requests] == [
        wbtc_requests[0]["since"],
        BASE + HOUR,
        BASE + 3 * HOUR,
    ]


def test_a_second_run_resumes_from_the_stored_cursor(
    db: DatabaseManager,
    client: SubgraphClient,
    registry: TokenRegistry,
    ingest_settings: Settings,
    subgraph: FakeSubgraph,
) -> None:
    run(db, client, registry, ingest_settings)
    subgraph.requests.clear()

    second = run(db, client, registry, ingest_settings)

    assert second.hours_written == 0
    assert [v["since"] for v in subgraph.requests if v.get("token") == WBTC] == [BASE + 3 * HOUR]


def test_re_running_does_not_duplicate_rows(
    db: DatabaseManager, client: SubgraphClient, registry: TokenRegistry, ingest_settings: Settings
) -> None:
    run(db, client, registry, ingest_settings)
    run(db, client, registry, ingest_settings)

    [row] = db.fetch_all(f'SELECT COUNT(*) AS n FROM "{db.schema}".token_hours_data')
    assert row["n"] == 5


def test_a_new_hour_is_picked_up_on_the_next_run(
    db: DatabaseManager,
    client: SubgraphClient,
    registry: TokenRegistry,
    ingest_settings: Settings,
    subgraph: FakeSubgraph,
) -> None:
    run(db, client, registry, ingest_settings)
    subgraph.rows[WBTC].append(hour_row(WBTC, 4, "190", "195", "188", "192", "191"))

    second = run(db, client, registry, ingest_settings)

    assert second.hours_by_symbol["WBTC"] == 1
    latest = {row["token_id"]: row["latest_unix"] for row in db.fetch_all(db.sql.get_latest_timestamps)}
    assert latest[WBTC] == BASE + 4 * HOUR


def test_ingest_survives_one_token_failing(
    db: DatabaseManager, registry: TokenRegistry, ingest_settings: Settings
) -> None:
    def flaky(request: httpx.Request) -> httpx.Response:
        variables = json.loads(request.content)["variables"]
        if variables.get("token") == WBTC:
            return httpx.Response(500, json={"error": "upstream"})
        if "ids" in variables:
            return httpx.Response(200, json={"data": {"tokens": TOKEN_METADATA}})
        rows = [r for r in SHIB_HOURS if r["periodStartUnix"] > variables["since"]]
        return httpx.Response(200, json={"data": {"tokenHourDatas": rows}})

    settings = make_settings(
        tokens_file=str(ingest_settings.tokens_file),
        lookback_days=3650,
        subgraph_max_tries=1,
        ingest_pools=False,
    )
    with SubgraphClient(settings, client=httpx.Client(transport=httpx.MockTransport(flaky))) as client:
        result = run(db, client, registry, settings)

    assert "WBTC" in result.errors
    assert result.hours_by_symbol == {"SHIB": 1}


# --- ingestion through to the API -----------------------------------------


def test_candles_served_over_graphql_match_the_ingested_data(
    db: DatabaseManager,
    client: SubgraphClient,
    registry: TokenRegistry,
    ingest_settings: Settings,
    api: TestClient,
) -> None:
    run(db, client, registry, ingest_settings)

    response = api.post(
        "/graphql",
        json={"query": CHART_QUERY, "variables": {"tokenSymbol": "WBTC", "timeUnitInHours": 4}},
    )

    assert response.status_code == 200
    payload = response.json()["data"]["getChartData"]
    assert payload["tokenMetadata"] == {
        "id": WBTC,
        "name": "Wrapped BTC",
        "symbol": "WBTC",
        "totalSupply": "18240",
        "volumeUsd": "238648011451.15",
        "decimals": "8",
    }

    opens, closes, highs, lows, price_usd = payload["candles"]
    expected_time = datetime.fromtimestamp(BASE, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S")
    assert opens == [{"time": expected_time, "priceType": "open", "value": 100.0}]
    assert closes == [{"time": expected_time, "priceType": "close", "value": 190.0}]
    assert highs[0]["value"] == 200.0
    assert lows[0]["value"] == 60.0
    assert price_usd[0]["value"] == pytest.approx(135.0)


def test_hourly_candles_return_one_point_per_ingested_hour(
    db: DatabaseManager,
    client: SubgraphClient,
    registry: TokenRegistry,
    ingest_settings: Settings,
    api: TestClient,
) -> None:
    run(db, client, registry, ingest_settings)

    response = api.post(
        "/graphql",
        json={"query": CHART_QUERY, "variables": {"tokenSymbol": "WBTC", "timeUnitInHours": 1}},
    )

    opens = response.json()["data"]["getChartData"]["candles"][0]
    assert [point["value"] for point in opens] == [100.0, 120.0, 125.0, 180.0]


def test_two_hour_candles_halve_the_series(
    db: DatabaseManager,
    client: SubgraphClient,
    registry: TokenRegistry,
    ingest_settings: Settings,
    api: TestClient,
) -> None:
    run(db, client, registry, ingest_settings)

    response = api.post(
        "/graphql",
        json={"query": CHART_QUERY, "variables": {"tokenSymbol": "WBTC", "timeUnitInHours": 2}},
    )

    candles = response.json()["data"]["getChartData"]["candles"]
    assert [point["value"] for point in candles[0]] == [100.0, 125.0]
    assert [point["value"] for point in candles[1]] == [125.0, 190.0]


def test_a_token_with_no_rows_yields_the_empty_payload(db: DatabaseManager, api: TestClient) -> None:
    response = api.post(
        "/graphql",
        json={"query": CHART_QUERY, "variables": {"tokenSymbol": "WBTC", "timeUnitInHours": 1}},
    )

    payload = response.json()["data"]["getChartData"]
    assert payload["tokenMetadata"]["id"] == ""
    assert payload["candles"][0] == [{"time": "", "priceType": "open", "value": 0.0}]


def test_status_reports_a_healthy_database(db: DatabaseManager, api: TestClient) -> None:
    response = api.get("/status")

    assert response.status_code == 200
    assert response.json()["database"] == "up"


def test_history_outside_the_lookback_window_survives_a_run(
    db: DatabaseManager, client: SubgraphClient, registry: TokenRegistry, tokens_file: Path
) -> None:
    """A short lookback bounds the first fetch; it must not evict stored rows."""
    run(
        db,
        client,
        registry,
        make_settings(tokens_file=str(tokens_file), lookback_days=3650, subgraph_page_size=2),
    )

    short = make_settings(tokens_file=str(tokens_file), lookback_days=1, subgraph_page_size=2)
    result = ingest_once(db, client, registry, short, now=datetime(2026, 10, 25, tzinfo=UTC))

    assert result.ok
    [row] = db.fetch_all(f'SELECT COUNT(*) AS n FROM "{db.schema}".token_hours_data')
    assert row["n"] == 5


def test_a_backfill_reaches_back_past_what_is_already_stored(
    db: DatabaseManager,
    client: SubgraphClient,
    registry: TokenRegistry,
    ingest_settings: Settings,
    subgraph: FakeSubgraph,
) -> None:
    """Resuming from the newest row only ever walks forwards.

    Without the rewind, `candlegraph backfill --days 90` on a database that
    already holds recent hours fetches nothing at all.
    """
    recent = make_settings(
        tokens_file=str(ingest_settings.tokens_file), lookback_days=1, subgraph_page_size=2
    )
    subgraph.rows[WBTC] = [hour_row(WBTC, i, "1", "2", "0", "1", "1") for i in (100, 101)]
    ingest_once(db, client, registry, recent, now=datetime(2026, 10, 25, tzinfo=UTC))

    # Older history appears upstream; a plain poll must not see it.
    subgraph.rows[WBTC] = [hour_row(WBTC, i, "1", "2", "0", "1", "1") for i in range(102)]
    forward_only = ingest_once(db, client, registry, recent, now=datetime(2026, 10, 25, tzinfo=UTC))
    assert forward_only.hours_by_symbol["WBTC"] == 0

    backfilled = ingest_once(
        db, client, registry, recent, now=datetime(2026, 10, 25, tzinfo=UTC), lookback_days=3650
    )

    assert backfilled.hours_by_symbol["WBTC"] == 102
    [row] = db.fetch_all(
        f'SELECT COUNT(*) AS n FROM "{db.schema}".token_hours_data WHERE token_id = %(t)s',
        {"t": WBTC},
    )
    assert row["n"] == 102


def test_a_backfill_already_covered_does_not_rewind(
    db: DatabaseManager,
    client: SubgraphClient,
    registry: TokenRegistry,
    ingest_settings: Settings,
    subgraph: FakeSubgraph,
) -> None:
    """No point re-reading history the database already has in full."""
    run(db, client, registry, ingest_settings)
    subgraph.requests.clear()

    ingest_once(
        db, client, registry, ingest_settings, now=datetime(2026, 10, 25, tzinfo=UTC), lookback_days=1
    )

    wbtc = [v for v in subgraph.requests if v.get("token") == WBTC]
    assert wbtc[0]["since"] == BASE + 3 * HOUR
