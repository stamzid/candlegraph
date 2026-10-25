"""Pure transforms from subgraph rows into database rows.

No I/O, so this is the cheapest place to test the write path's behaviour.

One rule runs through all of it: a field the subgraph did not send becomes
``None``, never ``0``. "No data collected" and "the value was zero" are
different facts, and conflating them would quietly corrupt any average or sum
computed later.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

# Fields copied straight through from a tokenHourData row.
TOKEN_HOUR_FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "priceUSD",
    "volume",
    "volumeUSD",
    "untrackedVolumeUSD",
    "totalValueLocked",
    "totalValueLockedUSD",
    "feesUSD",
)

# Without these a row is not a usable price bar.
REQUIRED_HOUR_FIELDS = ("open", "high", "low", "close", "priceUSD")

TOKEN_FIELDS = (
    "name",
    "symbol",
    "totalSupply",
    "volumeUSD",
    "decimals",
    "volume",
    "feesUSD",
    "txCount",
    "poolCount",
    "totalValueLocked",
    "totalValueLockedUSD",
    "derivedETH",
)

POOL_FIELDS = (
    "feeTier",
    "createdAtTimestamp",
    "liquidity",
    "volumeUSD",
    "feesUSD",
    "txCount",
    "totalValueLockedUSD",
)

POOL_HOUR_FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "liquidity",
    "sqrtPrice",
    "token0Price",
    "token1Price",
    "tvlUSD",
    "volumeToken0",
    "volumeToken1",
    "volumeUSD",
    "feesUSD",
    "txCount",
)


class RowFormatError(ValueError):
    """A subgraph row is missing fields the database write path requires."""


def split_token_id(composite_id: str) -> str:
    """Return the entity address from a ``<address>-<hour index>`` subgraph id."""
    return composite_id.split("-")[0].lower()


def _period_start(row: dict[str, Any]) -> int:
    try:
        return int(row["periodStartUnix"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RowFormatError(f"row is missing a usable periodStartUnix: {row!r}") from exc


def _pick(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    """Project ``fields`` out of ``row``, defaulting anything absent to None."""
    return {field: row.get(field) for field in fields}


def prepare_hour_rows(rows: list[dict[str, Any]], symbol_by_address: dict[str, str]) -> list[dict[str, Any]]:
    """Turn raw ``tokenHourDatas`` rows into rows the insert statement accepts.

    Replaces the composite ``id`` with the bare token address, attaches the
    configured ``symbol``, and derives a UTC ``timestamp`` from
    ``periodStartUnix``. Rows for tokens outside the registry are dropped: the
    subgraph is not supposed to return them, and inserting them would store a
    symbol we cannot resolve on read.

    Raises:
        RowFormatError: a row has no usable id/``periodStartUnix``, or is
            missing one of the OHLC/price fields.

    Returns:
        A new list; the input rows are not mutated.
    """
    prepared: list[dict[str, Any]] = []
    for row in rows:
        try:
            address = split_token_id(row["id"])
        except (KeyError, TypeError, AttributeError) as exc:
            raise RowFormatError(f"row is missing a usable id/periodStartUnix: {row!r}") from exc
        period_start = _period_start(row)

        symbol = symbol_by_address.get(address)
        if symbol is None:
            continue

        missing = [field for field in REQUIRED_HOUR_FIELDS if field not in row]
        if missing:
            raise RowFormatError(f"row {row['id']!r} is missing price fields: {missing}")

        prepared.append(
            {
                **_pick(row, TOKEN_HOUR_FIELDS),
                "id": address,
                "symbol": symbol,
                "periodStartUnix": period_start,
                "timestamp": datetime.fromtimestamp(period_start, tz=UTC),
            }
        )
    return prepared


def prepare_token_rows(rows: list[dict[str, Any]], now: datetime | None = None) -> list[dict[str, Any]]:
    """Turn ``tokens`` rows into rows the token upsert accepts."""
    stamped = now or datetime.now(tz=UTC)
    prepared: list[dict[str, Any]] = []
    for row in rows:
        token_id = row.get("id")
        if not isinstance(token_id, str):
            raise RowFormatError(f"token row has no id: {row!r}")
        prepared.append({**_pick(row, TOKEN_FIELDS), "id": token_id.lower(), "updated_at": stamped})
    return prepared


def prepare_pool_rows(rows: list[dict[str, Any]], now: datetime | None = None) -> list[dict[str, Any]]:
    """Turn ``pools`` rows into rows the pool upsert accepts.

    Flattens the nested ``token0``/``token1`` objects into plain id and symbol
    columns. A pool whose pair cannot be resolved is dropped rather than
    stored with a null side, since every read joins on those ids.
    """
    stamped = now or datetime.now(tz=UTC)
    prepared: list[dict[str, Any]] = []
    for row in rows:
        pool_id = row.get("id")
        token0 = row.get("token0") or {}
        token1 = row.get("token1") or {}
        if not isinstance(pool_id, str) or not token0.get("id") or not token1.get("id"):
            continue
        prepared.append(
            {
                **_pick(row, POOL_FIELDS),
                "id": pool_id.lower(),
                "token0_id": str(token0["id"]).lower(),
                "token1_id": str(token1["id"]).lower(),
                "token0_symbol": token0.get("symbol"),
                "token1_symbol": token1.get("symbol"),
                "updated_at": stamped,
            }
        )
    return prepared


def prepare_pool_hour_rows(rows: list[dict[str, Any]], pool_id: str) -> list[dict[str, Any]]:
    """Turn ``poolHourDatas`` rows into rows the pool-hour upsert accepts.

    ``pool_id`` is passed in rather than parsed from the composite id so a
    page can never be attributed to the wrong pool.
    """
    wanted = pool_id.lower()
    prepared: list[dict[str, Any]] = []
    for row in rows:
        period_start = _period_start(row)
        composite = row.get("id")
        if isinstance(composite, str) and split_token_id(composite) != wanted:
            raise RowFormatError(f"row {composite!r} does not belong to pool {wanted!r}")
        prepared.append(
            {
                **_pick(row, POOL_HOUR_FIELDS),
                "pool_id": wanted,
                "periodStartUnix": period_start,
                "timestamp": datetime.fromtimestamp(period_start, tz=UTC),
            }
        )
    return prepared
