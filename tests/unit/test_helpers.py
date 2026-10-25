"""Pure row/payload transforms, including the timezone and gap behaviour."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from candlegraph.helpers import (
    PRICE_TYPES,
    RowFormatError,
    empty_chart_data,
    format_chart_data,
    prepare_hour_rows,
    split_token_id,
    to_utc_iso,
)
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

    with pytest.raises(RowFormatError, match="id/periodStartUnix"):
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


# --- to_utc_iso ------------------------------------------------------------


def test_to_utc_iso_formats_utc() -> None:
    assert to_utc_iso(datetime(2024, 5, 7, 23, 0, tzinfo=UTC)) == "2024-05-07T23:00:00"


def test_to_utc_iso_converts_other_offsets() -> None:
    """A connection that was not pinned to UTC must not shift reported times."""
    plus_two = timezone(timedelta(hours=2))

    assert to_utc_iso(datetime(2024, 5, 8, 1, 0, tzinfo=plus_two)) == "2024-05-07T23:00:00"


def test_to_utc_iso_treats_naive_input_as_utc() -> None:
    assert to_utc_iso(datetime(2024, 5, 7, 23, 0)) == "2024-05-07T23:00:00"


# --- format_chart_data -----------------------------------------------------


def bucket(start: datetime, opens: list[str], closes: list[str], high: str, low: str, avg: str) -> dict:
    return {
        "interval_start": start,
        "opens": opens,
        "closes": closes,
        "max_high": Decimal(high),
        "min_low": Decimal(low),
        "avg_price_usd": Decimal(avg),
        "hours": len(opens),
    }


def test_format_chart_data_returns_the_five_series_in_order() -> None:
    rows = [bucket(datetime(2024, 5, 7, 23, tzinfo=UTC), ["500"], ["550"], "600", "450", "525")]

    result = format_chart_data(rows)

    assert len(result) == len(PRICE_TYPES)
    assert [series[0][1] for series in result] == list(PRICE_TYPES)


def test_format_chart_data_picks_first_open_and_last_close() -> None:
    """`opens` arrives ascending and `closes` descending, so index 0 is right."""
    rows = [
        bucket(
            datetime(2024, 5, 7, 23, tzinfo=UTC),
            ["500", "510", "520"],
            ["570", "560", "550"],
            "600",
            "450",
            "525",
        )
    ]

    opens, closes, highs, lows, price_usd = format_chart_data(rows)

    assert opens[0] == ["2024-05-07T23:00:00", "open", 500.0]
    assert closes[0] == ["2024-05-07T23:00:00", "close", 570.0]
    assert highs[0][2] == 600.0
    assert lows[0][2] == 450.0
    assert price_usd[0][2] == 525.0


def test_format_chart_data_keeps_buckets_in_input_order() -> None:
    rows = [
        bucket(datetime(2024, 5, 7, 23, tzinfo=UTC), ["500"], ["550"], "600", "450", "525"),
        bucket(datetime(2024, 5, 8, 0, tzinfo=UTC), ["510"], ["560"], "610", "460", "535"),
    ]

    opens = format_chart_data(rows)[0]

    assert [point[0] for point in opens] == ["2024-05-07T23:00:00", "2024-05-08T00:00:00"]
    assert [point[2] for point in opens] == [500.0, 510.0]


def test_format_chart_data_coerces_decimals_to_floats() -> None:
    """GraphQL Float cannot carry a Decimal; the conversion belongs here."""
    rows = [bucket(datetime(2024, 5, 7, 23, tzinfo=UTC), ["500"], ["550"], "600", "450", "525")]

    for series in format_chart_data(rows):
        assert all(isinstance(point[2], float) for point in series)


def test_format_chart_data_treats_missing_values_as_zero() -> None:
    rows = [{"interval_start": datetime(2024, 5, 7, 23, tzinfo=UTC), "opens": [], "closes": []}]

    assert [series[0][2] for series in format_chart_data(rows)] == [0.0] * 5


def test_format_chart_data_handles_no_rows() -> None:
    assert format_chart_data([]) == [[], [], [], [], []]


def test_empty_chart_data_is_one_zero_point_per_series() -> None:
    result = empty_chart_data()

    assert len(result) == len(PRICE_TYPES)
    assert all(
        len(series) == 1 and series[0] == ["", ptype, 0.0]
        for series, ptype in zip(result, PRICE_TYPES, strict=True)
    )
