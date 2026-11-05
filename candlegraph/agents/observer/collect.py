"""The nodes that read: ledger, quality report, and metric windows.

Each one catches its own failures and records them in ``errors`` so the
graph always reaches ``publish``; a report that says "could not read the
ledger" is worth more than a stack trace in a log nobody tails.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from candlegraph.agents.base import Finding, Severity
from candlegraph.agents.narrator import Narrator
from candlegraph.agents.observer.assess import run_metrics, symbol_metrics
from candlegraph.agents.observer.state import KnowledgeFacts, ObserverState, SymbolMetrics
from candlegraph.agents.reporting import Sink
from candlegraph.agents.stages import StageHook
from candlegraph.analytics.prices import load_price_history
from candlegraph.ingestion import ledger
from candlegraph.ingestion.quality import build_report
from candlegraph.knowledge.news import source_spend
from candlegraph.knowledge.sources.tavily import SOURCE_ID as TAVILY
from candlegraph.knowledge.statements import knowledge_statements_for
from candlegraph.storage import DatabaseManager

# Enough ledger rows to cover a week of five-minute polls with headroom.
LEDGER_ROWS = 2500
# Source calls to read for the failure-streak rule: at one search per token
# per UTC day, fifty rows is several days of a ten-token deployment.
SOURCE_CALL_ROWS = 50


@dataclass
class ObserverDeps:
    """What the nodes need that is not state: the database and the outputs."""

    db: DatabaseManager
    narrator: Narrator
    sinks: Sequence[Sink]
    run_id: str
    started_at: datetime
    on_stage: StageHook | None = None
    """Called with each stage's record as it finishes; the CLI prints them."""


def collect_ledger(deps: ObserverDeps, state: ObserverState) -> dict[str, Any]:
    now = state["now"]
    try:
        runs = ledger.recent_runs(deps.db, limit=LEDGER_ROWS)
        age = ledger.seconds_since_last_success(deps.db, now=now)
    except Exception as exc:
        return {
            "database_up": False,
            "errors": [f"ledger: {exc}"],
            "findings": [Finding("database", Severity.CRITICAL, "postgres", f"database unreachable: {exc}")],
        }
    return {"database_up": True, "runs": runs, "seconds_since_success": age}


def route_after_ledger(state: ObserverState) -> str:
    """Nothing else can be read if the first read failed."""
    return "collect_quality" if state.get("database_up") else "publish"


def collect_quality(deps: ObserverDeps, state: ObserverState) -> dict[str, Any]:
    t = state["thresholds"]
    now = state["now"]
    start_unix = int((now - timedelta(seconds=t.baseline_seconds + t.window_seconds)).timestamp())
    try:
        report = build_report(
            deps.db,
            expected_symbols=state.get("expected_symbols", ()),
            start_unix=start_unix,
            min_expected_coverage=t.min_expected_coverage,
            now=now,
        )
    except Exception as exc:
        return {"quality": None, "errors": [f"quality report: {exc}"]}
    return {"quality": report}


def collect_metrics(deps: ObserverDeps, state: ObserverState) -> dict[str, Any]:
    t = state["thresholds"]
    now = state["now"]
    split = now - timedelta(seconds=t.window_seconds)
    baseline_start = split - timedelta(seconds=t.baseline_seconds)
    symbols = list(state.get("expected_symbols", ()))
    try:
        history = load_price_history(
            deps.db, symbols, start_unix=int(baseline_start.timestamp()), end_unix=int(now.timestamp())
        )
    except Exception as exc:
        return {"metrics": {}, "run_metrics": None, "errors": [f"metrics: {exc}"]}

    metrics: dict[str, SymbolMetrics] = {}
    if not history.empty:
        split_unix = int(split.timestamp())
        for symbol, rows in history.frame.groupby("symbol", sort=True):
            metrics[str(symbol)] = symbol_metrics(str(symbol), rows, split_unix)
    return {
        "metrics": metrics,
        "run_metrics": run_metrics(state.get("runs", []), split_at=split, baseline_start=baseline_start),
    }


def collect_knowledge(deps: ObserverDeps, state: ObserverState) -> dict[str, Any]:
    """The knowledge layer's numbers: spend, call outcomes, backlog, citations.

    Reads only (invariant 21). A stack that has never used the knowledge
    layer yields zeros everywhere, which assess grades as the calm ``OK``
    it is — absence of news is not a fault.
    """
    t = state["thresholds"]
    now = state["now"]
    statements = knowledge_statements_for(deps.db.schema)
    try:
        month, today = source_spend(deps.db, TAVILY, now)
        calls = deps.db.fetch_all(
            statements.get_recent_source_calls, {"source_id": TAVILY, "limit": SOURCE_CALL_ROWS}
        )
        (backlog,) = deps.db.fetch_all(statements.count_embedding_backlog, {"model": t.embedding_model})
        since = now - timedelta(hours=t.drift_window_hours)
        (explanations,) = deps.db.fetch_all(statements.count_uncited_explanations, {"since": since})
    except Exception as exc:
        return {"knowledge": None, "errors": [f"knowledge: {exc}"]}
    return {
        "knowledge": KnowledgeFacts(
            source_id=TAVILY,
            month_spent=month,
            today_spent=today,
            monthly_cap=t.tavily_monthly_credits,
            daily_cap=t.tavily_daily_credits,
            call_statuses=tuple(str(row.get("status", "")) for row in calls),
            embedding_model=t.embedding_model,
            backlog_missing=int(backlog["missing"]),
            backlog_stale=int(backlog["stale"]),
            chunks_total=int(backlog["total"]),
            explanations_total=int(explanations["total"]),
            explanations_uncited=int(explanations["uncited"]),
            window_hours=t.drift_window_hours,
        )
    }
