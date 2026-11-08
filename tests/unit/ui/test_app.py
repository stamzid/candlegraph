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

PAGES = (
    "Market",
    "Token",
    "Quality",
    "Risk",
    "Compare",
    "Portfolio",
    "Strategy",
    "Simulation",
    "Forecast",
    "Agents",
    "News",
    "Data",
)


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


def test_compare_opens_on_two_volatile_majors() -> None:
    """WETH against WBTC, not a token against a dollar.

    It used to open on WBTC/USDT, which is a dollar chart — something the
    Token dashboard already draws. The question this screen answers (do
    these move together, and which one leads) needs a second leg that
    actually moves, so a stablecoin is the wrong default here even though it
    is still the right one for the Risk benchmark.
    """
    app = run_app("Compare")

    a = next(w for w in app.main.selectbox if w.label == "Token A")
    b = next(w for w in app.main.selectbox if w.label == "Token B")
    assert (a.value, b.value) == ("WETH", "WBTC")


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


# --- the quality dashboard renders a real gap --------------------------------


def gappy_db() -> FakeDatabase:
    """A token missing ten interior hours, with impossible rows to report."""
    db = FakeDatabase(
        get_price_history=[row for i, s in enumerate(SYMBOLS) for row in price_rows(s, 300, seed=3 + i)],
        coverage_report=[{**coverage(s, 300), "stored_hours": 290} for s in SYMBOLS],
        missing_hours=[{"period_start_unix": BASE + i * HOUR} for i in range(10, 20)],
        field_completeness=[
            {"rows_total": 300, "volume_present": 300, "tvl_usd_present": 90, "fees_usd_present": 0}
        ],
        ohlc_violations=[{"token_id": "0xweth", "period_start_unix": BASE, "high": 1.0, "low": 2.0}],
        get_recent_agent_runs=[],
        get_recent_runs=[],
    )
    set_db(db)
    return db


def test_the_quality_dashboard_renders_gaps_without_an_exception() -> None:
    """The empty-data smoke test cannot reach the gap map, the histogram or
    the violations table — every one of those needs a gap to exist first."""
    gappy_db()
    app = AppTest.from_file(APP, default_timeout=TIMEOUT)
    app.run()
    app.sidebar.radio[0].set_value("Quality").run()

    assert not app.exception, [str(e) for e in app.exception]
    rendered = " ".join(m.value for m in app.markdown)
    assert "Gap Map" in rendered
    assert "Field Completeness" in rendered
    assert "Impossible Rows" in rendered, "a violation was seeded and must be shown"
    # Both branches render the titles above, so without this the test would
    # pass on a page that found no gaps at all.
    infos = [i.value for i in app.info]
    assert not any("No gaps" in text for text in infos), "the seeded gap must reach the chart"
    assert any(m.label == "Gaps" and m.value != "0" for m in app.metric)


def test_the_quality_dashboard_says_a_gap_is_not_automatically_a_fault() -> None:
    """Invariant 19 on the one screen most likely to be read as an alarm."""
    gappy_db()
    app = AppTest.from_file(APP, default_timeout=TIMEOUT)
    app.run()
    app.sidebar.radio[0].set_value("Quality").run()

    text = " ".join(c.value for c in app.caption) + " ".join(m.value for m in app.markdown)
    assert "no row" in text or "no swaps" in text
    assert "fills a gap" in text, "the page must say it is read-only"


def test_the_token_dashboard_reports_coverage_alongside_the_price() -> None:
    gappy_db()
    app = AppTest.from_file(APP, default_timeout=TIMEOUT)
    app.run()
    app.sidebar.radio[0].set_value("Token").run()

    assert not app.exception, [str(e) for e in app.exception]
    assert any(m.label == "Coverage" for m in app.metric)


def test_the_compare_dashboard_shows_spearman_beside_pearson() -> None:
    """A wide gap between the two means outliers are driving the linear number."""
    app = run_app("Compare")

    labels = {m.label for m in app.metric}
    assert "Correlation (Pearson)" in labels
    assert "Correlation (Spearman)" in labels


# --- selections survive a dashboard switch -----------------------------------


