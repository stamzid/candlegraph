"""GraphQL resolvers, exercised through the schema with a fake database."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from candlegraph.dba import set_db
from candlegraph.schema import (
    MAX_TIME_UNIT_IN_HOURS,
    build_chart_data,
    chart_schema,
    fetch_chart_data,
    fetch_token_metadata,
)
from candlegraph.tokens import TokenRegistry
from tests.conftest import WBTC, FakeDatabase

CHART_QUERY = """
query getChartData($tokenSymbol: String!, $timeUnitInHours: Int!) {
  getChartData(tokenSymbol: $tokenSymbol, timeUnitInHours: $timeUnitInHours) {
    tokenMetadata { id name symbol totalSupply volumeUsd decimals }
    candles { time priceType value }
  }
}
"""

METADATA_ROW = {
    "id": WBTC,
    "name": "Wrapped BTC",
    "symbol": "WBTC",
    "total_supply": "18240",
    "volume_usd": "238648011451.15",
    "decimals": "8",
}


def chart_rows(*starts: datetime) -> list[dict[str, Any]]:
    return [
        {
            "interval_start": start,
            "opens": ["500"],
            "closes": ["550"],
            "max_high": Decimal("600"),
            "min_low": Decimal("450"),
            "avg_price_usd": Decimal("525"),
            "hours": 1,
        }
        for start in starts
    ]


@pytest.fixture
def db_with_data(tokens_file: Path, monkeypatch: pytest.MonkeyPatch) -> FakeDatabase:
    """A fake database holding one WBTC bucket and its metadata, wired in globally."""
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    db = FakeDatabase(
        chart_query=chart_rows(datetime(2024, 5, 7, 23, tzinfo=UTC)),
        get_token_metadata=[METADATA_ROW],
    )
    set_db(db)
    return db


@pytest.fixture
def empty_db(tokens_file: Path, monkeypatch: pytest.MonkeyPatch) -> FakeDatabase:
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    db = FakeDatabase()
    set_db(db)
    return db


# --- fetch helpers ---------------------------------------------------------


def test_fetch_chart_data_converts_hours_to_seconds(registry: TokenRegistry) -> None:
    db = FakeDatabase()

    fetch_chart_data("WBTC", 4, db=db, registry=registry)

    [(label, params)] = db.fetches
    assert label == "chart_query"
    assert params == {"interval": 4 * 3600, "token_id": WBTC, "start_unix": None, "end_unix": None}


def test_fetch_chart_data_skips_the_query_for_an_unknown_symbol(registry: TokenRegistry) -> None:
    db = FakeDatabase()

    assert fetch_chart_data("NOPE", 1, db=db, registry=registry) == []
    assert db.fetches == []


def test_fetch_token_metadata_returns_the_single_row(registry: TokenRegistry) -> None:
    db = FakeDatabase(get_token_metadata=[METADATA_ROW])

    assert fetch_token_metadata("WBTC", db=db, registry=registry) == METADATA_ROW


def test_fetch_token_metadata_is_empty_before_the_first_ingest(registry: TokenRegistry) -> None:
    assert fetch_token_metadata("WBTC", db=FakeDatabase(), registry=registry) == {}


def test_fetch_token_metadata_skips_the_query_for_an_unknown_symbol(registry: TokenRegistry) -> None:
    db = FakeDatabase()

    assert fetch_token_metadata("NOPE", db=db, registry=registry) == {}
    assert db.fetches == []


# --- build_chart_data ------------------------------------------------------


def test_build_chart_data_returns_five_series(db_with_data: FakeDatabase) -> None:
    result = build_chart_data("WBTC", 1)

    assert [series[0].priceType for series in result.candles] == [
        "open",
        "close",
        "high",
        "low",
        "priceUSD",
    ]
    assert result.tokenMetadata.symbol == "WBTC"
    assert result.tokenMetadata.totalSupply == "18240"


def test_build_chart_data_is_case_insensitive(db_with_data: FakeDatabase) -> None:
    assert build_chart_data("wbtc", 1).tokenMetadata.id == WBTC


def test_build_chart_data_returns_a_zero_placeholder_without_rows(empty_db: FakeDatabase) -> None:
    result = build_chart_data("WBTC", 1)

    assert len(result.candles) == 5
    assert all(len(series) == 1 and series[0].value == 0.0 for series in result.candles)
    assert result.tokenMetadata.id == ""


@pytest.mark.parametrize("hours", [0, -1, MAX_TIME_UNIT_IN_HOURS + 1])
def test_build_chart_data_rejects_an_unusable_interval(empty_db: FakeDatabase, hours: int) -> None:
    """Zero would divide by zero in SQL; a huge value is never a real request."""
    with pytest.raises(ValueError, match="timeUnitInHours"):
        build_chart_data("WBTC", hours)


def test_build_chart_data_tolerates_null_metadata_columns(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    set_db(FakeDatabase(get_token_metadata=[{"id": WBTC, "name": None, "symbol": "WBTC"}]))

    metadata = build_chart_data("WBTC", 1).tokenMetadata

    assert metadata.name == ""
    assert metadata.decimals == ""


# --- through the schema ----------------------------------------------------


def test_the_graphql_query_returns_candles(db_with_data: FakeDatabase) -> None:
    result = chart_schema.execute_sync(
        CHART_QUERY, variable_values={"tokenSymbol": "WBTC", "timeUnitInHours": 2}
    )

    assert result.errors is None
    assert result.data is not None
    payload = result.data["getChartData"]
    assert payload["tokenMetadata"]["symbol"] == "WBTC"
    assert len(payload["candles"]) == 5
    assert payload["candles"][0][0] == {
        "time": "2024-05-07T23:00:00",
        "priceType": "open",
        "value": 500.0,
    }


def test_the_graphql_query_handles_an_unknown_token(empty_db: FakeDatabase) -> None:
    result = chart_schema.execute_sync(
        CHART_QUERY, variable_values={"tokenSymbol": "EMPTYTOKEN", "timeUnitInHours": 1}
    )

    assert result.errors is None
    assert result.data is not None
    payload = result.data["getChartData"]
    assert payload["tokenMetadata"]["id"] == ""
    assert payload["candles"][0][0]["value"] == 0


def test_an_invalid_interval_surfaces_as_a_graphql_error(empty_db: FakeDatabase) -> None:
    result = chart_schema.execute_sync(
        CHART_QUERY, variable_values={"tokenSymbol": "WBTC", "timeUnitInHours": 0}
    )

    assert result.errors is not None
    assert "timeUnitInHours" in str(result.errors[0])


def test_supported_tokens_lists_the_configured_symbols(empty_db: FakeDatabase) -> None:
    result = chart_schema.execute_sync("{ supportedTokens }")

    assert result.errors is None
    assert result.data == {"supportedTokens": ["WBTC", "SHIB"]}


def test_the_published_sdl_keeps_the_documented_field_names() -> None:
    """Renaming these silently breaks every client; pin them."""
    sdl = str(chart_schema)

    for fragment in (
        "getChartData(",
        "tokenSymbol: String!",
        "timeUnitInHours: Int!",
        "tokenMetadata: TokenMetadata!",
        "candles: [[Candle!]!]!",
        "priceType: String!",
        "volumeUsd: String!",
    ):
        assert fragment in sdl
