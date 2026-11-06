"""The pure half of the tool surface: JSON-safety and redaction.

Everything here runs on hand-built values. The rules being pinned down are
the two that only bite at the protocol boundary — ``NaN`` is not JSON, and a
credential can reach a result the same way it can reach a report.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from decimal import Decimal

import numpy as np
import pandas as pd

from candlegraph.agents.base import AgentReport, Finding, Severity
from candlegraph.mcp import payloads


def test_a_not_a_number_becomes_null_rather_than_breaking_the_json() -> None:
    """``json.dumps`` writes a bare ``NaN``, which no JSON parser accepts."""
    assert payloads.finite(float("nan")) is None
    assert payloads.finite(float("inf")) is None
    assert payloads.finite(float("-inf")) is None
    assert payloads.finite(np.float64("nan")) is None
    assert payloads.finite(None) is None
    assert payloads.finite("not a number") is None


def test_a_real_number_survives_whatever_type_it_arrived_as() -> None:
    assert payloads.finite(Decimal("1.5")) == 1.5
    assert payloads.finite(3) == 3.0
    assert payloads.finite("2.25") == 2.25


def test_a_naive_timestamp_is_read_as_utc_rather_than_local_time() -> None:
    naive = datetime(2026, 11, 4, 14, 0)
    assert payloads.iso(naive) == "2026-11-04T14:00:00+00:00"
    assert payloads.iso(None) is None


def test_a_jsonb_column_keeps_its_shape_instead_of_becoming_a_repr() -> None:
    """``ingest_run.errors`` and ``agent_run.findings`` arrive as nested data."""
    value = payloads.plain({"WETH": {"tries": 3, "last": Decimal("1.5")}, "seen": [1, 2]})

    assert value == {"WETH": {"tries": 3, "last": 1.5}, "seen": [1, 2]}


def test_a_credential_is_masked_at_any_depth_of_the_result() -> None:
    payload = payloads.redact_payload(
        {
            "runs": [{"error": "connection to postgresql://candlegraph:hunter2@db failed"}],
            "note": "TAVILY_API_KEY=tvly-abcdefgh rejected",
            "count": 2,
        }
    )

    text = json.dumps(payload)
    assert "hunter2" not in text
    assert "abcdefgh" not in text
    assert payload["count"] == 2


def test_an_incomplete_rolling_window_reports_no_numbers_and_says_why() -> None:
    """Invariant 16: a window is complete or absent, never estimated short."""
    summary = pd.DataFrame(
        {"price": [1.0, 2.0], "rolling_mean": [float("nan")] * 2},
        index=pd.date_range("2026-11-04", periods=2, freq="h", tz="UTC"),
    )

    payload = payloads.stats_payload("WBTC", 24, summary)

    assert payload["complete_windows"] == 0
    assert "not enough history" in payload["note"]
    assert "annualised_volatility" not in payload


def test_a_complete_window_reports_the_latest_row_with_its_timestamp() -> None:
    index = pd.date_range("2026-11-04", periods=2, freq="h", tz="UTC")
    summary = pd.DataFrame(
        {
            "price": [10.0, 11.0],
            "rolling_mean": [9.0, 10.0],
            "rolling_return": [0.01, 0.02],
            "rolling_volatility": [0.5, 0.6],
        },
        index=index,
    )

    payload = payloads.stats_payload("WBTC", 2, summary)

    assert payload["as_of"] == "2026-11-04T01:00:00+00:00"
    assert payload["annualised_volatility"] == 0.6
    assert payload["price"] == 11.0


def test_a_report_payload_carries_the_findings_and_optionally_the_markdown() -> None:
    report = AgentReport(
        agent="observer",
        run_id="r-1",
        started_at=datetime(2026, 11, 4, tzinfo=UTC),
        finished_at=datetime(2026, 11, 4, 0, 0, 2, tzinfo=UTC),
        findings=[Finding("gap", Severity.WARNING, "WETH", "7h gap")],
        markdown="# observer",
    )

    assert "markdown" not in payloads.report_payload(report)
    full = payloads.report_payload(report, markdown=True)
    assert full["markdown"] == "# observer"
    assert full["status"] == "warning"
    assert full["findings"][0]["subject"] == "WETH"


def test_every_payload_survives_a_strict_json_encoder() -> None:
    """The real check: ``allow_nan=False`` is what a protocol encoder does."""
    index = pd.date_range("2026-11-04", periods=3, freq="h", tz="UTC")
    summary = pd.DataFrame(
        {
            "price": [1.0, 2.0, 3.0],
            "rolling_mean": [float("nan"), 1.5, 2.5],
            "rolling_return": [float("nan"), 0.5, 0.5],
            "rolling_volatility": [float("nan"), float("inf"), 0.2],
        },
        index=index,
    )

    encoded = json.dumps(payloads.stats_payload("WBTC", 2, summary), allow_nan=False)

    assert math.isfinite(json.loads(encoded)["price"])
