"""Statement construction, asserted without a database connection."""

from __future__ import annotations

import pytest
from psycopg2 import sql

from candlegraph.storage import statements_for


def render(composed: sql.Composed) -> str:
    """Flatten a Composed into readable SQL without needing a connection."""
    parts: list[str] = []
    for part in composed.seq:
        if isinstance(part, sql.Identifier):
            parts.append('"' + '"."'.join(part.strings) + '"')
        else:
            parts.append(part.string)
    return "".join(parts)


# --- statements ------------------------------------------------------------


def test_statements_are_cached_per_schema() -> None:
    assert statements_for("candlegraph") is statements_for("candlegraph")
    assert statements_for("candlegraph") is not statements_for("other")


def test_statements_are_namespaced_to_their_schema() -> None:
    rendered = render(statements_for("other_schema").chart_query)

    assert '"other_schema".token_hours_data' in rendered


def test_the_hourly_insert_upserts_on_the_natural_key() -> None:
    """Without this, one duplicate row failed an entire batch silently."""
    rendered = render(statements_for("candlegraph").insert_token_hours)

    assert "ON CONFLICT (token_id, period_start_unix) DO UPDATE SET" in rendered
    assert "price_usd = EXCLUDED.price_usd" in rendered
    assert rendered.strip().endswith("RETURNING id")


def test_the_token_insert_upserts_on_the_primary_key() -> None:
    rendered = render(statements_for("candlegraph").insert_token)

    assert "ON CONFLICT (id) DO UPDATE SET" in rendered
    assert "name = EXCLUDED.name" in rendered


@pytest.mark.parametrize(
    ("template", "fields"),
    [
        (
            "insert_token_hours_template",
            [
                "id",
                "symbol",
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
                "periodStartUnix",
                "timestamp",
            ],
        ),
        (
            "insert_token_template",
            [
                "id",
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
                "updated_at",
            ],
        ),
        (
            "insert_pool_template",
            [
                "id",
                "token0_id",
                "token1_id",
                "token0_symbol",
                "token1_symbol",
                "feeTier",
                "createdAtTimestamp",
                "liquidity",
                "volumeUSD",
                "feesUSD",
                "txCount",
                "totalValueLockedUSD",
                "updated_at",
            ],
        ),
        (
            "insert_pool_hours_template",
            [
                "pool_id",
                "periodStartUnix",
                "timestamp",
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
            ],
        ),
    ],
)
def test_insert_templates_name_every_column(template: str, fields: list[str]) -> None:
    rendered = getattr(statements_for("candlegraph"), template)

    assert [part for part in fields if f"%({part})s" in rendered] == fields
    assert rendered.count("%(") == len(fields)


@pytest.mark.parametrize(
    ("statement", "columns", "template"),
    [
        ("insert_token_hours", "TOKEN_HOUR_COLUMNS", "insert_token_hours_template"),
        ("insert_token", "TOKEN_COLUMNS", "insert_token_template"),
        ("insert_pool", "POOL_COLUMNS", "insert_pool_template"),
        ("insert_pool_hours", "POOL_HOUR_COLUMNS", "insert_pool_hours_template"),
    ],
)
def test_each_insert_lists_as_many_columns_as_its_template(
    statement: str, columns: str, template: str
) -> None:
    """A column list and a VALUES template that disagree is a runtime crash."""
    from candlegraph.storage import statements

    statements_obj = statements_for("candlegraph")
    column_names = getattr(statements, columns)
    rendered_template = getattr(statements_obj, template)

    assert rendered_template.count("%(") == len(column_names)
    rendered = render(getattr(statements_obj, statement))
    for name in column_names:
        assert name in rendered


def test_the_chart_bucket_label_is_the_bucket_boundary() -> None:
    """Labelling with MIN(timestamp) shifts the candle when an hour is missing.

    The bucket is computed from the interval and the epoch, never from the
    rows present. This was `FLOOR(period_start_unix / interval) * interval`
    until the TimescaleDB migration; `time_bucket` on an integer column is
    the same arithmetic, and is the form a continuous aggregate can be
    defined on. The behaviour this protects is asserted against a real
    database by `test_candles_still_bucket_on_boundaries_through_time_bucket`.
    """
    rendered = render(statements_for("candlegraph").chart_query)

    assert "to_timestamp(time_bucket(%(interval)s::bigint, period_start_unix))" in rendered
    assert "GROUP BY time_bucket(%(interval)s::bigint, period_start_unix)" in rendered
    assert "MIN(timestamp)" not in rendered


def test_the_chart_query_supports_an_optional_time_window() -> None:
    rendered = render(statements_for("candlegraph").chart_query)

    assert "%(start_unix)s" in rendered
    assert "%(end_unix)s" in rendered
