"""Wiring the observer: collect, assess, publish.

The graph is deliberately linear with one short-circuit. If the database is
down there is nothing to assess, so ``collect_ledger`` routes straight to
``publish`` with its one critical finding.
"""

from __future__ import annotations

from datetime import datetime
from functools import partial
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from candlegraph.agents.base import AgentReport, new_run_id, now_utc
from candlegraph.agents.narrator import Narrator, TemplateNarrator
from candlegraph.agents.observer.assess import assess
from candlegraph.agents.observer.collect import (
    ObserverDeps,
    collect_ledger,
    collect_metrics,
    collect_quality,
    route_after_ledger,
)
from candlegraph.agents.observer.state import ObserverState, ObserverThresholds, SymbolMetrics
from candlegraph.agents.reporting import Sink, build_sinks, finish_report, publish
from candlegraph.settings import Settings
from candlegraph.storage import DatabaseManager, get_db
from candlegraph.tokens import TokenRegistry, get_token_registry
from candlegraph.utils.logging_utils import service_logger

AGENT = "observer"

ObserverGraph = CompiledStateGraph[ObserverState, None, ObserverState, ObserverState]


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
    sections = [
        ("Coverage", coverage_section(state)),
        ("Drift", drift_section(state)),
        ("Recent ingest runs", ledger_section(state)),
    ]
    detail = {
        "database_up": state.get("database_up", False),
        "seconds_since_success": state.get("seconds_since_success"),
        "symbols_judged": len(state.get("metrics", {})),
        "errors": state.get("errors", []),
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
    return {"report": report}


def build_observer_graph(deps: ObserverDeps) -> ObserverGraph:
    graph: StateGraph[ObserverState, None, ObserverState, ObserverState] = StateGraph(ObserverState)
    graph.add_node("collect_ledger", partial(collect_ledger, deps))
    graph.add_node("collect_quality", partial(collect_quality, deps))
    graph.add_node("collect_metrics", partial(collect_metrics, deps))
    graph.add_node("assess", assess)
    graph.add_node("publish", partial(publish_node, deps))

    graph.add_edge(START, "collect_ledger")
    graph.add_conditional_edges(
        "collect_ledger", route_after_ledger, {"collect_quality": "collect_quality", "publish": "publish"}
    )
    graph.add_edge("collect_quality", "collect_metrics")
    graph.add_edge("collect_metrics", "assess")
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
) -> AgentReport:
    """One observer run, posted to the sinks. Never raises for a failing graph."""
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
    )
    initial: ObserverState = {
        "now": now or started,
        "thresholds": ObserverThresholds.from_settings(settings),
        "expected_symbols": tuple(registry.symbols),
        "findings": [],
        "errors": [],
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
