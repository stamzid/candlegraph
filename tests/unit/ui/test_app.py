"""Smoke tests that actually execute the Streamlit script.

``AppTest`` is Streamlit's own headless runner: it executes ``app.py`` top to
bottom, records what was rendered, and captures any exception. That makes it
possible to catch a broken widget call or a bad column name without a
browser, which is the failure mode the view-model tests cannot see.

One dashboard renders per run (the sidebar radio picks it), so each page gets
its own smoke test by setting the radio and re-running.

Skipped when the UI extra is not installed, so ``make test-unit`` still works
on a core install.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import numpy as np
import pytest

from candlegraph.ingestion.runner import IngestResult
from candlegraph.settings import REPO_ROOT
from candlegraph.storage import set_db
from tests.conftest import FakeDatabase

pytest.importorskip("streamlit", reason="the UI extra is not installed")

from streamlit.testing.v1 import AppTest

# AppTest resolves relative paths against *this* file, so be absolute.
APP = str(REPO_ROOT / "candlegraph" / "ui" / "app.py")
HOUR = 3600
BASE = 1_700_000_000 // (24 * HOUR) * (24 * HOUR)
# ARIMA refits make the strategy page slow when selected.
TIMEOUT = 120

PAGES = ("Market", "Token", "Risk", "Compare", "Portfolio", "Strategy", "Forecast", "Agents", "News", "Data")


def price_rows(symbol: str, n: int, seed: int = 3) -> list[dict[str, Any]]:
    rng = np.random.default_rng(seed)
    walk = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.01, n)))
    return [
        {
            "symbol": symbol,
            "token_id": f"0x{symbol.lower()}",
            "period_start_unix": BASE + i * HOUR,
            "timestamp": datetime.fromtimestamp(BASE + i * HOUR, tz=UTC),
            "open": Decimal(str(p)),
            "high": Decimal(str(p * 1.01)),
            "low": Decimal(str(p * 0.99)),
            "close": Decimal(str(p)),
            "price_usd": Decimal(str(p)),
            "volume_usd": Decimal("1000"),
            "total_value_locked_usd": Decimal("5000"),
        }
        for i, p in enumerate(walk)
    ]


def coverage(symbol: str, n: int) -> dict[str, Any]:
    return {
        "token_id": f"0x{symbol.lower()}",
        "symbol": symbol,
        "stored_hours": n,
        "first_unix": BASE,
        "last_unix": BASE + (n - 1) * HOUR,
        "expected_hours": n,
    }


def agent_run_row() -> dict[str, Any]:
    return {
        "run_id": "r-1",
        "agent": "observer",
        "status": "warning",
        "started_at": datetime(2026, 10, 30, 12, 0, tzinfo=UTC),
        "finished_at": datetime(2026, 10, 30, 12, 0, 5, tzinfo=UTC),
        "duration_seconds": 5.0,
        "findings_total": 1,
        "summary": "WETH: 7h gap on a liquid token",
        "report": "# observer",
        "findings": [
            {"kind": "gap", "severity": "warning", "subject": "WETH", "message": "7h gap", "evidence": {}}
        ],
        "detail": {},
    }


def source_call_row(status: str = "ok") -> dict[str, Any]:
    return {
        "called_at": datetime(2026, 11, 1, 14, 0, tzinfo=UTC),
        "status": status,
        "credits": 1,
        "results": 5,
        "query": "WBTC Wrapped BTC Bitcoin crypto news",
        "error": None,
    }


def ingest_run_row() -> dict[str, Any]:
    return {
        "run_id": "i-1",
        "trigger": "scheduled",
        "status": "succeeded",
        "started_at": datetime(2026, 10, 30, 12, 0, tzinfo=UTC),
        "token_hours_written": 100,
        "pool_hours_written": 300,
        "duration_seconds": 3.0,
    }


@pytest.fixture(autouse=True)
def _clear_streamlit_cache() -> None:
    """The symbol list is cached for five minutes; tests must not share it."""
    import streamlit as st

    st.cache_data.clear()


SYMBOLS = ("AAVE", "USDT", "WBTC", "WETH")


def seeded_db(bars: int = 300) -> FakeDatabase:
    db = FakeDatabase(
        get_price_history=[row for i, s in enumerate(SYMBOLS) for row in price_rows(s, bars, seed=3 + i)],
        coverage_report=[coverage(s, bars) for s in SYMBOLS],
        get_recent_agent_runs=[agent_run_row()],
        get_recent_runs=[ingest_run_row()],
        get_recent_source_calls=[source_call_row()],
        source_spend=[{"month": 12, "today": 2}],
        source_call_daily_stats=[
            {
                "day": "2026-11-01",
                "calls": 3,
                "credits": 3,
                "results": 14,
                "ok": 2,
                "errors": 1,
                "rate_limited": 0,
                "refused": 0,
            }
        ],
        count_source_documents=[
            {"documents": 9, "chunks": 40, "embedded": 30, "undated": 2, "last_retrieved_at": None}
        ],
        count_embedding_backlog=[{"missing": 10, "stale": 0, "total": 40}],
    )
    set_db(db)
    return db


def run_app(page: str | None = None) -> AppTest:
    seeded_db()
    app = AppTest.from_file(APP, default_timeout=TIMEOUT)
    app.run()
    if page is not None:
        app.sidebar.radio[0].set_value(page).run()
    return app


# --- every dashboard renders -------------------------------------------------


@pytest.mark.parametrize("page", PAGES)
def test_each_dashboard_renders_without_an_exception(page: str) -> None:
    """Catches broken widget calls and bad column names the view-models cannot."""
    app = run_app(page)

    assert not app.exception, [str(e) for e in app.exception]


def test_the_sidebar_offers_every_dashboard() -> None:
    app = run_app()

    assert tuple(app.sidebar.radio[0].options) == PAGES


# --- units are stated where they are entered ---------------------------------


def test_rates_are_entered_in_percent_and_say_so() -> None:
    """The old input took 0.04 with no unit; a trader could read it as 4 bps."""
    app = run_app()

    rate = next(w for w in app.sidebar.number_input if "Risk-free" in w.label)
    assert "% / yr" in rate.label
    assert "not basis points" in (rate.help or "")


def test_costs_are_entered_in_basis_points_and_say_so() -> None:
    app = run_app()

    labels = {w.label for w in app.sidebar.number_input}
    assert any("Fee (bps" in label for label in labels)
    assert any("Slippage (bps" in label for label in labels)


def test_the_percent_rate_is_converted_before_it_reaches_a_sharpe() -> None:
    """Entering 5 must mean 5% a year (0.05), not 500%."""
    app = run_app("Portfolio")

    rate = next(w for w in app.sidebar.number_input if "Risk-free" in w.label)
    rate.set_value(5.0).run()

    assert not app.exception
    # The portfolio metric help names the decimal rate the ratio used.
    helps = " ".join((m.help or "") for m in app.main.metric)
    assert "5.00%/yr" in helps


def test_the_real_rate_is_shown_beside_the_nominal_one() -> None:
    app = run_app()

    inflation = next(w for w in app.sidebar.number_input if "Inflation" in w.label)
    inflation.set_value(5.0).run()

    captions = " ".join(c.value for c in app.sidebar.caption)
    assert "real" in captions


def test_the_history_window_can_be_chosen() -> None:
    app = run_app()

    assert any("History" in widget.label for widget in app.sidebar.selectbox)


# --- the agents dashboard -----------------------------------------------------


def test_the_agents_dashboard_offers_an_on_demand_observer_run() -> None:
    app = run_app("Agents")

    assert any("Run observer" in b.label for b in app.main.button)


def test_pressing_the_button_runs_the_observer_and_writes_the_ledger() -> None:
    seeded_db()
    app = AppTest.from_file(APP, default_timeout=TIMEOUT)
    app.run()
    app.sidebar.radio[0].set_value("Agents").run()

    db = seeded_db()  # fresh fake wired in for the click's re-run
    button = next(b for b in app.main.button if "Run observer" in b.label)
    button.click().run()

    assert not app.exception
    assert "insert_agent_run" in [label for label, _ in db.executed]


def test_the_latest_findings_are_on_screen() -> None:
    app = run_app("Agents")

    frames = app.main.dataframe
    assert frames, "the findings table must render"
    assert not app.exception


# --- empty and broken states ----------------------------------------------


def test_an_empty_database_asks_the_user_to_backfill() -> None:
    """The first thing a new user sees must tell them what to do."""
    set_db(FakeDatabase())

    app = AppTest.from_file(APP, default_timeout=TIMEOUT)
    app.run()

    assert not app.exception
    assert any("backfill" in warning.value for warning in app.warning)


def test_an_unreachable_database_is_reported_not_raised() -> None:
    class BrokenDatabase(FakeDatabase):
        def fetch_all(self, query: Any, params: Any = None) -> list[dict[str, Any]]:
            raise RuntimeError("could not connect to server")

    set_db(BrokenDatabase())

    app = AppTest.from_file(APP, default_timeout=TIMEOUT)
    app.run()

    assert not app.exception
    assert any("Cannot reach the database" in error.value for error in app.error)


def test_too_little_history_is_explained_rather_than_crashing() -> None:
    """A ViewError must land as an info box, not a traceback."""
    db = FakeDatabase(
        get_price_history=price_rows("WETH", 5),
        coverage_report=[coverage("WETH", 5)],
    )
    set_db(db)

    app = AppTest.from_file(APP, default_timeout=TIMEOUT)
    app.run()
    app.sidebar.radio[0].set_value("Token").run()

    assert not app.exception
    assert app.info


# --- interaction -----------------------------------------------------------


def test_widening_the_history_window_re_runs_cleanly() -> None:
    app = run_app("Market")

    window = next(w for w in app.sidebar.selectbox if "History" in w.label)
    window.set_value("Everything").run()

    assert not app.exception


# --- defaults a trader expects -----------------------------------------------------


@pytest.mark.parametrize("page", ["Token", "Risk", "Strategy", "Forecast"])
def test_single_token_pages_open_on_wbtc_not_the_alphabet(page: str) -> None:
    app = run_app(page)

    token = next(w for w in app.main.selectbox if w.label == "Token")
    assert token.value == "WBTC"


def test_compare_opens_on_wbtc_against_usdt() -> None:
    app = run_app("Compare")

    a = next(w for w in app.main.selectbox if w.label == "Token A")
    b = next(w for w in app.main.selectbox if w.label == "Token B")
    assert (a.value, b.value) == ("WBTC", "USDT")


def test_the_portfolio_opens_on_the_majors_without_stablecoins_at_a_quarter_cap() -> None:
    app = run_app("Portfolio")

    tokens = app.main.multiselect[0]
    assert "USDT" not in tokens.value
    assert tokens.value[0] == "WBTC"
    cap = next(w for w in app.main.slider if "Max weight" in w.label)
    # Three non-stable tokens: 25% cannot sum to one, so the slider opens at the feasible 35%.
    assert cap.value == pytest.approx(0.35)


# --- the data dashboard ------------------------------------------------------------


def test_the_data_page_offers_a_backfill_but_does_not_start_one() -> None:
    app = run_app("Data")

    assert any(b.label.startswith("Backfill") for b in app.main.button)
    assert not any("Confirm" in b.label for b in app.main.button)
    assert not app.warning


def test_asking_for_a_backfill_shows_a_confirmation_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing hits the network until the user confirms."""
    called: list[int] = []
    monkeypatch.setattr(
        "candlegraph.ui.viewmodels.run_backfill",
        lambda settings, db, days: called.append(days) or IngestResult(hours_written=1),
    )
    app = run_app("Data")

    window = next(w for w in app.main.selectbox if w.label == "Window")
    window.set_value("30 days").run()
    next(b for b in app.main.button if b.label.startswith("Backfill")).click().run()

    assert not app.exception
    assert any("Backfill 30 days" in w.value for w in app.warning)
    assert any("Confirm" in b.label for b in app.main.button)
    assert called == [], "the backfill must not start before confirmation"


