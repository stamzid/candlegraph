"""Subgraph rows into database rows: symbols, timestamps, and rejection."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from candlegraph.ingestion.transform import RowFormatError, prepare_hour_rows, split_token_id
from tests.conftest import SHIB, WBTC

SYMBOLS = {WBTC: "WBTC", SHIB: "SHIB"}


def hour_row(period_start: int = 1609459200, token: str = WBTC) -> dict[str, str | int]:
    return {
        "id": f"{token}-{period_start // 3600}",
        "periodStartUnix": period_start,
        "open": "500",
        "high": "600",
        "low": "450",
        "close": "550",
        "priceUSD": "525",
    }


def test_split_token_id_drops_the_hour_index() -> None:
    assert split_token_id(f"{WBTC}-489251") == WBTC


def test_split_token_id_lowercases() -> None:
    assert split_token_id(f"{WBTC.upper()}-1") == WBTC


# --- prepare_hour_rows -----------------------------------------------------


def test_prepare_hour_rows_adds_symbol_and_utc_timestamp() -> None:
    [row] = prepare_hour_rows([hour_row(1609459200)], SYMBOLS)

    assert row["id"] == WBTC
    assert row["symbol"] == "WBTC"
    assert row["periodStartUnix"] == 1609459200
    assert row["timestamp"] == datetime(2021, 1, 1, tzinfo=UTC)


def test_prepare_hour_rows_produces_an_aware_timestamp() -> None:
    """Naive timestamps land in the server's zone; the column is timestamptz."""
    [row] = prepare_hour_rows([hour_row()], SYMBOLS)

    assert row["timestamp"].tzinfo is not None


def test_prepare_hour_rows_does_not_mutate_its_input() -> None:
    original = hour_row()
    snapshot = dict(original)

    prepare_hour_rows([original], SYMBOLS)

    assert original == snapshot


def test_prepare_hour_rows_preserves_the_price_fields() -> None:
    [row] = prepare_hour_rows([hour_row()], SYMBOLS)

    assert (row["open"], row["high"], row["low"], row["close"], row["priceUSD"]) == (
        "500",
        "600",
        "450",
        "550",
        "525",
    )


def test_prepare_hour_rows_drops_tokens_outside_the_registry() -> None:
    """Writing a row we cannot resolve a symbol for would corrupt reads."""
    unknown = hour_row(token="0x" + "ab" * 20)

    assert prepare_hour_rows([hour_row(), unknown], SYMBOLS) == prepare_hour_rows([hour_row()], SYMBOLS)


def test_prepare_hour_rows_handles_an_empty_page() -> None:
    assert prepare_hour_rows([], SYMBOLS) == []


def test_prepare_hour_rows_accepts_a_string_period_start() -> None:
    row = hour_row()
    row["periodStartUnix"] = "1609459200"

    assert prepare_hour_rows([row], SYMBOLS)[0]["periodStartUnix"] == 1609459200


@pytest.mark.parametrize("missing", ["id", "periodStartUnix"])
def test_prepare_hour_rows_rejects_rows_without_a_key(missing: str) -> None:
    row = hour_row()
    del row[missing]

    with pytest.raises(RowFormatError, match="missing a usable"):
        prepare_hour_rows([row], SYMBOLS)


@pytest.mark.parametrize("missing", ["open", "high", "low", "close", "priceUSD"])
def test_prepare_hour_rows_rejects_rows_without_a_price(missing: str) -> None:
    row = hour_row()
    del row[missing]

    with pytest.raises(RowFormatError, match=f"missing price fields.*{missing}"):
        prepare_hour_rows([row], SYMBOLS)


def test_prepare_hour_rows_rejects_an_unparseable_period_start() -> None:
    row = hour_row()
    row["periodStartUnix"] = "not-a-number"

    with pytest.raises(RowFormatError):
        prepare_hour_rows([row], SYMBOLS)


# --- enrichment fields -----------------------------------------------------


def test_volume_tvl_and_fee_fields_are_carried_through() -> None:
    row = hour_row()
    row.update(volume="12.5", volumeUSD="1275", totalValueLockedUSD="5e6", feesUSD="3.82")

    [prepared] = prepare_hour_rows([row], SYMBOLS)

    assert prepared["volume"] == "12.5"
    assert prepared["volumeUSD"] == "1275"
    assert prepared["totalValueLockedUSD"] == "5e6"
    assert prepared["feesUSD"] == "3.82"


def test_an_absent_enrichment_field_becomes_none_not_zero() -> None:
    """NULL means "not collected"; 0 would silently skew any later average."""
    [prepared] = prepare_hour_rows([hour_row()], SYMBOLS)

    assert prepared["volume"] is None
    assert prepared["feesUSD"] is None


def test_every_column_the_insert_names_is_present_on_a_prepared_row() -> None:
    """A missing key is a psycopg2 KeyError at write time, not at review time."""
    from candlegraph.storage.statements import TOKEN_HOUR_COLUMNS

    [prepared] = prepare_hour_rows([hour_row()], SYMBOLS)

    # token_id and symbol are supplied as `id`/`symbol` by the template.
    assert len(prepared) == len(TOKEN_HOUR_COLUMNS)


