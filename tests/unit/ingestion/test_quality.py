"""Gap detection and the data-quality report.

The behaviour that matters most: a missing hour is normal (no swaps means no
row), so the report measures it rather than treating it as an error.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from candlegraph.ingestion.quality import (
    HOUR,
    Gap,
    QualityReport,
    TokenQuality,
    build_report,
    find_gaps,
    format_report,
    group_missing_hours,
)
from tests.conftest import SHIB, WBTC, FakeDatabase

BASE = 1_700_000_000 // (24 * HOUR) * (24 * HOUR)
NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)


def coverage_row(token_id: str, symbol: str, stored: int, first: int, last: int) -> dict:
    return {
        "token_id": token_id,
        "symbol": symbol,
        "stored_hours": stored,
        "first_unix": first,
        "last_unix": last,
        "expected_hours": (last - first) // HOUR + 1,
    }


# --- grouping --------------------------------------------------------------


def test_no_missing_hours_is_no_gaps() -> None:
    assert group_missing_hours([]) == []


def test_a_single_missing_hour_is_a_one_hour_gap() -> None:
    [gap] = group_missing_hours([BASE])

    assert gap.hours == 1
    assert gap.start_unix == gap.end_unix == BASE


def test_consecutive_missing_hours_collapse_into_one_gap() -> None:
    """212 separate missing hours is noise; '1 gap, 212h' is actionable."""
    [gap] = group_missing_hours([BASE + i * HOUR for i in range(5)])

    assert gap.hours == 5
    assert gap.start_unix == BASE
    assert gap.end_unix == BASE + 4 * HOUR


def test_non_adjacent_missing_hours_stay_separate() -> None:
    gaps = group_missing_hours([BASE, BASE + HOUR, BASE + 5 * HOUR])

    assert [gap.hours for gap in gaps] == [2, 1]


def test_unsorted_input_is_still_grouped_correctly() -> None:
    gaps = group_missing_hours([BASE + 2 * HOUR, BASE, BASE + HOUR])

    assert len(gaps) == 1
    assert gaps[0].hours == 3


def test_a_gap_renders_its_span_and_length() -> None:
    text = str(Gap(BASE, BASE + 2 * HOUR))

    assert "3h" in text
    assert ".." in text


def test_find_gaps_queries_the_requested_window() -> None:
    db = FakeDatabase(missing_hours=[{"period_start_unix": BASE}])

    gaps = find_gaps(db, WBTC, start_unix=BASE)

    assert [gap.hours for gap in gaps] == [1]
    assert ("missing_hours", {"token_id": WBTC, "start_unix": BASE, "end_unix": None}) in db.fetches


# --- TokenQuality ----------------------------------------------------------


def test_full_coverage_is_one() -> None:
    token = TokenQuality(WBTC, "WBTC", stored_hours=24, expected_hours=24)

    assert token.coverage == 1.0
    assert token.missing_hours == 0


def test_partial_coverage_is_the_stored_fraction() -> None:
    token = TokenQuality(WBTC, "WBTC", stored_hours=18, expected_hours=24)

    assert token.coverage == pytest.approx(0.75)
    assert token.missing_hours == 6


def test_coverage_of_an_empty_token_is_zero_not_a_crash() -> None:
    assert TokenQuality(WBTC, "WBTC").coverage == 0.0


def test_the_longest_gap_is_reported() -> None:
    token = TokenQuality(WBTC, "WBTC", gaps=[Gap(BASE, BASE), Gap(BASE + 5 * HOUR, BASE + 9 * HOUR)])

    longest = token.longest_gap
    assert longest is not None
    assert longest.hours == 5


def test_a_token_with_no_gaps_has_no_longest_gap() -> None:
    assert TokenQuality(WBTC, "WBTC").longest_gap is None


# --- build_report ----------------------------------------------------------


def test_a_clean_database_reports_ok() -> None:
    db = FakeDatabase(
        coverage_report=[coverage_row(WBTC, "WBTC", 25, BASE, BASE + 24 * HOUR)],
        field_completeness=[{"rows_total": 25, "volume_present": 25}],
    )

    report = build_report(db, expected_symbols=("WBTC",), now=NOW)

    assert report.ok
    assert report.total_rows == 25
    assert report.tokens[0].coverage == 1.0


def test_a_token_with_no_rows_is_reported_missing() -> None:
    """A configured token that ingested nothing is the loudest possible signal."""
    db = FakeDatabase(coverage_report=[coverage_row(WBTC, "WBTC", 25, BASE, BASE + 24 * HOUR)])

    report = build_report(db, expected_symbols=("WBTC", "GNO"), now=NOW)

    assert report.missing_tokens == ["GNO"]
    assert not report.ok


def test_missing_token_matching_is_case_insensitive() -> None:
    db = FakeDatabase(coverage_report=[coverage_row(WBTC, "wbtc", 2, BASE, BASE + HOUR)])

    assert build_report(db, expected_symbols=("WBTC",), now=NOW).missing_tokens == []


def test_coverage_below_the_threshold_is_flagged() -> None:
    db = FakeDatabase(
        coverage_report=[coverage_row(WBTC, "WBTC", 10, BASE, BASE + 24 * HOUR)],
        missing_hours=[{"period_start_unix": BASE + i * HOUR} for i in range(1, 16)],
    )

    report = build_report(db, min_expected_coverage=0.8, now=NOW)

    assert [t.symbol for t in report.under_covered] == ["WBTC"]
    assert not report.ok


def test_a_quiet_token_above_the_threshold_is_not_flagged() -> None:
    """Hours with no swaps are legitimate; the threshold is the judgement call."""
    db = FakeDatabase(
        coverage_report=[coverage_row(WBTC, "WBTC", 23, BASE, BASE + 24 * HOUR)],
        missing_hours=[{"period_start_unix": BASE + HOUR}, {"period_start_unix": BASE + 3 * HOUR}],
    )

    report = build_report(db, min_expected_coverage=0.8, now=NOW)

    assert report.under_covered == []
    assert len(report.tokens[0].gaps) == 2


def test_gaps_are_only_looked_up_when_hours_are_missing() -> None:
    db = FakeDatabase(coverage_report=[coverage_row(WBTC, "WBTC", 25, BASE, BASE + 24 * HOUR)])

    build_report(db, now=NOW)

    assert [label for label, _ in db.fetches if label == "missing_hours"] == []


def test_impossible_rows_are_surfaced() -> None:
    db = FakeDatabase(
        coverage_report=[coverage_row(WBTC, "WBTC", 25, BASE, BASE + 24 * HOUR)],
        ohlc_violations=[{"token_id": WBTC, "period_start_unix": BASE, "high": 1, "low": 9}],
    )

    report = build_report(db, now=NOW)

    assert len(report.violations) == 1
    assert not report.ok


def test_several_tokens_are_each_measured() -> None:
    db = FakeDatabase(
        coverage_report=[
            coverage_row(WBTC, "WBTC", 25, BASE, BASE + 24 * HOUR),
            coverage_row(SHIB, "SHIB", 20, BASE, BASE + 24 * HOUR),
        ],
        missing_hours=[{"period_start_unix": BASE + HOUR}],
    )

    report = build_report(db, now=NOW)

    assert {t.symbol for t in report.tokens} == {"WBTC", "SHIB"}
    assert report.total_rows == 45


# --- formatting ------------------------------------------------------------


def test_the_report_renders_a_row_per_token() -> None:
    db = FakeDatabase(
        coverage_report=[coverage_row(WBTC, "WBTC", 25, BASE, BASE + 24 * HOUR)],
        field_completeness=[
            {
                "rows_total": 25,
                "volume_present": 25,
                "volume_usd_present": 25,
                "tvl_usd_present": 20,
                "fees_usd_present": 0,
            }
        ],
    )

    text = format_report(build_report(db, expected_symbols=("WBTC",), now=NOW))

    assert "WBTC" in text
    assert "100.0%" in text
    assert "field completeness" in text
    assert "tvl_usd=80%" in text
    assert "OK:" in text


def test_the_report_names_what_needs_attention() -> None:
    db = FakeDatabase(
        coverage_report=[coverage_row(WBTC, "WBTC", 5, BASE, BASE + 24 * HOUR)],
        missing_hours=[{"period_start_unix": BASE + i * HOUR} for i in range(1, 21)],
        ohlc_violations=[{"token_id": WBTC}],
    )

    text = format_report(build_report(db, expected_symbols=("WBTC", "GNO"), now=NOW))

    assert "MISSING (no rows at all): GNO" in text
    assert "BELOW 80% COVERAGE" in text
    assert "IMPOSSIBLE ROWS: 1" in text
    assert "OK:" not in text


def test_an_empty_report_still_renders() -> None:
    text = format_report(QualityReport(generated_at=NOW))

    assert "Data-quality report" in text
    assert "total" in text
