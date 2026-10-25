"""Reshaping aggregated database rows into the GraphQL chart payload."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

PRICE_TYPES = ("open", "close", "high", "low", "priceUSD")
TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"


def to_utc_iso(moment: datetime) -> str:
    """Format a datetime as a naive-looking UTC ISO string.

    Naive input is assumed to already be UTC, which is how the database hands
    back timestamps given the pool pins ``timezone=UTC``.
    """
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC)
    return moment.strftime(TIME_FORMAT)


def _as_float(value: Any) -> float:
    """Coerce a database numeric (``Decimal``, ``str``, ``None``) to a float."""
    if value is None:
        return 0.0
    if isinstance(value, Decimal | float | int):
        return float(value)
    return float(str(value))


def format_chart_data(data: list[dict[str, Any]]) -> list[list[list[Any]]]:
    """Reshape aggregated bucket rows into the five chart series.

    Args:
        data: Rows from the chart query, each holding ``interval_start``,
            ``opens`` and ``closes`` arrays, ``max_high``, ``min_low``, and
            ``avg_price_usd``.

    Returns:
        One list per entry in :data:`PRICE_TYPES`, in that order, each a list
        of ``[iso_time, price_type, value]`` sorted by time.
    """
    series: dict[str, list[list[Any]]] = {price_type: [] for price_type in PRICE_TYPES}

    for record in data:
        time_str = to_utc_iso(record["interval_start"])
        opens = record.get("opens") or []
        closes = record.get("closes") or []
        values = {
            # `opens` is ordered ascending and `closes` descending, so the
            # first element of each is the bucket's opening and closing price.
            "open": opens[0] if opens else 0,
            "close": closes[0] if closes else 0,
            "high": record.get("max_high", 0),
            "low": record.get("min_low", 0),
            "priceUSD": record.get("avg_price_usd", 0),
        }
        for price_type in PRICE_TYPES:
            series[price_type].append([time_str, price_type, _as_float(values[price_type])])

    return [series[price_type] for price_type in PRICE_TYPES]


def empty_chart_data() -> list[list[list[Any]]]:
    """The placeholder payload returned when a token has no stored rows."""
    return [[["", price_type, 0.0]] for price_type in PRICE_TYPES]
