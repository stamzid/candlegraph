"""The analytics CLI commands: dispatch, flag plumbing, and exit codes."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from candlegraph.analytics.arima import ArimaConfig
from candlegraph.cli import build_strategy_config, main, parse_order
from candlegraph.storage import set_db
from candlegraph.strategies.arima_signal import ArimaSignalConfig
from candlegraph.strategies.bollinger import BollingerConfig
from candlegraph.strategies.crossover import CrossoverConfig
from tests.conftest import FakeDatabase, make_settings

HOUR = 3600
BASE = 1_700_000_000 // (24 * HOUR) * (24 * HOUR)


def price_rows(symbol: str, n: int, seed: int = 11) -> list[dict[str, Any]]:
    """A deterministic random walk, shaped like rows from the price query."""
    rng = np.random.default_rng(seed)
    walk = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.01, n)))
    return [
        {
            "symbol": symbol,
            "token_id": f"0x{symbol.lower()}",
            "period_start_unix": BASE + i * HOUR,
            "timestamp": datetime.fromtimestamp(BASE + i * HOUR, tz=UTC),
            "open": Decimal(str(price)),
            "high": Decimal(str(price)),
            "low": Decimal(str(price)),
            "close": Decimal(str(price)),
            "price_usd": Decimal(str(price)),
            "volume_usd": Decimal("1000"),
            "total_value_locked_usd": Decimal("5000"),
        }
        for i, price in enumerate(walk)
    ]


@pytest.fixture
def cli(tokens_file: Path, monkeypatch: pytest.MonkeyPatch) -> FakeDatabase:
    """A CLI wired to a fake database holding WETH and WBTC history."""
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    monkeypatch.setattr(
        "candlegraph.cli.get_settings",
        lambda: make_settings(tokens_file=str(tokens_file), analytics_lookback_days=30),
    )
    db = FakeDatabase(get_price_history=price_rows("WBTC", 400) + price_rows("SHIB", 400, seed=12))
    set_db(db)
    return db


@pytest.fixture
def empty_cli(tokens_file: Path, monkeypatch: pytest.MonkeyPatch) -> FakeDatabase:
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    monkeypatch.setattr("candlegraph.cli.get_settings", lambda: make_settings(tokens_file=str(tokens_file)))
    db = FakeDatabase()
    set_db(db)
    return db


# --- helpers ---------------------------------------------------------------


def test_an_order_string_parses_into_three_integers() -> None:
    assert parse_order("2,1,3") == (2, 1, 3)
    assert parse_order(" 1 , 0 , 1 ") == (1, 0, 1)


@pytest.mark.parametrize("text", ["1,1", "a,b,c", "1,1,1,1"])
def test_a_malformed_order_exits_with_a_message(text: str) -> None:
    with pytest.raises(SystemExit, match="order"):
        parse_order(text)


# --- config building -------------------------------------------------------


def namespace(**kwargs: Any) -> Any:
    import argparse

    base = dict.fromkeys(
        ("window", "entry_z", "exit_z", "fast", "slow", "order", "threshold", "refit_every", "allow_short")
    )
    return argparse.Namespace(**{**base, **kwargs})


def test_bollinger_flags_build_a_bollinger_config() -> None:
    config = build_strategy_config("bollinger", namespace(window=30, entry_z=2.5, exit_z=0.4))

    assert isinstance(config, BollingerConfig)
    assert (config.window, config.entry_z, config.exit_z) == (30, 2.5, 0.4)


def test_unset_flags_leave_the_strategy_defaults_alone() -> None:
    """Each strategy keeps its own defaults rather than inheriting another's."""
    config = build_strategy_config("bollinger", namespace())

    assert isinstance(config, BollingerConfig)
    assert config.window == BollingerConfig().window


@pytest.mark.parametrize(
    ("name", "expected"),
    [("bollinger", True), ("crossover", False), ("arima", True)],
)
def test_an_unspecified_short_flag_keeps_each_strategys_own_default(name: str, expected: bool) -> None:
    """`store_true` would make "unset" look like False and silently disable
    shorting for the two strategies that default to allowing it."""
    config = build_strategy_config(name, namespace())

    assert config is not None
    assert config.allow_short is expected  # type: ignore[attr-defined]


