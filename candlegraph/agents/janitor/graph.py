"""Wiring the janitor.

inspect_tree → run_checks → [apply_fixes → rerun_checks → [draft_pr]] → assess → publish
"""

from __future__ import annotations

from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from candlegraph.agents.base import AgentReport, new_run_id, now_utc
from candlegraph.agents.janitor.checks import (
    JanitorDeps,
    apply_fixes,
    assess,
    draft_pr,
    inspect_tree,
    rerun_checks,
    route_after_checks,
    route_after_rerun,
    run_checks,
)
from candlegraph.agents.janitor.state import FIXERS, JanitorState
from candlegraph.agents.narrator import Narrator, TemplateNarrator
from candlegraph.agents.reporting import Sink, build_sinks, finish_report, publish
from candlegraph.agents.shell import Shell, SubprocessShell
from candlegraph.settings import Settings
from candlegraph.storage import DatabaseManager
from candlegraph.utils.logging_utils import service_logger

AGENT = "janitor"

JanitorGraph = CompiledStateGraph[JanitorState, None, JanitorState, JanitorState]


def checks_section(state: JanitorState) -> str:
    checks = state.get("checks", [])
    if not checks:
        return ""
    before = {c.target: c for c in state.get("before", [])}
    lines = ["| check | before | after | fixer | secs |", "|---|---|---|---|---:|"]
    for check in checks:
        was = before.get(check.target)
        fixer = (
            f"make {FIXERS[check.target]}"
            if was and not was.ok and check.target in FIXERS and state.get("fixes_applied")
            else ""
        )
        before_label = was.label if was else "-"
        lines.append(
            f"| {check.target} | {before_label} | {check.label} | {fixer} | {check.duration_seconds:.0f} |"
        )
    return "\n".join(lines)


def changes_section(state: JanitorState) -> str:
    parts: list[str] = []
    if state.get("pr_url"):
        parts.append(f"Draft PR: {state['pr_url']} (branch `{state.get('pr_branch')}`)")
    elif state.get("pr_branch"):
        parts.append(f"Branch `{state['pr_branch']}` was created but no PR was opened; see the errors.")
    if state.get("diff_stat"):
        parts.append("```\n" + state["diff_stat"] + "\n```")
    return "\n\n".join(parts)


def failures_section(state: JanitorState) -> str:
    blocks = []
    for check in state.get("checks", []):
        if not check.ok and check.output.strip():
            blocks.append(f"### make {check.target}\n\n```\n{check.output.strip()}\n```")
    return "\n\n".join(blocks)


def publish_node(deps: JanitorDeps, state: JanitorState) -> dict[str, Any]:
    sections = [
        ("Checks", checks_section(state)),
        ("Changes", changes_section(state)),
        ("Output of failing checks", failures_section(state)),
    ]
    detail = {
        "branch": state.get("branch"),
        "tree_clean": state.get("tree_clean"),
        "checks": [c.to_dict() for c in state.get("checks", [])],
        "fixes_applied": state.get("fixes_applied", []),
        "changed_files": state.get("changed_files", []),
        "pr_url": state.get("pr_url"),
        "errors": state.get("errors", []),
    }
    report = finish_report(
        AGENT,
        deps.run_id,
        deps.started_at,
        state.get("findings", []),
        deps.narrator.narrate,
        sections,
        detail,
    )
    failed = publish(report, deps.sinks)
    if failed:
        report.detail["failed_sinks"] = failed
    return {"report": report}


def build_janitor_graph(deps: JanitorDeps) -> JanitorGraph:
    graph: StateGraph[JanitorState, None, JanitorState, JanitorState] = StateGraph(JanitorState)
    graph.add_node("inspect_tree", partial(inspect_tree, deps))
    graph.add_node("run_checks", partial(run_checks, deps))
    graph.add_node("apply_fixes", partial(apply_fixes, deps))
    graph.add_node("rerun_checks", partial(rerun_checks, deps))
    graph.add_node("draft_pr", partial(draft_pr, deps))
    graph.add_node("assess", assess)
    graph.add_node("publish", partial(publish_node, deps))

    graph.add_edge(START, "inspect_tree")
    graph.add_edge("inspect_tree", "run_checks")
    graph.add_conditional_edges(
        "run_checks", route_after_checks, {"apply_fixes": "apply_fixes", "assess": "assess"}
    )
    graph.add_edge("apply_fixes", "rerun_checks")
    graph.add_conditional_edges(
        "rerun_checks", partial(route_after_rerun, deps), {"draft_pr": "draft_pr", "assess": "assess"}
    )
    graph.add_edge("draft_pr", "assess")
    graph.add_edge("assess", "publish")
    graph.add_edge("publish", END)
    return graph.compile()


def run_janitor(
    settings: Settings,
    shell: Shell | None = None,
    db: DatabaseManager | None = None,
    repo_root: Path | None = None,
    checks: tuple[str, ...] | None = None,
    open_pr: bool | None = None,
    now: datetime | None = None,
    narrator: Narrator | None = None,
    sinks: list[Sink] | None = None,
) -> AgentReport:
    """One janitor run, posted to the sinks. Never raises for a failing graph.

    ``db`` is only needed for the ``ledger`` sink; the janitor itself reads
    no database. When it is None and the configured sinks include the ledger,
    the process-wide manager is used.
    """
    shell = shell or SubprocessShell()
    narrator = narrator or TemplateNarrator()
    started = now_utc()
    if sinks is None:
        if db is None and "ledger" in settings.report_sinks:
            from candlegraph.storage import get_db

            db = get_db()
        sinks = build_sinks(settings, db=db, shell=shell)
    deps = JanitorDeps(
        shell=shell,
        repo_root=repo_root or settings.janitor_root,
        checks=checks or settings.janitor_check_targets,
        timeout=settings.janitor_timeout_seconds,
        output_lines=settings.janitor_output_lines,
        open_pr=settings.janitor_open_pr if open_pr is None else open_pr,
        base_branch=settings.janitor_base_branch,
        narrator=narrator,
        sinks=sinks,
        run_id=new_run_id(),
        started_at=started,
        now=now or started,
    )
    initial: JanitorState = {"findings": [], "errors": []}
    try:
        final = build_janitor_graph(deps).invoke(initial)
        report: AgentReport = final["report"]
        return report
    except Exception as exc:
        service_logger.exception("Janitor graph failed")
        report = finish_report(
            AGENT, deps.run_id, started, [], narrator.narrate, error=f"{type(exc).__name__}: {exc}"
        )
        publish(report, deps.sinks)
        return report
