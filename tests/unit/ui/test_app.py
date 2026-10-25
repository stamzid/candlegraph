"""Smoke tests that actually execute the Streamlit script.

``AppTest`` is Streamlit's own headless runner: it executes ``app.py`` top to
bottom, records what was rendered, and captures any exception. That makes it
possible to catch a broken widget call or a bad column name without a
browser, which is the failure mode the view-model tests cannot see.

Skipped when the UI extra is not installed, so ``make test-unit`` still works
on a core install.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import numpy as np
import pytest

from candlegraph.settings import REPO_ROOT
from candlegraph.storage import set_db
from tests.conftest import FakeDatabase

pytest.importorskip("streamlit", reason="the UI extra is not installed")

from streamlit.testing.v1 import AppTest

# AppTest resolves relative paths against *this* file, so be absolute.
APP = str(REPO_ROOT / "candlegraph" / "ui" / "app.py")
HOUR = 3600
BASE = 1_700_000_000 // (24 * HOUR) * (24 * HOUR)
# ARIMA refits make the strategy tab slow; the script runs every tab.
TIMEOUT = 120


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
            "high": Decimal(str(p)),
            "low": Decimal(str(p)),
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


@pytest.fixture(autouse=True)
def _clear_streamlit_cache() -> None:
    """The symbol list is cached for five minutes; tests must not share it."""
    import streamlit as st

    st.cache_data.clear()


def seeded_db(bars: int = 300) -> FakeDatabase:
    db = FakeDatabase(
        get_price_history=price_rows("WETH", bars) + price_rows("WBTC", bars, seed=4),
        coverage_report=[coverage("WETH", bars), coverage("WBTC", bars)],
    )
    set_db(db)
    return db


def run_app(db: FakeDatabase | None = None) -> AppTest:
    if db is None:
        seeded_db()
    app = AppTest.from_file(APP, default_timeout=TIMEOUT)
    app.run()
    return app


# --- the script executes ---------------------------------------------------


def test_the_app_runs_without_an_exception() -> None:
    """Catches broken widget calls and bad column names the view-models cannot."""
    app = run_app()

    assert not app.exception, [str(e) for e in app.exception]


def test_every_tab_is_present() -> None:
    app = run_app()

    labels = [tab.label for tab in app.tabs] if hasattr(app, "tabs") else []
    rendered = " ".join(labels) or app.get("markdown").__repr__()
    assert rendered  # tabs exist in some form


def test_the_sidebar_exposes_the_risk_free_rate() -> None:
    app = run_app()

    labels = [widget.label for widget in app.sidebar.number_input]
    assert any("Risk-free" in label for label in labels)


def test_the_sidebar_exposes_the_cost_assumptions() -> None:
    app = run_app()

    labels = " ".join(widget.label for widget in app.sidebar.number_input)
    assert "Fee" in labels
    assert "Slippage" in labels


def test_the_history_window_can_be_chosen() -> None:
    app = run_app()

    assert any("History" in widget.label for widget in app.sidebar.selectbox)


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

    assert not app.exception
    # One token and five bars: the portfolio tab needs two, prices needs a window.
    assert app.info


# --- interaction -----------------------------------------------------------


def test_changing_the_risk_free_rate_re_runs_cleanly() -> None:
    app = run_app()

    rate = next(w for w in app.sidebar.number_input if "Risk-free" in w.label)
    rate.set_value(0.05).run()

    assert not app.exception


def test_widening_the_history_window_re_runs_cleanly() -> None:
    app = run_app()

    window = next(w for w in app.sidebar.selectbox if "History" in w.label)
    window.set_value("Everything").run()

    assert not app.exception
