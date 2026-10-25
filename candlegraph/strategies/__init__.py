"""Trading strategies and the backtester that scores them.

Importing this package registers every built-in strategy, so
``candlegraph.strategies.available()`` is complete after a single import.
"""

from candlegraph.strategies.arima_signal import ArimaSignalConfig, arima_signal
from candlegraph.strategies.backtest import (
    BacktestConfig,
    BacktestResult,
    backtest,
    compare,
    format_backtest,
)
from candlegraph.strategies.base import (
    Signal,
    StrategyResult,
    available,
    get,
    run_strategy,
)
from candlegraph.strategies.bollinger import BollingerConfig, bollinger_mean_reversion
from candlegraph.strategies.crossover import CrossoverConfig, moving_average_crossover
from candlegraph.strategies.store import signals_to_rows, store_signals

__all__ = [
    "ArimaSignalConfig",
    "BacktestConfig",
    "BacktestResult",
    "BollingerConfig",
    "CrossoverConfig",
    "Signal",
    "StrategyResult",
    "arima_signal",
    "available",
    "backtest",
    "bollinger_mean_reversion",
    "compare",
    "format_backtest",
    "get",
    "moving_average_crossover",
    "run_strategy",
    "signals_to_rows",
    "store_signals",
]
