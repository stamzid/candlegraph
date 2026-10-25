"""The shared vocabulary every strategy speaks.

A strategy is a pure function from a price series plus a config to a frame of
**target positions**, one per bar. It never looks at the clock, never reads
the database, and never peeks ahead: the position on bar *t* must be
computable from information available at the close of bar *t*. Turning that
position into a return is the backtester's job, and it is the backtester that
applies the one-bar execution lag.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import IntEnum
from typing import Any, Protocol

import pandas as pd


class Signal(IntEnum):
    """A target position. The integer value *is* the position size."""

    SHORT = -1
    FLAT = 0
    LONG = 1


# Columns every strategy result carries. `signal` is the position; `score` is
# the continuous quantity behind it (z-score, forecast return, ...) and exists
# so the UI can show *why* and the signal table can store a strength.
REQUIRED_COLUMNS = ("signal", "score")


@dataclass
class StrategyResult:
    """What a strategy produced, and under what configuration."""

    name: str
    symbol: str
    frame: pd.DataFrame
    params: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        missing = [c for c in REQUIRED_COLUMNS if c not in self.frame.columns]
        if missing:
            raise ValueError(f"strategy {self.name!r} produced no {missing} column")

    @property
    def signals(self) -> pd.Series:
        """The target position per bar, as integers."""
        return self.frame["signal"].astype("int64")

    @property
    def latest(self) -> Signal:
        """The most recent target position, or FLAT when there are no bars."""
        values = self.signals.dropna()
        return Signal(int(values.iloc[-1])) if len(values) else Signal.FLAT

    def trades(self) -> int:
        """How many times the position changed."""
        return int((self.signals.diff().fillna(0) != 0).sum())

    def exposure(self) -> float:
        """Fraction of bars holding a non-zero position."""
        if self.signals.empty:
            return 0.0
        return float((self.signals != 0).mean())


class Strategy(Protocol):
    """Callable signature every registered strategy satisfies."""

    def __call__(self, prices: pd.Series, config: Any | None = ...) -> StrategyResult: ...


@dataclass(frozen=True)
class Registration:
    """A strategy plus the config type the CLI and UI should build for it."""

    name: str
    run: Strategy
    config_type: type
    description: str


_REGISTRY: dict[str, Registration] = {}


def register(name: str, config_type: type, description: str) -> Callable[[Strategy], Strategy]:
    """Decorator putting a strategy in the registry under ``name``."""

    def decorate(run: Strategy) -> Strategy:
        if name in _REGISTRY:
            raise ValueError(f"strategy {name!r} is already registered")
        _REGISTRY[name] = Registration(name, run, config_type, description)
        return run

    return decorate


def available() -> tuple[str, ...]:
    """Registered strategy names, sorted."""
    return tuple(sorted(_REGISTRY))


def get(name: str) -> Registration:
    """Look up a strategy, with a message that lists the alternatives."""
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown strategy {name!r}; available: {', '.join(available())}") from None


def run_strategy(name: str, prices: pd.Series, config: Any | None = None) -> StrategyResult:
    """Run a registered strategy by name."""
    return get(name).run(prices, config)


def params_of(config: Any) -> dict[str, Any]:
    """A JSON-safe dict of a config dataclass, for storing alongside signals."""
    if config is None:
        return {}
    if is_dataclass(config) and not isinstance(config, type):
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in asdict(config).items()}
    return dict(config) if isinstance(config, dict) else {}
