"""Severities, findings, and the report derived from them."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from candlegraph.agents.base import AgentReport, Finding, Severity, overall_severity, sort_findings
from tests.unit.agents.conftest import finding


def test_severities_order_from_ok_to_critical() -> None:
    assert Severity.OK < Severity.INFO < Severity.WARNING < Severity.CRITICAL
    assert Severity.CRITICAL.label == "critical"


def test_overall_severity_is_the_worst_present_and_ok_when_empty() -> None:
    assert overall_severity([]) == Severity.OK
    assert overall_severity([finding(Severity.INFO), finding(Severity.WARNING)]) == Severity.WARNING


def test_findings_sort_worst_first_then_stably() -> None:
    ordered = sort_findings(
        [finding(Severity.OK, subject="b"), finding(Severity.CRITICAL), finding(Severity.OK, subject="a")]
    )

    assert [f.severity for f in ordered] == [Severity.CRITICAL, Severity.OK, Severity.OK]
    assert [f.subject for f in ordered[1:]] == ["a", "b"]


def test_a_finding_serialises_its_severity_as_a_label() -> None:
    data = Finding("gap", Severity.WARNING, "WETH", "7h gap", {"hours": 7}).to_dict()

    assert data == {
        "kind": "gap",
        "severity": "warning",
        "subject": "WETH",
        "message": "7h gap",
        "evidence": {"hours": 7},
    }


def test_a_report_status_follows_its_findings_unless_it_errored() -> None:
    started = datetime(2026, 10, 25, tzinfo=UTC)
    report = AgentReport(
        "observer", "r", started, started + timedelta(seconds=3), [finding(Severity.WARNING)]
    )

    assert report.status == Severity.WARNING
    assert report.status_label == "warning"
    assert report.at_least(Severity.WARNING) and not report.at_least(Severity.CRITICAL)
    assert report.duration_seconds == 3.0

    report.error = "RuntimeError: x"
    assert report.status_label == "error"
    assert report.at_least(Severity.CRITICAL), "an errored run always needs attention"


def test_a_report_serialises_to_plain_json_types() -> None:
    started = datetime(2026, 10, 25, tzinfo=UTC)
    data = AgentReport("janitor", "r", started, started, [finding()], summary="s").to_dict()

    assert data["status"] == "ok"
    assert data["findings"][0]["severity"] == "ok"
    assert data["started_at"] == "2026-10-25T00:00:00+00:00"