def test_a_pair_choice_survives_leaving_the_dashboard_and_coming_back() -> None:
    """The reported bug, end to end through the real script.

    Compare WBTC against something, go to another view, come back: it used
    to be back on the defaults, because Streamlit discards a widget's state
    as soon as a rerun does not render it.
    """
    app = run_app("Compare")
    a = next(w for w in app.main.selectbox if w.label == "Token A")
    b = next(w for w in app.main.selectbox if w.label == "Token B")
    a.set_value("WBTC").run()
    next(w for w in app.main.selectbox if w.label == "Token B").set_value("USDT").run()

    app.sidebar.radio[0].set_value("Market").run()
    app.sidebar.radio[0].set_value("Compare").run()

    a = next(w for w in app.main.selectbox if w.label == "Token A")
    b = next(w for w in app.main.selectbox if w.label == "Token B")
    assert (a.value, b.value) == ("WBTC", "USDT"), "the pair came back as it was left"
    assert not app.exception, [str(e) for e in app.exception]


def test_the_history_window_is_remembered_too() -> None:
    app = run_app()
    window = next(w for w in app.sidebar.selectbox if w.label == "History")
    window.set_value("7 days").run()

    app.sidebar.radio[0].set_value("Token").run()
    app.sidebar.radio[0].set_value("Market").run()

    assert next(w for w in app.sidebar.selectbox if w.label == "History").value == "7 days"


def test_reset_returns_every_dashboard_to_its_defaults() -> None:
    app = run_app("Compare")
    next(w for w in app.main.selectbox if w.label == "Token A").set_value("WBTC").run()

    reset = next(b for b in app.sidebar.button if "Reset" in b.label)
    reset.set_value(True).run()

    app.sidebar.radio[0].set_value("Compare").run()
    assert next(w for w in app.main.selectbox if w.label == "Token A").value == "WETH"
    assert not app.exception, [str(e) for e in app.exception]


def test_the_reset_button_is_disabled_until_something_is_remembered() -> None:
    """Nothing to forget, so the control says so rather than doing nothing."""
    app = AppTest.from_file(APP, default_timeout=TIMEOUT)
    seeded_db()
    app.run()

    reset = next(b for b in app.sidebar.button if "Reset" in b.label)
    assert reset.disabled


def test_reset_leaves_a_pending_backfill_confirmation_alone() -> None:
    """Resetting selections must not cancel an action in flight."""
    app = run_app("Compare")
    # Something to reset, or the button is (correctly) disabled.
    next(w for w in app.main.selectbox if w.label == "Token A").set_value("WBTC").run()

    app.sidebar.radio[0].set_value("Data").run()
    next(b for b in app.main.button if b.label == "Backfill…").set_value(True).run()
    assert "backfill_pending" in app.session_state

    next(b for b in app.sidebar.button if "Reset" in b.label).set_value(True).run()

    assert "backfill_pending" in app.session_state, "a confirmation is not a preference"
    app.sidebar.radio[0].set_value("Compare").run()
    assert next(w for w in app.main.selectbox if w.label == "Token A").value == "WETH"


# --- choosing a gap estimator on the Quality dashboard ----------------------


def test_the_quality_dashboard_offers_all_three_estimators() -> None:
    gappy_db()
    app = AppTest.from_file(APP, default_timeout=TIMEOUT)
    app.run()
    app.sidebar.radio[0].set_value("Quality").run()

    picker = next(w for w in app.main.selectbox if w.label == "Estimator")
    assert list(picker.options) == ["linear", "causal", "bridge"]
    assert picker.value == "linear", "the default keeps existing behaviour"
    assert not app.exception, [str(e) for e in app.exception]


def test_selecting_a_causal_estimator_says_a_strategy_could_use_it() -> None:
    """The one distinction a reader must not get wrong, stated on selection."""
    gappy_db()
    app = AppTest.from_file(APP, default_timeout=TIMEOUT)
    app.run()
    app.sidebar.radio[0].set_value("Quality").run()

    next(w for w in app.main.selectbox if w.label == "Estimator").set_value("causal").run()

    assert any("could safely consume" in m.value for m in app.success)
    assert not app.exception, [str(e) for e in app.exception]


def test_selecting_a_look_ahead_estimator_warns_instead() -> None:
    gappy_db()
    app = AppTest.from_file(APP, default_timeout=TIMEOUT)
    app.run()
    app.sidebar.radio[0].set_value("Quality").run()

    next(w for w in app.main.selectbox if w.label == "Estimator").set_value("bridge").run()

    warnings = " ".join(w.value for w in app.warning)
    assert "Display only" in warnings and "could not have had" in warnings