# --- prepare_token_rows ----------------------------------------------------


def test_token_rows_are_lowercased_and_stamped() -> None:
    from candlegraph.ingestion.transform import prepare_token_rows

    now = datetime(2026, 10, 25, tzinfo=UTC)

    [row] = prepare_token_rows([{"id": WBTC.upper(), "symbol": "WBTC", "txCount": "7"}], now)

    assert row["id"] == WBTC
    assert row["txCount"] == "7"
    assert row["updated_at"] == now


def test_a_token_row_without_an_id_is_rejected() -> None:
    from candlegraph.ingestion.transform import prepare_token_rows

    with pytest.raises(RowFormatError, match="no id"):
        prepare_token_rows([{"symbol": "WBTC"}])


def test_token_rows_fill_every_column_the_insert_names() -> None:
    from candlegraph.ingestion.transform import prepare_token_rows
    from candlegraph.storage.statements import TOKEN_COLUMNS

    [row] = prepare_token_rows([{"id": WBTC}])

    assert len(row) == len(TOKEN_COLUMNS)


# --- prepare_pool_rows -----------------------------------------------------


def pool_row(**overrides: object) -> dict:
    base = {
        "id": "0x" + "ab" * 20,
        "feeTier": "3000",
        "liquidity": "1",
        "volumeUSD": "2",
        "feesUSD": "3",
        "txCount": "4",
        "totalValueLockedUSD": "5",
        "token0": {"id": WBTC, "symbol": "WBTC"},
        "token1": {"id": SHIB, "symbol": "SHIB"},
    }
    return {**base, **overrides}


def test_pool_rows_flatten_the_pair_into_columns() -> None:
    from candlegraph.ingestion.transform import prepare_pool_rows

    now = datetime(2026, 10, 25, tzinfo=UTC)

    [row] = prepare_pool_rows([pool_row()], now)

    assert row["token0_id"] == WBTC
    assert row["token1_symbol"] == "SHIB"
    assert row["feeTier"] == "3000"
    assert row["updated_at"] == now


def test_pool_ids_and_token_ids_are_lowercased() -> None:
    from candlegraph.ingestion.transform import prepare_pool_rows

    [row] = prepare_pool_rows([pool_row(id="0x" + "AB" * 20, token0={"id": WBTC.upper()})])

    assert row["id"] == "0x" + "ab" * 20
    assert row["token0_id"] == WBTC


@pytest.mark.parametrize("broken", [{"token0": {}}, {"token1": None}, {"id": None}])
def test_a_pool_missing_a_side_is_dropped(broken: dict) -> None:
    """Every read joins on both ids; a half-stored pool is unusable."""
    from candlegraph.ingestion.transform import prepare_pool_rows

    assert prepare_pool_rows([pool_row(**broken)]) == []


def test_pool_rows_fill_every_column_the_insert_names() -> None:
    from candlegraph.ingestion.transform import prepare_pool_rows
    from candlegraph.storage.statements import POOL_COLUMNS

    [row] = prepare_pool_rows([pool_row()])

    assert len(row) == len(POOL_COLUMNS)


# --- prepare_pool_hour_rows ------------------------------------------------


def pool_hour_row(pool: str, index: int = 1) -> dict:
    return {
        "id": f"{pool}-{index}",
        "periodStartUnix": index * 3600,
        "open": "1",
        "high": "2",
        "low": "0",
        "close": "1.5",
        "tvlUSD": "100",
        "volumeUSD": "10",
        "feesUSD": "0.03",
        "txCount": "5",
    }


def test_pool_hour_rows_take_the_pool_id_from_the_caller() -> None:
    """Trusting the caller's id means a page can never land on the wrong pool."""
    from candlegraph.ingestion.transform import prepare_pool_hour_rows

    pool = "0x" + "cd" * 20

    [row] = prepare_pool_hour_rows([pool_hour_row(pool)], pool)

    assert row["pool_id"] == pool
    assert row["txCount"] == "5"
    assert row["timestamp"] == datetime(1970, 1, 1, 1, tzinfo=UTC)


def test_a_pool_hour_row_from_another_pool_is_rejected() -> None:
    from candlegraph.ingestion.transform import prepare_pool_hour_rows

    with pytest.raises(RowFormatError, match="does not belong to pool"):
        prepare_pool_hour_rows([pool_hour_row("0x" + "ef" * 20)], "0x" + "cd" * 20)


def test_pool_hour_rows_fill_every_column_the_insert_names() -> None:
    from candlegraph.ingestion.transform import prepare_pool_hour_rows
    from candlegraph.storage.statements import POOL_HOUR_COLUMNS

    pool = "0x" + "cd" * 20

    [row] = prepare_pool_hour_rows([pool_hour_row(pool)], pool)

    assert len(row) == len(POOL_HOUR_COLUMNS)
