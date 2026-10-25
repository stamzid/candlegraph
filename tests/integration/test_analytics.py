"""Analytics against a real database: the signal table and the price query."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pandas as pd
import pytest

from candlegraph.analytics.prices import load_price_frame, load_price_history
from candlegraph.analytics.rolling import log_returns
from candlegraph.storage import DatabaseManager
from candlegraph.strategies import backtest, run_strategy
from candlegraph.strategies.bollinger import BollingerConfig
from candlegraph.strategies.store import signals_to_rows, store_signals
from tests.conftest import SHIB, WBTC

pytestmark = pytest.mark.integration

HOUR = 3600
BASE = 1_700_000_000 // (24 * HOUR) * (24 * HOUR)
NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)


def hour_record(index: int, price: str, token: str = WBTC, symbol: str = "WBTC") -> dict[str, Any]:
    period_start = BASE + index * HOUR
    return {
        "id": token,
        "symbol": symbol,
        "open": price,
        "high": price,
        "low": price,
        "close": price,
        "priceUSD": price,
        "volume": "1",
        "volumeUSD": "1000",
        "untrackedVolumeUSD": "1000",
        "totalValueLocked": "10",
        "totalValueLockedUSD": "5000",
        "feesUSD": "3",
        "periodStartUnix": period_start,
        "timestamp": datetime.fromtimestamp(period_start, tz=UTC),
    }


def seed(db: DatabaseManager, count: int = 120) -> None:
    """A gentle zig-zag, so rolling statistics have something to measure."""
    rows = [hour_record(i, f"{100 + (i % 10):.4f}") for i in range(count)] + [
        hour_record(i, f"{50 + (i % 7):.4f}", token=SHIB, symbol="SHIB") for i in range(count)
    ]
    db.upsert_many(db.sql.insert_token_hours, db.sql.insert_token_hours_template, rows)


@pytest.fixture
def seeded(db: DatabaseManager) -> DatabaseManager:
    db.execute(f'TRUNCATE "{db.schema}".signal')
    seed(db)
    return db


# --- migration shape -------------------------------------------------------


def test_the_signal_table_exists(seeded: DatabaseManager) -> None:
    rows = seeded.fetch_all(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = %(schema)s",
        {"schema": seeded.schema},
    )

    assert "signal" in {row["table_name"] for row in rows}


def test_the_signal_natural_key_is_in_place(seeded: DatabaseManager) -> None:
    rows = seeded.fetch_all(
        """
        SELECT conname FROM pg_constraint
        WHERE conrelid = (quote_ident(%(schema)s) || '.signal')::regclass AND contype = 'u'
        """,
        {"schema": seeded.schema},
    )

    assert "uix_signal_strategy_symbol_period" in {row["conname"] for row in rows}


def test_an_out_of_range_position_is_rejected_by_the_database(seeded: DatabaseManager) -> None:
    """A CHECK constraint, because -1/0/1 is the whole domain."""
    import psycopg2

    row = {
        "strategy": "x",
        "symbol": "WBTC",
        "period_start_unix": BASE,
        "timestamp": datetime.fromtimestamp(BASE, tz=UTC),
        "signal": 7,
        "score": None,
        "params": "{}",
        "generated_at": NOW,
    }

    with pytest.raises(psycopg2.Error):
        seeded.upsert_many(seeded.sql.insert_signal, seeded.sql.insert_signal_template, [row])


# --- the price query -------------------------------------------------------


def test_price_history_comes_back_for_the_requested_symbols(seeded: DatabaseManager) -> None:
    history = load_price_history(seeded, ["WBTC"])

    assert not history.empty
    assert set(history.frame["symbol"].unique()) == {"WBTC"}
    assert len(history.frame) == 120


def test_the_wide_frame_aligns_two_tokens(seeded: DatabaseManager) -> None:
    frame = load_price_frame(seeded, ["WBTC", "SHIB"])

    assert set(frame.columns) == {"WBTC", "SHIB"}
    assert frame.notna().all().all()
    assert str(frame.index.tz) == "UTC"


def test_the_window_filters_the_query(seeded: DatabaseManager) -> None:
    everything = load_price_history(seeded, ["WBTC"])
    clipped = load_price_history(seeded, ["WBTC"], start_unix=BASE + 100 * HOUR)

    assert len(clipped.frame) == 20
    assert len(everything.frame) == 120


def test_prices_arrive_as_floats_not_decimals(seeded: DatabaseManager) -> None:
    """Decimal silently breaks numpy's vectorised paths."""
    frame = load_price_frame(seeded, ["WBTC"])

    assert frame["WBTC"].dtype == "float64"


