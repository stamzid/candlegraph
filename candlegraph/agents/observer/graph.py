"""Wiring the observer: collect, assess, publish.

The graph is deliberately linear with one short-circuit. If the database is
down there is nothing to assess, so ``collect_ledger`` routes straight to
``publish`` with its one critical finding.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import datetime
from functools import partial
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from candlegraph.agents.base import AgentReport, Severity, new_run_id, now_utc
from candlegraph.agents.narrator import Narrator, TemplateNarrator
from candlegraph.agents.observer.assess import assess
from candlegraph.agents.observer.collect import (
    ObserverDeps,
    collect_knowledge,
    collect_ledger,
    collect_metrics,
    collect_quality,
    route_after_ledger,
)
from candlegraph.agents.observer.state import ObserverState, ObserverThresholds, SymbolMetrics
from candlegraph.agents.reporting import Sink, build_sinks, finish_report, publish
from candlegraph.agents.stages import StageHook, StageRecord, complete_stages, staged, stages_section
from candlegraph.settings import Settings
from candlegraph.storage import DatabaseManager, get_db
from candlegraph.tokens import TokenRegistry, get_token_registry
from candlegraph.utils.logging_utils import service_logger

AGENT = "observer"
# The nodes in the order they run; `publish` marks any that did not as skipped.
STAGES: tuple[str, ...] = (
    "collect_ledger",
    "collect_quality",
    "collect_metrics",
    "collect_knowledge",
    "assess",
)

ObserverGraph = CompiledStateGraph[ObserverState, None, ObserverState, ObserverState]


# --- what each stage reports about itself (pure) ----------------------------------


def ledger_detail(_state: Mapping[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    age = update.get("seconds_since_success")
    return {
        "database_up": bool(update.get("database_up", False)),
        "runs": len(update.get("runs", [])),
        "seconds_since_success": round(float(age)) if age is not None else None,
    }


def quality_detail(_state: Mapping[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    quality = update.get("quality")
    if quality is None:
        return {"tokens": 0}
    return {
        "tokens": len(quality.tokens),
        "missing_tokens": len(quality.missing_tokens),
        "violations": len(quality.violations),
    }


def metrics_detail(_state: Mapping[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    metrics: dict[str, SymbolMetrics] = update.get("metrics", {})
    return {
        "symbols": len(metrics),
        "bars": sum(m.recent_bars + m.baseline_bars for m in metrics.values()),
        "runs_judged": getattr(update.get("run_metrics"), "recent_runs", 0)
        + getattr(update.get("run_metrics"), "baseline_runs", 0),
    }


def knowledge_detail(_state: Mapping[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    facts = update.get("knowledge")
    if facts is None:
        return {"read": False}
    return {
        "credits_month": facts.month_spent,
        "calls": len(facts.call_statuses),
        "backlog": facts.backlog_missing + facts.backlog_stale,
        "explanations": facts.explanations_total,
    }


def assess_detail(_state: Mapping[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    findings = update.get("findings", [])
    return {
        "findings": len(findings),
        **{
            level.label: sum(1 for f in findings if f.severity == level)
            for level in (Severity.CRITICAL, Severity.WARNING, Severity.INFO, Severity.OK)
        },
    }


def _ratio(a: float | None, b: float | None) -> str:
    if a is None or b is None or b <= 0:
        return "-"
    return f"x{a / b:.2f}"


def coverage_section(state: ObserverState) -> str:
    quality = state.get("quality")
    if quality is None or not quality.tokens:
        return ""
    metrics = state.get("metrics", {})
    lines = [
        "| symbol | rows | coverage | gaps | longest | mean hourly volume |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for token in sorted(quality.tokens, key=lambda tok: tok.symbol):
        longest = token.longest_gap
        m = metrics.get(token.symbol)
        volume = f"${m.baseline_volume_mean:,.0f}" if m and m.baseline_volume_mean is not None else "-"
        lines.append(
            f"| {token.symbol} | {token.stored_hours} | {token.coverage:.1%} | {len(token.gaps)} | "
            f"{f'{longest.hours}h' if longest else '-'} | {volume} |"
        )
    return "\n".join(lines)


def drift_section(state: ObserverState) -> str:
    metrics: dict[str, SymbolMetrics] = state.get("metrics", {})
    if not metrics:
        return ""
    lines = [
        "| symbol | bars recent/baseline | volume median recent | baseline | ratio "
        "| vol recent | baseline | ratio |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]

    def money(v: float | None) -> str:
        return f"${v:,.0f}" if v is not None else "-"

    def sigma(v: float | None) -> str:
        return f"{v:.4f}" if v is not None else "-"

    for symbol, m in sorted(metrics.items()):
        volume_ratio = _ratio(m.recent_volume_median, m.baseline_volume_median)
        vol_ratio = _ratio(m.recent_volatility, m.baseline_volatility)
        lines.append(
            f"| {symbol} | {m.recent_bars}/{m.baseline_bars} | {money(m.recent_volume_median)} | "
            f"{money(m.baseline_volume_median)} | {volume_ratio} | "
            f"{sigma(m.recent_volatility)} | {sigma(m.baseline_volatility)} | {vol_ratio} |"
        )
    return "\n".join(lines)


def ledger_section(state: ObserverState, limit: int = 8) -> str:
    runs = state.get("runs", [])
    if not runs:
        return ""
    lines = [
        "| started | trigger | status | token hours | pool hours | secs |",
        "|---|---|---|---:|---:|---:|",
    ]
    for run in runs[:limit]:
        started = run.get("started_at")
        when = f"{started:%Y-%m-%d %H:%M}" if isinstance(started, datetime) else str(started)
        duration = run.get("duration_seconds")
        secs = f"{float(duration):.1f}" if duration is not None else "-"
        lines.append(
            f"| {when} | {run.get('trigger')} | {run.get('status')} | {run.get('token_hours_written', 0)} | "
            f"{run.get('pool_hours_written', 0)} | {secs} |"
        )
    return "\n".join(lines)


def publish_node(deps: ObserverDeps, state: ObserverState) -> dict[str, Any]:
    started = now_utc()
    t0 = time.perf_counter()
    # The stages that fed this report, with the short-circuited ones named
    # as skipped rather than silently absent.
    reason = "database unreachable" if not state.get("database_up", False) else "not run"
    stages = complete_stages(state.get("stages", []), STAGES, reason)
    sections = [
        ("Stages", stages_section(stages)),
        ("Coverage", coverage_section(state)),
        ("Drift", drift_section(state)),
        ("Recent ingest runs", ledger_section(state)),
    ]
    detail = {
        "database_up": state.get("database_up", False),
        "seconds_since_success": state.get("seconds_since_success"),
        "symbols_judged": len(state.get("metrics", {})),
        "errors": state.get("errors", []),
        "stages": [s.to_dict() for s in stages],
    }
    report = finish_report(
        AGENT,
        deps.run_id,
        deps.started_at,
        state.get("findings", []),
        deps.narrator.narrate,
        sections=sections,
        detail=detail,
        now=state["now"] if state["now"] > deps.started_at else None,
    )
    failed = publish(report, deps.sinks)
    if failed:
        report.detail["failed_sinks"] = failed
    # Publish is a stage too; it can only record itself once the sinks have
    # answered, so it lands in the ledger's detail and the hook, not the table.
    posted = StageRecord(
        "publish",
        "error" if failed else "ok",
        started,
        round(time.perf_counter() - t0, 3),
        {"sinks": [s.name for s in deps.sinks], "failed": failed},
    )
    report.detail["stages"].append(posted.to_dict())
    if deps.on_stage is not None:
        deps.on_stage(posted)
    return {"report": report, "stages": [posted]}


def build_observer_graph(deps: ObserverDeps) -> ObserverGraph:
    graph: StateGraph[ObserverState, None, ObserverState, ObserverState] = StateGraph(ObserverState)
    hook = deps.on_stage
    # Bound explicitly so the wrapper is typed over ObserverState, not `partial[...]`.
    ledger_node: Callable[[ObserverState], dict[str, Any]] = partial(collect_ledger, deps)
    quality_node: Callable[[ObserverState], dict[str, Any]] = partial(collect_quality, deps)
    metrics_node: Callable[[ObserverState], dict[str, Any]] = partial(collect_metrics, deps)
    knowledge_node: Callable[[ObserverState], dict[str, Any]] = partial(collect_knowledge, deps)
    graph.add_node("collect_ledger", staged("collect_ledger", ledger_node, ledger_detail, hook))
    graph.add_node("collect_quality", staged("collect_quality", quality_node, quality_detail, hook))
    graph.add_node("collect_metrics", staged("collect_metrics", metrics_node, metrics_detail, hook))
    graph.add_node("collect_knowledge", staged("collect_knowledge", knowledge_node, knowledge_detail, hook))
    graph.add_node("assess", staged("assess", assess, assess_detail, hook))
    graph.add_node("publish", partial(publish_node, deps))

    graph.add_edge(START, "collect_ledger")
    graph.add_conditional_edges(
        "collect_ledger", route_after_ledger, {"collect_quality": "collect_quality", "publish": "publish"}
    )
    graph.add_edge("collect_quality", "collect_metrics")
    graph.add_edge("collect_metrics", "collect_knowledge")
    graph.add_edge("collect_knowledge", "assess")
    graph.add_edge("assess", "publish")
    graph.add_edge("publish", END)
    return graph.compile()


def run_observer(
    settings: Settings,
    db: DatabaseManager | None = None,
    registry: TokenRegistry | None = None,
    now: datetime | None = None,
    narrator: Narrator | None = None,
    sinks: list[Sink] | None = None,
    on_stage: StageHook | None = None,
) -> AgentReport:
    """One observer run, posted to the sinks. Never raises for a failing graph.

    ``on_stage`` is called with each node's :class:`StageRecord` as it
    finishes — what ``observe --progress`` prints and the UI streams.
    """
    db = db or get_db()
    registry = registry or get_token_registry()
    narrator = narrator or TemplateNarrator()
    started = now_utc()
    deps = ObserverDeps(
        db=db,
        narrator=narrator,
        sinks=sinks if sinks is not None else build_sinks(settings, db=db),
        run_id=new_run_id(),
        started_at=started,
        on_stage=on_stage,
    )
    initial: ObserverState = {
        "now": now or started,
        "thresholds": ObserverThresholds.from_settings(settings),
        "expected_symbols": tuple(registry.symbols),
        "findings": [],
        "errors": [],
        "stages": [],
    }
    try:
        final = build_observer_graph(deps).invoke(initial)
        report: AgentReport = final["report"]
        return report
    except Exception as exc:
        service_logger.exception("Observer graph failed")
        report = finish_report(
            AGENT, deps.run_id, started, [], narrator.narrate, error=f"{type(exc).__name__}: {exc}"
        )
        publish(report, deps.sinks)
        return report