@pytest.mark.parametrize("name", ["bollinger", "crossover", "arima"])
@pytest.mark.parametrize("choice", [True, False])
def test_an_explicit_short_flag_overrides_the_default(name: str, choice: bool) -> None:
    config = build_strategy_config(name, namespace(allow_short=choice))

    assert config is not None
    assert config.allow_short is choice  # type: ignore[attr-defined]


def test_the_cli_exposes_both_halves_of_the_short_flag() -> None:
    from candlegraph.cli import _parser

    assert _parser().parse_args(["signals", "WBTC"]).allow_short is None
    assert _parser().parse_args(["signals", "WBTC", "--allow-short"]).allow_short is True
    assert _parser().parse_args(["signals", "WBTC", "--no-allow-short"]).allow_short is False


def test_crossover_flags_build_a_crossover_config() -> None:
    config = build_strategy_config("crossover", namespace(fast=10, slow=40, allow_short=True))

    assert isinstance(config, CrossoverConfig)
    assert (config.fast, config.slow, config.allow_short) == (10, 40, True)


def test_arima_flags_build_an_arima_config() -> None:
    config = build_strategy_config("arima", namespace(order="2,1,0", threshold=0.01, refit_every=12))

    assert isinstance(config, ArimaSignalConfig)
    assert config.arima.order == (2, 1, 0)
    assert config.threshold == 0.01
    assert config.refit_every == 12


def test_an_unknown_strategy_name_builds_no_config() -> None:
    assert build_strategy_config("nope", namespace()) is None


# --- strategies ------------------------------------------------------------


def test_strategies_lists_each_one_with_a_description(
    empty_cli: FakeDatabase, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["strategies"]) == 0

    out = capsys.readouterr().out
    assert "bollinger" in out
    assert "Mean reversion" in out
    assert "arima" in out


# --- stats -----------------------------------------------------------------


def test_stats_reports_rolling_mean_and_volatility(
    cli: FakeDatabase, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["stats", "WBTC", "--window", "24"]) == 0

    out = capsys.readouterr().out
    assert "rolling mean" in out
    assert "volatility (annual)" in out
    assert "400 bars" in out


def test_stats_on_an_unknown_token_exits_with_the_tracked_list(cli: FakeDatabase) -> None:
    with pytest.raises(SystemExit, match="unknown token"):
        main(["stats", "DOGE"])


def test_stats_without_history_exits_non_zero(empty_cli: FakeDatabase) -> None:
    with pytest.raises(SystemExit, match="no stored price history"):
        main(["stats", "WBTC"])


