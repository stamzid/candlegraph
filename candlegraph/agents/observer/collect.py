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
from candlegraph.agents.observer.state import ObserverState, SymbolMetrics
from candlegraph.agents.reporting import Sink
from candlegraph.analytics.prices import load_price_history
from candlegraph.ingestion import ledger
from candlegraph.ingestion.quality import build_report
from candlegraph.storage import DatabaseManager

# Enough ledger rows to cover a week of five-minute polls with headroom.
LEDGER_ROWS = 2500


@dataclass
class ObserverDeps:
    """What the nodes need that is not state: the database and the outputs."""

    db: DatabaseManager
    narrator: Narrator
    sinks: Sequence[Sink]
    run_id: str
    started_at: datetime


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
