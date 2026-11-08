"""The fill cache against a real TimescaleDB: the table, and its invalidation.

The unit tier proves which rows the refresh decides to write. What only this
can prove is that the migration produced a table those rows actually fit,
that the natural key really is `(token_id, period_start_unix, method)`, and
that a cached estimate disappears the moment ingestion supplies the hour it
was standing in for.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from candlegraph.ingestion.fills import (
    LINEAR,
    METHODS,
    fill_cache_summary,
    load_fills,
    refresh_fill_cache,
)
from candlegraph.storage import DatabaseManager
from tests.integration.test_timescale import BASE, HOUR, hour_record, write_hours

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


@pytest.fixture
def gapped(db: DatabaseManager) -> DatabaseManager:
    """Hours 0-2 and 5-7 stored for WBTC; 3 and 4 never emitted."""
    write_hours(db, [hour_record(BASE + i * HOUR, price=str(100 + i)) for i in (0, 1, 2, 5, 6, 7)])
    return db


def test_the_cache_stores_only_the_hours_the_subgraph_never_emitted(
    gapped: DatabaseManager,
) -> None:
    summary = refresh_fill_cache(gapped, ["WBTC"], now=NOW)

    assert summary.rows_written == 2
    stored = load_fills(gapped, ["WBTC"])
    assert list(stored["period_start_unix"]) == [BASE + 3 * HOUR, BASE + 4 * HOUR]
    assert stored["price_usd"].tolist() == [pytest.approx(103.0), pytest.approx(104.0)]
    assert stored["gap_hours"].tolist() == [2, 2]

    # The market table is untouched: six rows in, six rows out.
    rows = gapped.fetch_all(f"SELECT COUNT(*) AS n FROM {gapped.schema}.token_hours_data", None)
    assert rows[0]["n"] == 6, "filling must not add a row to token_hours_data"


def test_the_coverage_report_is_unmoved_by_a_populated_cache(gapped: DatabaseManager) -> None:
    """The whole reason the fills live in their own table.

    If filling touched `token_hours_data`, coverage would read 100% by
    construction and the quality report could never be falsified again.
    """
    before = gapped.fetch_all(gapped.sql.coverage_report, {"start_unix": None})

    refresh_fill_cache(gapped, ["WBTC"], now=NOW)
    after = gapped.fetch_all(gapped.sql.coverage_report, {"start_unix": None})

    assert before == after
    assert after[0]["stored_hours"] == 6
    assert after[0]["expected_hours"] == 8, "two hours still count as missing"


def test_refreshing_twice_rewrites_rather_than_duplicates(gapped: DatabaseManager) -> None:
    """The natural key is doing its job."""
    refresh_fill_cache(gapped, ["WBTC"], now=NOW)
    refresh_fill_cache(gapped, ["WBTC"], now=NOW)

    assert len(load_fills(gapped, ["WBTC"])) == 2


def test_an_estimate_is_removed_once_the_real_hour_arrives(gapped: DatabaseManager) -> None:
    """A cached fill for an ingested hour is not stale but wrong."""
    refresh_fill_cache(gapped, ["WBTC"], now=NOW)
    assert len(load_fills(gapped, ["WBTC"])) == 2

    # Ingestion catches up on one of the two missing hours.
    write_hours(gapped, [hour_record(BASE + 3 * HOUR, price="103.5")])
    removed = gapped.execute(gapped.sql.delete_superseded_fills, None)

    assert removed == 1
    assert list(load_fills(gapped, ["WBTC"])["period_start_unix"]) == [BASE + 4 * HOUR]


def test_a_gap_longer_than_the_cap_leaves_the_table_empty(gapped: DatabaseManager) -> None:
    summary = refresh_fill_cache(gapped, ["WBTC"], max_fill=1, now=NOW)

    assert summary.rows_written == 0
    assert load_fills(gapped, ["WBTC"]).empty


def test_clearing_a_method_empties_it(gapped: DatabaseManager) -> None:
    refresh_fill_cache(gapped, ["WBTC"], now=NOW)

    removed = gapped.execute(gapped.sql.delete_fills_for_method, {"method": LINEAR, "symbols": ["WBTC"]})

    assert removed == 2
    assert load_fills(gapped, ["WBTC"]).empty


def test_the_summary_reports_what_is_cached(gapped: DatabaseManager) -> None:
    refresh_fill_cache(gapped, ["WBTC"], now=NOW)

    summary = fill_cache_summary(gapped)

    assert summary.iloc[0]["symbol"] == "WBTC"
    assert summary.iloc[0]["method"] == LINEAR
    assert int(summary.iloc[0]["filled_hours"]) == 2
    assert int(summary.iloc[0]["longest_gap_hours"]) == 2


def test_the_fill_table_is_keyed_on_token_hour_and_method(db: DatabaseManager) -> None:
    """`method` is in the key so a causal estimator can land beside the linear
    one in step 4 without either overwriting the other."""
    rows = db.fetch_all(
        """
        SELECT a.attname AS column_name
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        JOIN unnest(c.conkey) AS k(attnum) ON TRUE
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
        WHERE n.nspname = %(schema)s AND t.relname = 'token_hours_fill' AND c.contype = 'u'
        """,
        {"schema": db.schema},
    )

    assert {row["column_name"] for row in rows} == {"token_id", "period_start_unix", "method"}


# --- three estimators against a real database -------------------------------


def test_all_three_methods_coexist_for_the_same_missing_hour(gapped: DatabaseManager) -> None:
    """The natural key includes `method`, which is what makes them comparable.

    Without this the second estimator would overwrite the first and there
    would be nothing to compare.
    """
    for method in METHODS:
        refresh_fill_cache(gapped, ["WBTC"], method=method, now=NOW)

    stored = {m: load_fills(gapped, ["WBTC"], method=m) for m in METHODS}

    assert all(len(frame) == 2 for frame in stored.values())
    hours = {m: list(frame["period_start_unix"]) for m, frame in stored.items()}
    assert len({tuple(v) for v in hours.values()}) == 1, "the same two hours, three ways"
    # Three different answers for one hour is the point.
    first = {m: frame["price_usd"].iloc[0] for m, frame in stored.items()}
    assert first["causal"] == pytest.approx(102.0), "flat forecast"
    assert first["linear"] != pytest.approx(first["causal"])

    total = gapped.fetch_all(
        f"SELECT COUNT(*) AS n FROM {gapped.schema}.token_hours_fill",
        None,
    )
    assert total[0]["n"] == 6, "two hours x three methods"


def test_the_error_bar_round_trips_through_the_database(gapped: DatabaseManager) -> None:
    """`sigma_log` is NULL for linear and a real number for the model forms."""
    for method in METHODS:
        refresh_fill_cache(gapped, ["WBTC"], method=method, now=NOW)

    assert load_fills(gapped, ["WBTC"], method="linear")["sigma_log"].isna().all()
    bridge = load_fills(gapped, ["WBTC"], method="bridge")["sigma_log"]
    assert bridge.notna().all()
    assert (bridge > 0).all(), "a stored band is positive, never zero"


def test_clearing_one_method_leaves_the_others_intact(gapped: DatabaseManager) -> None:
    for method in METHODS:
        refresh_fill_cache(gapped, ["WBTC"], method=method, now=NOW)

    removed = gapped.execute(gapped.sql.delete_fills_for_method, {"method": "linear", "symbols": ["WBTC"]})

    assert removed == 2
    assert load_fills(gapped, ["WBTC"], method="linear").empty
    assert len(load_fills(gapped, ["WBTC"], method="bridge")) == 2


def test_a_real_row_supersedes_every_method_at_once(gapped: DatabaseManager) -> None:
    """An observation beats all three estimates of it, not just the selected one."""
    for method in METHODS:
        refresh_fill_cache(gapped, ["WBTC"], method=method, now=NOW)

    write_hours(gapped, [hour_record(BASE + 3 * HOUR, price="103.5")])
    removed = gapped.execute(gapped.sql.delete_superseded_fills, None)

    assert removed == 3, "one per method"
    for method in METHODS:
        assert list(load_fills(gapped, ["WBTC"], method=method)["period_start_unix"]) == [BASE + 4 * HOUR]


def test_the_summary_counts_banded_hours_per_method(gapped: DatabaseManager) -> None:
    for method in METHODS:
        refresh_fill_cache(gapped, ["WBTC"], method=method, now=NOW)

    summary = fill_cache_summary(gapped).set_index("method")

    assert int(summary.loc["linear", "hours_with_band"]) == 0
    assert int(summary.loc["bridge", "hours_with_band"]) == 2
    assert int(summary.loc["bridge", "filled_hours"]) == 2
