"""The observer graph end to end against a fake database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pytest

from candlegraph.agents.base import Severity
from candlegraph.agents.observer import build_observer_graph, run_observer
from candlegraph.agents.observer.collect import ObserverDeps
from candlegraph.agents.observer.state import ObserverThresholds
from candlegraph.agents.reporting import LedgerSink
from candlegraph.agents.scheduler import observer_tick
from candlegraph.storage import set_db
from candlegraph.tokens import TokenRegistry
from tests.conftest import FakeDatabase, make_settings
from tests.unit.agents.conftest import run_row

HOUR = 3600
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def price_rows(
    symbol: str, hours: int, end: datetime, volume: float = 1e6, recent_volume: float | None = None
) -> list[dict[str, Any]]:
    start = int(end.timestamp()) - hours * HOUR
    rng = np.random.default_rng(1)
    prices = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, hours)))
    rows = []
    for i in range(hours):
        unix = start + i * HOUR
        in_recent = unix >= int(end.timestamp()) - 24 * HOUR
        rows.append(
            {
                "symbol": symbol,
                "token_id": f"0x{symbol}",
                "period_start_unix": unix,
                "timestamp": datetime.fromtimestamp(unix, tz=UTC),
                "open": prices[i],
                "high": prices[i],
                "low": prices[i],
                "close": prices[i],
                "price_usd": prices[i],
                "volume_usd": (recent_volume if in_recent and recent_volume is not None else volume),
                "total_value_locked_usd": 1.0,
            }
        )
    return rows


def healthy_db(**overrides: Any) -> FakeDatabase:
    runs = [run_row(NOW - timedelta(minutes=5 * i)) for i in range(1, 40)]
    reads: dict[str, list[dict[str, Any]]] = {
        "get_recent_runs": runs,
        "get_last_successful_run": [runs[0]],
        "coverage_report": [
            {
                "token_id": "0xWBTC",
                "symbol": "WBTC",
                "stored_hours": 192,
                "expected_hours": 192,
                "first_unix": int(NOW.timestamp()) - 192 * HOUR,
                "last_unix": int(NOW.timestamp()) - HOUR,
            },
            {
                "token_id": "0xSHIB",
                "symbol": "SHIB",
                "stored_hours": 192,
                "expected_hours": 192,
                "first_unix": int(NOW.timestamp()) - 192 * HOUR,
                "last_unix": int(NOW.timestamp()) - HOUR,
            },
        ],
        "missing_hours": [],
        "ohlc_violations": [],
        "field_completeness": [
            {
                "rows_total": 384,
                "volume_present": 384,
                "volume_usd_present": 384,
                "tvl_usd_present": 384,
                "fees_usd_present": 384,
            }
        ],
        "get_price_history": price_rows("WBTC", 192, NOW) + price_rows("SHIB", 192, NOW),
    }
    reads.update(overrides)
    return FakeDatabase(**reads)


class DownDatabase(FakeDatabase):
    def fetch_all(self, query: Any, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        raise ConnectionError("connection refused")


def test_a_healthy_stack_produces_an_ok_report_and_records_it(registry: TokenRegistry) -> None:
    db = healthy_db()

    report = run_observer(make_settings(), db=db, registry=registry, now=NOW, sinks=[LedgerSink(db)])

    assert report.status == Severity.OK, [f.message for f in report.findings]
    assert {f.kind for f in report.findings} == {"fresh", "ledger", "coverage", "drift"}
    assert db.executed[0][0] == "insert_agent_run"
    assert "## Coverage" in report.markdown and "## Drift" in report.markdown
    assert report.detail["symbols_judged"] == 2


def test_volume_drift_in_the_recent_window_is_found(registry: TokenRegistry) -> None:
    db = healthy_db(
        get_price_history=price_rows("WBTC", 192, NOW) + price_rows("SHIB", 192, NOW, recent_volume=6e6)
    )

    report = run_observer(make_settings(), db=db, registry=registry, now=NOW, sinks=[])

    (drift,) = [f for f in report.findings if f.kind == "drift" and f.severity == Severity.WARNING]
    assert drift.subject == "SHIB" and "x6.0" in drift.message


def test_a_stale_ledger_is_a_warning(registry: TokenRegistry) -> None:
    old = run_row(NOW - timedelta(minutes=20))  # stale after 15m, critical after 30m
    db = healthy_db(get_recent_runs=[old], get_last_successful_run=[old])

    report = run_observer(make_settings(), db=db, registry=registry, now=NOW, sinks=[])

    assert report.status == Severity.WARNING
    assert [f.kind for f in report.findings if f.severity == Severity.WARNING] == ["stale"]


def test_a_down_database_short_circuits_to_one_critical_finding(registry: TokenRegistry) -> None:
    db = DownDatabase()

    report = run_observer(make_settings(), db=db, registry=registry, now=NOW, sinks=[])

    assert report.status == Severity.CRITICAL
    assert [f.kind for f in report.findings] == ["database"]
    assert report.detail["database_up"] is False
    assert "## Coverage" not in report.markdown


def test_the_graph_has_the_documented_shape(registry: TokenRegistry) -> None:
    """``docs/agents/run-observer.md`` draws this graph; keep them in step."""
    deps = ObserverDeps(healthy_db(), narrator=None, sinks=[], run_id="r", started_at=NOW)  # type: ignore[arg-type]
    drawing = build_observer_graph(deps).get_graph()

    assert set(drawing.nodes) == {
        "__start__",
        "collect_ledger",
        "collect_quality",
        "collect_metrics",
        "assess",
        "publish",
        "__end__",
    }
    edges = {(e.source, e.target) for e in drawing.edges}
    assert ("collect_ledger", "publish") in edges, "the database-down short-circuit"
    assert ("collect_ledger", "collect_quality") in edges
    assert ("assess", "publish") in edges and ("publish", "__end__") in edges


def test_a_crashing_graph_still_posts_an_error_report(
    registry: TokenRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = healthy_db()
    monkeypatch.setattr(
        "candlegraph.agents.observer.graph.build_observer_graph",
        lambda _deps: (_ for _ in ()).throw(RuntimeError("bad wiring")),
    )

    report = run_observer(make_settings(), db=db, registry=registry, now=NOW, sinks=[LedgerSink(db)])

    assert report.error == "RuntimeError: bad wiring"
    assert report.status_label == "error"
    assert db.executed[0][1]["status"] == "error"  # type: ignore[index]


def test_thresholds_come_from_settings() -> None:
    t = ObserverThresholds.from_settings(
        make_settings(data_poll_interval=60, observer_stale_intervals=5, observer_min_bars=6)
    )

    assert t.stale_after_seconds == 300.0 and t.min_bars == 6


def test_the_scheduler_tick_never_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    set_db(DownDatabase())
    monkeypatch.setattr(
        "candlegraph.agents.observer.graph.get_token_registry",
        lambda: (_ for _ in ()).throw(RuntimeError("no tokens")),
    )

    with caplog.at_level("ERROR", logger="candlegraph"):
        assert observer_tick(make_settings()) is None

    assert "Observer run failed" in caplog.text