def test_a_window_longer_than_the_history_reports_rather_than_crashes(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    monkeypatch.setattr("candlegraph.cli.get_settings", lambda: make_settings(tokens_file=str(tokens_file)))
    set_db(FakeDatabase(get_price_history=price_rows("WBTC", 10)))

    assert main(["stats", "WBTC", "--window", "50"]) == 1
    assert "not enough history" in capsys.readouterr().out


# --- signals ---------------------------------------------------------------


def test_signals_prints_a_summary_and_a_tail(cli: FakeDatabase, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["signals", "WBTC", "--strategy", "bollinger", "--tail", "3"]) == 0

    out = capsys.readouterr().out
    assert "bollinger on WBTC" in out
    assert "trades" in out
    assert "signal" in out


def test_signals_does_not_write_unless_asked(cli: FakeDatabase) -> None:
    main(["signals", "WBTC"])

    assert cli.upserts == []


def test_store_writes_through_the_signal_statement(
    cli: FakeDatabase, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["signals", "WBTC", "--store"]) == 0

    [(label, rows)] = cli.upserts
    assert label == "insert_signal"
    assert len(rows) == 400
    assert "stored 400 rows" in capsys.readouterr().out


def test_signal_flags_reach_the_strategy(cli: FakeDatabase, capsys: pytest.CaptureFixture[str]) -> None:
    main(["signals", "WBTC", "--strategy", "bollinger", "--entry-z", "0.5", "--exit-z", "0.1"])
    eager = capsys.readouterr().out

    main(["signals", "WBTC", "--strategy", "bollinger", "--entry-z", "3.5", "--exit-z", "0.1"])
    patient = capsys.readouterr().out

    assert eager != patient


def test_an_unknown_strategy_is_an_error(cli: FakeDatabase) -> None:
    with pytest.raises(KeyError, match="unknown strategy"):
        main(["signals", "WBTC", "--strategy", "nope"])


# --- backtest --------------------------------------------------------------


def test_backtest_prints_a_report(cli: FakeDatabase, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["backtest", "WBTC", "--strategy", "bollinger"]) == 0

    out = capsys.readouterr().out
    assert "Backtest: bollinger on WBTC" in out
    assert "sharpe" in out
    assert "max drawdown" in out


def test_backtest_all_compares_every_strategy(cli: FakeDatabase, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["backtest", "WBTC", "--strategy", "all"]) == 0

    out = capsys.readouterr().out
    for name in ("arima", "bollinger", "crossover"):
        assert name in out


def test_cost_flags_change_the_result(cli: FakeDatabase, capsys: pytest.CaptureFixture[str]) -> None:
    main(["backtest", "WBTC", "--fee-bps", "0", "--slippage-bps", "0"])
    free = capsys.readouterr().out

    main(["backtest", "WBTC", "--fee-bps", "200", "--slippage-bps", "50"])
    pricey = capsys.readouterr().out

    assert free != pricey
    assert "200bps" in pricey


def test_the_backtest_is_reproducible(cli: FakeDatabase, capsys: pytest.CaptureFixture[str]) -> None:
    """The phase-3 exit criterion: same data and flags, same report."""
    main(["backtest", "WBTC", "--strategy", "bollinger"])
    first = capsys.readouterr().out

    main(["backtest", "WBTC", "--strategy", "bollinger"])
    second = capsys.readouterr().out

    assert first == second


# --- portfolio -------------------------------------------------------------


def test_portfolio_allocates_across_the_tracked_tokens(
    cli: FakeDatabase, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["portfolio"]) == 0

    out = capsys.readouterr().out
    assert "WBTC" in out
    assert "SHIB" in out
    assert "sharpe" in out


def test_the_risk_free_rate_flag_is_reported(cli: FakeDatabase, capsys: pytest.CaptureFixture[str]) -> None:
    main(["portfolio", "--risk-free-rate", "0.04"])

    assert "risk-free: 4.00%" in capsys.readouterr().out


def test_the_objective_flag_is_reported(cli: FakeDatabase, capsys: pytest.CaptureFixture[str]) -> None:
    main(["portfolio", "--objective", "risk_parity"])

    assert "Objective: risk_parity" in capsys.readouterr().out


def test_a_symbol_subset_is_honoured(cli: FakeDatabase, capsys: pytest.CaptureFixture[str]) -> None:
    main(["portfolio", "--symbols", "WBTC"])

    params = next(p for label, p in cli.fetches if label == "get_price_history")
    assert params["symbols"] == ["WBTC"]
    assert "WBTC" in capsys.readouterr().out


def test_the_frontier_can_be_printed(cli: FakeDatabase, capsys: pytest.CaptureFixture[str]) -> None:
    main(["portfolio", "--frontier", "5"])

    out = capsys.readouterr().out
    assert "volatility" in out
    assert out.count("%") > 5


def test_an_infeasible_weight_cap_exits_with_a_message(cli: FakeDatabase) -> None:
    with pytest.raises(SystemExit, match="cannot sum to 1"):
        main(["portfolio", "--max-weight", "0.1"])


def test_portfolio_without_history_exits(empty_cli: FakeDatabase) -> None:
    with pytest.raises(SystemExit, match="no stored price history"):
        main(["portfolio"])


# --- forecast --------------------------------------------------------------


def test_forecast_prints_a_price_and_an_interval(
    cli: FakeDatabase, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["forecast", "WBTC"]) == 0

    out = capsys.readouterr().out
    assert "ARIMA(1, 1, 1)" in out
    assert "expected return" in out
    assert "interval" in out


def test_a_custom_order_is_used(cli: FakeDatabase, capsys: pytest.CaptureFixture[str]) -> None:
    main(["forecast", "WBTC", "--order", "2,1,0"])

    assert "ARIMA(2, 1, 0)" in capsys.readouterr().out


def test_auto_order_may_pick_a_different_order(cli: FakeDatabase, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["forecast", "WBTC", "--auto-order", "--order", "3,1,3"]) == 0

    assert "ARIMA" in capsys.readouterr().out


def test_a_multi_step_forecast_is_labelled(cli: FakeDatabase, capsys: pytest.CaptureFixture[str]) -> None:
    main(["forecast", "WBTC", "--steps", "6"])

    assert "+6h" in capsys.readouterr().out


def test_a_forecast_with_too_little_history_exits_non_zero(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    monkeypatch.setattr("candlegraph.cli.get_settings", lambda: make_settings(tokens_file=str(tokens_file)))
    set_db(FakeDatabase(get_price_history=price_rows("WBTC", 20)))

    assert main(["forecast", "WBTC"]) == 1
    assert "no forecast" in capsys.readouterr().out


def test_the_default_arima_config_is_the_documented_one() -> None:
    assert ArimaConfig().order == (1, 1, 1)


# --- risk --------------------------------------------------------------------


def test_risk_prints_tail_risk_and_the_benchmark_split(
    cli: FakeDatabase, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["risk", "SHIB", "--benchmark", "WBTC"]) == 0

    out = capsys.readouterr().out
    assert "VaR 95% 1d" in out
    assert "CVaR 95% 1d" in out
    assert "versus WBTC" in out
    assert "systematic share" in out
    assert "risk-free rate:" in out


def test_risk_without_a_benchmark_omits_the_split(
    cli: FakeDatabase, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["risk", "SHIB", "--no-benchmark"]) == 0

    out = capsys.readouterr().out
    assert "VaR" in out
    assert "versus" not in out


def test_risk_against_itself_skips_the_split_and_says_so(
    cli: FakeDatabase, capsys: pytest.CaptureFixture[str]
) -> None:
    """The default benchmark (WETH) is not in this fixture's registry; the report still prints."""
    assert main(["risk", "WBTC"]) == 0

    out = capsys.readouterr().out
    assert "versus" not in out
    assert "no benchmark split" in out


def test_risk_flags_reach_the_report(cli: FakeDatabase, capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(
            [
                "risk",
                "SHIB",
                "--no-benchmark",
                "--method",
                "parametric",
                "--confidence",
                "0.99",
                "--horizon",
                "1",
            ]
        )
        == 0
    )

    out = capsys.readouterr().out
    assert "parametric" in out
    assert "VaR 99% 1h" in out


def test_risk_reports_the_real_rate_from_the_configured_inflation(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    monkeypatch.setattr(
        "candlegraph.cli.get_settings",
        lambda: make_settings(
            tokens_file=str(tokens_file), analytics_lookback_days=30, risk_free_rate=0.04, inflation_rate=0.05
        ),
    )
    set_db(FakeDatabase(get_price_history=price_rows("WBTC", 400)))

    assert main(["risk", "WBTC", "--no-benchmark"]) == 0

    out = capsys.readouterr().out
    assert "4.00% nominal" in out
    assert "-0.95% real" in out


def test_risk_on_an_unknown_token_exits_with_the_tracked_list(cli: FakeDatabase) -> None:
    with pytest.raises(SystemExit, match="tracked: WBTC, SHIB"):
        main(["risk", "DOGE"])


def test_risk_without_history_exits(empty_cli: FakeDatabase) -> None:
    with pytest.raises(SystemExit, match="no stored price history"):
        main(["risk", "WBTC", "--no-benchmark"])


def test_an_unknown_rate_source_exits_with_a_message(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    monkeypatch.setattr(
        "candlegraph.cli.get_settings",
        lambda: make_settings(tokens_file=str(tokens_file), risk_free_rate_source="tavily"),
    )
    set_db(FakeDatabase(get_price_history=price_rows("WBTC", 400)))

    with pytest.raises(SystemExit, match="RISK_FREE_RATE_SOURCE"):
        main(["risk", "WBTC", "--no-benchmark"])


def test_portfolio_reports_the_allocations_tail_risk(
    cli: FakeDatabase, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["portfolio", "--objective", "equal_weight"]) == 0

    out = capsys.readouterr().out
    assert "VaR 95% 1d" in out
    assert "CVaR 95% 1d" in out