def test_the_chosen_estimator_survives_a_dashboard_switch() -> None:
    """It drives the Token chart too, so forgetting it would change that page."""
    gappy_db()
    app = AppTest.from_file(APP, default_timeout=TIMEOUT)
    app.run()
    app.sidebar.radio[0].set_value("Quality").run()
    next(w for w in app.main.selectbox if w.label == "Estimator").set_value("bridge").run()

    app.sidebar.radio[0].set_value("Token").run()
    app.sidebar.radio[0].set_value("Quality").run()

    assert next(w for w in app.main.selectbox if w.label == "Estimator").value == "bridge"


# --- the backtest, made legible ---------------------------------------------


def test_the_strategy_dashboard_explains_how_the_backtest_works() -> None:
    """It used to show six numbers and no account of where they came from."""
    app = run_app("Strategy")

    text = " ".join(m.value for m in app.markdown)
    # The expander's body, which is where the rules actually are.
    assert "position is decided on one bar and earns the next" in text
    assert "shift" in text, "the execution lag must be stated, not assumed"
    assert "Costs are charged on position changes" in text
    assert "Read `excess` before `total return`" in text
    assert "losing backtest is usually the honest answer" in text
    assert not app.exception, [str(e) for e in app.exception]


def test_the_strategy_dashboard_shows_the_costs_it_is_charging() -> None:
    """They live in the sidebar, far from the result they drive."""
    app = run_app("Strategy")

    captions = " ".join(c.value for c in app.caption)
    assert "bps fee" in captions and "slippage" in captions


def test_zero_execution_lag_is_labelled_as_not_real() -> None:
    """A control that can produce a lie has to say so where it is used."""
    app = run_app("Strategy")
    next(w for w in app.main.number_input if "Execution lag" in w.label).set_value(0).run()

    errors = " ".join(e.value for e in app.error)
    assert "not real" in errors and "look-ahead" in errors
    assert "does not always flatter" in errors, "the bias has a direction that depends on the rule"


def test_the_strategy_dashboard_compares_every_rule_on_the_token() -> None:
    """`backtest --strategy all` printed this; the screen ran one at a time."""
    app = run_app("Strategy")

    text = " ".join(m.value for m in app.markdown)
    assert "Strategy Comparison" in text
    assert "Where the Edge Went" in text
    assert "Look-ahead Check" in text
    assert not app.exception, [str(e) for e in app.exception]


# --- the simulation dashboard ------------------------------------------------


def test_the_simulation_dashboard_offers_both_questions() -> None:
    """They answer different doubts and must not read as one control."""
    app = run_app("Simulation")

    text = " ".join(m.value for m in app.markdown)
    captions = " ".join(c.value for c in app.caption)
    assert "sample of size one" in captions
    assert "Monte Carlo" in captions or "Monte Carlo" in text
    assert not app.exception, [str(e) for e in app.exception]


def test_the_simulation_dashboard_does_not_run_until_asked() -> None:
    """Hundreds of backtests on every widget change would make the page
    unusable; Streamlit re-runs the script on each one."""
    app = run_app("Simulation")

    captions = " ".join(c.value for c in app.caption)
    assert "Press to" in captions
    assert not app.exception, [str(e) for e in app.exception]


def test_the_generator_picker_says_what_each_one_destroys() -> None:
    app = run_app("Simulation")

    picker = next(w for w in app.main.selectbox if w.label == "Path generator")
    assert list(picker.options) == ["block", "iid", "gbm", "regime"]
    assert picker.value == "block", "the default must not be IID"


# --- the execution panel, made checkable on screen ---------------------------


def test_the_execution_panel_states_the_lifecycle_and_the_offset() -> None:
    """A reader should not have to open the source to match the engine
    against the theory, which is what prompted this panel."""
    app = run_app("Simulation")

    text = " ".join(m.value for m in app.markdown)
    assert "decide" in text and "delay" in text and "fill" in text
    assert "execution_lag=k + 1" in text or "execution_lag = 1" in text
    assert "stale" in text, "filling stale decisions is the thing shift(k) never says"
    assert not app.exception, [str(e) for e in app.exception]


def test_the_execution_panel_shows_a_worked_example_without_being_asked() -> None:
    """The reconciliation proves correctness; the walkthrough shows the
    mechanics, and it is useless behind a button nobody presses."""
    app = run_app("Simulation")

    text = " ".join(m.value for m in app.markdown)
    assert "Worked Example" in text
    assert any("latency_bars" in w.label for w in app.main.number_input)


def test_the_forecast_dashboard_offers_the_posterior() -> None:
    app = run_app("Forecast")

    text = " ".join(m.value for m in app.markdown)
    assert "Parameter Uncertainty" in text
    assert not app.exception, [str(e) for e in app.exception]