def test_an_unknown_symbol_yields_nothing(seeded: DatabaseManager) -> None:
    assert load_price_history(seeded, ["NOPE"]).empty


# --- end to end: prices in, stored signals out -----------------------------


def test_a_strategy_runs_off_stored_prices(seeded: DatabaseManager) -> None:
    prices = load_price_frame(seeded, ["WBTC"])["WBTC"].rename("WBTC")

    result = run_strategy("bollinger", prices, BollingerConfig(window=10, entry_z=1.0, exit_z=0.2))

    assert len(result.frame) == 120
    assert set(result.signals.unique()) <= {-1, 0, 1}


def test_signals_round_trip_through_the_table(seeded: DatabaseManager) -> None:
    prices = load_price_frame(seeded, ["WBTC"])["WBTC"].rename("WBTC")
    result = run_strategy("bollinger", prices, BollingerConfig(window=10, entry_z=1.0, exit_z=0.2))

    written = store_signals(seeded, result, generated_at=NOW)

    assert written == 120
    stored = seeded.fetch_all(
        seeded.sql.get_signals, {"strategy": "bollinger", "symbol": "WBTC", "start_unix": None}
    )
    assert len(stored) == 120
    assert {row["signal"] for row in stored} <= {-1, 0, 1}
    assert stored[0]["params"]["window"] == 10


def test_re_running_a_strategy_updates_rather_than_duplicates(seeded: DatabaseManager) -> None:
    prices = load_price_frame(seeded, ["WBTC"])["WBTC"].rename("WBTC")
    tight = run_strategy("bollinger", prices, BollingerConfig(window=10, entry_z=1.0, exit_z=0.2))
    loose = run_strategy("bollinger", prices, BollingerConfig(window=10, entry_z=3.0, exit_z=0.2))

    store_signals(seeded, tight, generated_at=NOW)
    store_signals(seeded, loose, generated_at=NOW)

    [count] = seeded.fetch_all(f'SELECT COUNT(*) AS n FROM "{seeded.schema}".signal')
    assert count["n"] == 120
    stored = seeded.fetch_all(
        seeded.sql.get_signals, {"strategy": "bollinger", "symbol": "WBTC", "start_unix": None}
    )
    assert stored[0]["params"]["entry_z"] == 3.0


def test_two_strategies_coexist_for_one_symbol(seeded: DatabaseManager) -> None:
    prices = load_price_frame(seeded, ["WBTC"])["WBTC"].rename("WBTC")

    store_signals(seeded, run_strategy("bollinger", prices), generated_at=NOW)
    store_signals(seeded, run_strategy("crossover", prices), generated_at=NOW)

    rows = seeded.fetch_all(seeded.sql.get_strategies)

    assert {row["strategy"] for row in rows} == {"bollinger", "crossover"}
    assert all(row["symbols"] == 1 for row in rows)


def test_the_latest_signal_per_strategy_and_symbol_is_queryable(seeded: DatabaseManager) -> None:
    """What a live trader or the phase-4 observer would actually read."""
    for symbol in ("WBTC", "SHIB"):
        prices = load_price_frame(seeded, [symbol])[symbol].rename(symbol)
        store_signals(seeded, run_strategy("bollinger", prices), generated_at=NOW)

    latest = seeded.fetch_all(seeded.sql.get_latest_signals)

    assert len(latest) == 2
    assert {row["symbol"] for row in latest} == {"WBTC", "SHIB"}
    assert all(row["period_start_unix"] == BASE + 119 * HOUR for row in latest)