def test_confirming_runs_the_backfill_and_reports_what_landed(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[int] = []

    def fake(settings: Any, db: Any, days: int) -> IngestResult:
        called.append(days)
        return IngestResult(hours_written=4200, pool_hours_written=700)

    monkeypatch.setattr("candlegraph.ui.viewmodels.run_backfill", fake)
    app = run_app("Data")
    next(b for b in app.main.button if b.label.startswith("Backfill")).click().run()
    next(b for b in app.main.button if "Confirm" in b.label).click().run()

    assert not app.exception
    assert called == [90]
    assert any("4,200 token hours" in m.value for m in app.success)


def test_cancelling_a_backfill_runs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[int] = []
    monkeypatch.setattr(
        "candlegraph.ui.viewmodels.run_backfill",
        lambda settings, db, days: called.append(days) or IngestResult(),
    )
    app = run_app("Data")
    next(b for b in app.main.button if b.label.startswith("Backfill")).click().run()
    next(b for b in app.main.button if b.label == "Cancel").click().run()

    assert not app.exception
    assert called == []
    assert not any("Confirm" in b.label for b in app.main.button)


# --- the news dashboard --------------------------------------------------------------


def test_the_agents_page_says_whether_the_news_runs_succeeded() -> None:
    app = run_app("Agents")

    labels = {m.label for m in app.main.metric}
    assert "News source (Tavily)" in labels
    assert "News runs failed" in labels
    source = next(m for m in app.main.metric if m.label == "News source (Tavily)")
    assert source.value == "OK"


def test_the_news_page_shows_the_budget_the_ledger_and_the_harvest() -> None:
    app = run_app("News")

    assert not app.exception
    labels = {m.label for m in app.main.metric}
    assert {"Month", "Today", "Reserve", "Documents", "Last call"} <= labels
    month = next(m for m in app.main.metric if m.label == "Month")
    assert month.value == "12 / 1000"
    assert any("await embedding" in i.value for i in app.info)


def test_a_news_fetch_needs_confirmation_before_any_credit_is_spent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[Any] = []
    monkeypatch.setattr(
        "candlegraph.ui.viewmodels.run_news_fetch",
        lambda db, settings, symbols=None: called.append(symbols),
    )
    app = run_app("News")

    next(b for b in app.main.button if b.label.startswith("Fetch news")).click().run()

    assert not app.exception
    assert any("Search Tavily" in w.value for w in app.warning)
    assert any("Confirm fetch" in b.label for b in app.main.button)
    assert called == [], "nothing is spent before the user confirms"


def test_confirming_a_news_fetch_runs_it_and_reports_the_outcomes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from candlegraph.knowledge.news import FetchOutcome, NewsFetchSummary

    called: list[Any] = []

    def fake(db: Any, settings: Any, symbols: Any = None) -> NewsFetchSummary:
        called.append(symbols)
        return NewsFetchSummary(
            day="2026-11-01",
            outcomes=[FetchOutcome("WBTC", "ok", credits=1, results=5, documents_stored=3, chunks_written=9)],
        )

    monkeypatch.setattr("candlegraph.ui.viewmodels.run_news_fetch", fake)
    app = run_app("News")
    next(b for b in app.main.button if b.label.startswith("Fetch news")).click().run()
    next(b for b in app.main.button if "Confirm fetch" in b.label).click().run()

    assert not app.exception
    assert called == [None]
    assert any("Spent 1 credit" in m.value for m in app.success)


def test_a_news_fetch_without_a_key_is_an_error_box_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from candlegraph.knowledge.news import NewsError

    def refuse(db: Any, settings: Any, symbols: Any = None) -> Any:
        raise NewsError("TAVILY_API_KEY is not set")

    monkeypatch.setattr("candlegraph.ui.viewmodels.run_news_fetch", refuse)
    app = run_app("News")
    next(b for b in app.main.button if b.label.startswith("Fetch news")).click().run()
    next(b for b in app.main.button if "Confirm fetch" in b.label).click().run()

    assert not app.exception
    assert any("TAVILY_API_KEY" in e.value for e in app.error)
