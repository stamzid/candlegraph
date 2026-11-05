"""Stage records: the wrapper, the skipped rows, and the renderings. Pure."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from candlegraph.agents.stages import (
    StageRecord,
    complete_stages,
    describe,
    format_stage,
    staged,
    stages_section,
)

NOW = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)


def clock() -> datetime:
    return NOW


def test_the_wrapper_leaves_the_node_update_intact_and_appends_a_record() -> None:
    def node(state: dict[str, Any]) -> dict[str, Any]:
        return {"runs": [1, 2, 3]}

    update = staged("collect_ledger", node, lambda s, u: {"runs": len(u["runs"])}, clock=clock)({"now": NOW})

    assert update["runs"] == [1, 2, 3]
    (record,) = update["stages"]
    assert record.name == "collect_ledger" and record.status == "ok"
    assert record.started_at == NOW and record.duration_seconds is not None and record.duration_seconds >= 0
    assert record.detail == {"runs": 3}


def test_an_update_carrying_errors_grades_the_stage_error_without_stopping_the_graph() -> None:
    update = staged("collect_quality", lambda s: {"quality": None, "errors": ["boom"]}, clock=clock)({})

    assert update["stages"][0].status == "error"
    assert update["errors"] == ["boom"], "the node's own error list still reaches the state"


def test_the_hook_sees_each_record_as_it_is_made() -> None:
    seen: list[str] = []

    staged("assess", lambda s: {"findings": []}, on_stage=lambda r: seen.append(r.name), clock=clock)({})

    assert seen == ["assess"]


def test_stages_that_never_ran_are_listed_as_skipped_with_the_reason() -> None:
    ran = [StageRecord("collect_ledger", "error", NOW, 0.01, {"database_up": False})]

    full = complete_stages(ran, ("collect_ledger", "collect_quality", "assess"), "database unreachable")

    assert [(r.name, r.status) for r in full] == [
        ("collect_ledger", "error"),
        ("collect_quality", "skipped"),
        ("assess", "skipped"),
    ]
    assert full[1].detail == {"reason": "database unreachable"} and full[1].duration_seconds is None


def test_records_round_trip_through_json_safe_dicts() -> None:
    record = StageRecord("collect_metrics", "ok", NOW, 1.234, {"symbols": 9, "bars": 1700})

    data = record.to_dict()
    assert data["started_at"] == "2026-11-04T09:00:00+00:00"
    assert StageRecord.from_dict(data) == record
    assert StageRecord.from_dict({"name": "x"}).status == "ok"


def test_the_terminal_line_is_aligned_and_names_the_status() -> None:
    line = format_stage(StageRecord("collect_metrics", "ok", NOW, 1.2, {"symbols": 9, "ratio": 0.33333}))
    skipped = format_stage(StageRecord("assess", "skipped", None, None, {"reason": "database unreachable"}))

    assert line.startswith("ok   collect_metrics ")
    assert "1.20s" in line and "symbols=9, ratio=0.333" in line
    assert skipped.startswith("skip assess") and "reason=database unreachable" in skipped


def test_describe_trims_floats_and_joins_lists() -> None:
    assert (
        describe({"a": 1, "b": 0.123456, "c": ["log", "ledger"], "d": []})
        == "a=1, b=0.123, c=log,ledger, d=-"
    )


def test_the_section_is_a_table_in_stage_order_and_empty_for_no_records() -> None:
    text = stages_section(
        [
            StageRecord("collect_ledger", "ok", NOW, 0.05, {"runs": 39}),
            StageRecord("assess", "skipped", None, None, {"reason": "x"}),
        ]
    )

    lines = text.splitlines()
    assert lines[0] == "| stage | status | seconds | detail |"
    assert lines[2] == "| collect_ledger | ok | 0.05 | runs=39 |"
    assert lines[3] == "| assess | skipped | - | reason=x |"
    assert stages_section([]) == ""