def test_a_null_score_survives_the_round_trip(seeded: DatabaseManager) -> None:
    """Before the warm-up there is no opinion; that must not become 0."""
    prices = load_price_frame(seeded, ["WBTC"])["WBTC"].rename("WBTC")
    result = run_strategy("bollinger", prices, BollingerConfig(window=20))

    store_signals(seeded, result, generated_at=NOW)

    stored = seeded.fetch_all(
        seeded.sql.get_signals, {"strategy": "bollinger", "symbol": "WBTC", "start_unix": None}
    )
    assert stored[0]["score"] is None
    assert stored[-1]["score"] is not None


def test_only_non_flat_bars_can_be_stored(seeded: DatabaseManager) -> None:
    prices = load_price_frame(seeded, ["WBTC"])["WBTC"].rename("WBTC")
    result = run_strategy("bollinger", prices, BollingerConfig(window=10, entry_z=1.0, exit_z=0.2))

    written = store_signals(seeded, result, generated_at=NOW, include_flat=False)

    assert 0 < written < 120
    stored = seeded.fetch_all(
        seeded.sql.get_signals, {"strategy": "bollinger", "symbol": "WBTC", "start_unix": None}
    )
    assert all(row["signal"] != 0 for row in stored)


def test_a_backtest_runs_from_stored_data_end_to_end(seeded: DatabaseManager) -> None:
    """The phase-3 exit criterion, exercised against real rows."""
    prices = load_price_frame(seeded, ["WBTC"])["WBTC"].rename("WBTC")
    result = run_strategy("bollinger", prices, BollingerConfig(window=10, entry_z=1.0, exit_z=0.2))

    report = backtest(result)

    assert report.bars == 120
    assert report.trades > 0
    assert -1.0 <= report.max_drawdown <= 0.0
    assert report.equity.notna().all()


def test_a_portfolio_optimises_over_stored_prices(seeded: DatabaseManager) -> None:
    from candlegraph.analytics.portfolio import PortfolioConfig, optimise_portfolio

    prices = load_price_frame(seeded, ["WBTC", "SHIB"])
    returns = prices.apply(log_returns).dropna(how="all")

    result = optimise_portfolio(returns, PortfolioConfig(risk_free_rate=0.04))

    assert set(result.weights) == {"WBTC", "SHIB"}
    assert sum(result.weights.values()) == pytest.approx(1.0)
    assert result.risk_free_rate == 0.04


def test_signal_rows_match_the_stored_bar_timestamps(seeded: DatabaseManager) -> None:
    prices = load_price_frame(seeded, ["WBTC"])["WBTC"].rename("WBTC")
    result = run_strategy("bollinger", prices)

    rows = signals_to_rows(result, generated_at=NOW)

    assert rows[0]["period_start_unix"] == BASE
    assert rows[-1]["period_start_unix"] == BASE + 119 * HOUR
    assert isinstance(rows[0]["timestamp"], datetime)


def test_stored_signals_can_be_read_back_into_a_frame(seeded: DatabaseManager) -> None:
    """A backtest must be reconstructable from the table alone."""
    prices = load_price_frame(seeded, ["WBTC"])["WBTC"].rename("WBTC")
    store_signals(seeded, run_strategy("bollinger", prices), generated_at=NOW)

    rows = seeded.fetch_all(
        seeded.sql.get_signals, {"strategy": "bollinger", "symbol": "WBTC", "start_unix": None}
    )
    frame = pd.DataFrame(rows).set_index("timestamp")

    assert len(frame) == 120
    assert frame["signal"].between(-1, 1).all()
