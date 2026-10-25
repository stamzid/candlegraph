"""The GraphQL surface: one query returning token metadata plus OHLC candles.

Candles of any width are built at read time by grouping the stored hourly rows
into ``timeUnitInHours`` buckets.
"""

from __future__ import annotations

from typing import Any

import strawberry

from candlegraph.dba import DatabaseManager, get_db
from candlegraph.helpers import empty_chart_data, format_chart_data
from candlegraph.tokens import TokenRegistry, get_token_registry

MAX_TIME_UNIT_IN_HOURS = 24 * 366


def fetch_chart_data(
    token_symbol: str,
    time_unit_in_hours: int,
    db: DatabaseManager | None = None,
    registry: TokenRegistry | None = None,
) -> list[dict[str, Any]]:
    """Aggregated buckets for a token, or an empty list for an unknown symbol."""
    registry = registry or get_token_registry()
    token = registry.by_symbol(token_symbol)
    if token is None:
        return []

    db = db or get_db()
    params = {
        "interval": time_unit_in_hours * 3600,
        "token_id": token.address,
        "start_unix": None,
        "end_unix": None,
    }
    return db.fetch_all(db.sql.chart_query, params)


def fetch_token_metadata(
    token_symbol: str,
    db: DatabaseManager | None = None,
    registry: TokenRegistry | None = None,
) -> dict[str, Any]:
    """Stored metadata for a token, or an empty dict when unknown/not yet ingested."""
    registry = registry or get_token_registry()
    token = registry.by_symbol(token_symbol)
    if token is None:
        return {}

    db = db or get_db()
    rows = db.fetch_all(db.sql.get_token_metadata, {"token_id": token.address})
    return rows[0] if rows else {}


@strawberry.type
class Candle:
    """One point of one price series."""

    time: str
    priceType: str
    value: float


@strawberry.type
class TokenMetadata:
    """Token facts as reported by the subgraph."""

    id: str
    name: str
    symbol: str
    totalSupply: str
    volumeUsd: str
    decimals: str


@strawberry.type
class ChartData:
    """Metadata plus one candle list per price type."""

    tokenMetadata: TokenMetadata
    candles: list[list[Candle]]


def _metadata_type(record: dict[str, Any]) -> TokenMetadata:
    return TokenMetadata(
        id=str(record.get("id") or ""),
        name=str(record.get("name") or ""),
        symbol=str(record.get("symbol") or ""),
        totalSupply=str(record.get("total_supply") or ""),
        volumeUsd=str(record.get("volume_usd") or ""),
        decimals=str(record.get("decimals") or ""),
    )


def build_chart_data(token_symbol: str, time_unit_in_hours: int) -> ChartData:
    """Resolver body, kept outside the Strawberry type so tests can call it."""
    if time_unit_in_hours < 1 or time_unit_in_hours > MAX_TIME_UNIT_IN_HOURS:
        raise ValueError(f"timeUnitInHours must be between 1 and {MAX_TIME_UNIT_IN_HOURS}")

    rows = fetch_chart_data(token_symbol, time_unit_in_hours)
    formatted = format_chart_data(rows) if rows else empty_chart_data()
    metadata = fetch_token_metadata(token_symbol)

    return ChartData(
        tokenMetadata=_metadata_type(metadata),
        candles=[
            [Candle(time=str(point[0]), priceType=str(point[1]), value=float(point[2])) for point in group]
            for group in formatted
        ],
    )


@strawberry.type
class Query:
    """Root query type."""

    @strawberry.field
    def get_chart_data(self, token_symbol: str, time_unit_in_hours: int) -> ChartData:
        """Token metadata plus OHLC + USD price series bucketed to the given width.

        Args:
            token_symbol: A symbol from the configured tokens file, case-insensitive.
            time_unit_in_hours: Candle width in hours; 1 returns the stored rows.

        Returns:
            Five series (open, close, high, low, priceUSD), each sorted by time.
            Unknown symbols yield empty metadata and a single zero-valued point.
        """
        return build_chart_data(token_symbol, time_unit_in_hours)

    @strawberry.field
    def supported_tokens(self) -> list[str]:
        """The symbols this deployment tracks."""
        return list(get_token_registry().symbols)


chart_schema = strawberry.Schema(query=Query)
