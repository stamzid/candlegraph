"""The observer's state and thresholds.

Everything a node reads or writes is declared here. Fields a node accumulates
(``findings``, ``errors``) carry an ``operator.add`` reducer so each node
returns only its own additions; everything else is set by exactly one node.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, TypedDict

from candlegraph.agents.base import AgentReport, Finding
from candlegraph.ingestion.quality import QualityReport
from candlegraph.settings import Settings


@dataclass(frozen=True)
class ObserverThresholds:
    """Every knob the assessment uses, in one place and out of ``Settings``.

    Built from settings in production and by hand in tests, so an assessment
    test states its thresholds explicitly instead of inheriting defaults.
    """

    poll_interval: int = 300
    stale_intervals: int = 3
    failure_streak: int = 3
    baseline_days: int = 7
    drift_window_hours: int = 24
    min_bars: int = 12
    volume_drift_ratio: float = 4.0
    volatility_drift_ratio: float = 3.0
    run_drift_ratio: float = 3.0
    suspicious_gap_hours: int = 6
    liquid_volume_usd: float = 100_000.0
    min_expected_coverage: float = 0.8

    @classmethod
    def from_settings(cls, settings: Settings) -> ObserverThresholds:
        return cls(
            poll_interval=settings.data_poll_interval,
            stale_intervals=settings.observer_stale_intervals,
            failure_streak=settings.observer_failure_streak,
            baseline_days=settings.observer_baseline_days,
            drift_window_hours=settings.observer_drift_window_hours,
            min_bars=settings.observer_min_bars,
            volume_drift_ratio=settings.observer_volume_drift_ratio,
            volatility_drift_ratio=settings.observer_volatility_drift_ratio,
            run_drift_ratio=settings.observer_run_drift_ratio,
            suspicious_gap_hours=settings.observer_suspicious_gap_hours,
            liquid_volume_usd=settings.observer_liquid_volume_usd,
            min_expected_coverage=settings.min_expected_coverage,
        )

    @property
    def stale_after_seconds(self) -> float:
        return float(self.poll_interval * self.stale_intervals)

    @property
    def window_seconds(self) -> int:
        return self.drift_window_hours * 3600

    @property
    def baseline_seconds(self) -> int:
        return self.baseline_days * 86_400


@dataclass(frozen=True)
class SymbolMetrics:
    """One token's recent window against its baseline window.

    ``None`` means the statistic could not be computed (too few rows), which
    is different from a value of zero and is never compared against a
    threshold.
    """

    symbol: str
    recent_bars: int
    baseline_bars: int
    recent_volume_median: float | None
    baseline_volume_median: float | None
    baseline_volume_mean: float | None
    recent_volatility: float | None
    baseline_volatility: float | None

    @property
    def is_liquid_above(self) -> float:
        return self.baseline_volume_mean or 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "recent_bars": self.recent_bars,
            "baseline_bars": self.baseline_bars,
            "recent_volume_median": self.recent_volume_median,
            "baseline_volume_median": self.baseline_volume_median,
            "baseline_volume_mean": self.baseline_volume_mean,
            "recent_volatility": self.recent_volatility,
            "baseline_volatility": self.baseline_volatility,
        }


@dataclass(frozen=True)
class RunMetrics:
    """Ingest run duration and output, recent window against baseline."""

    recent_runs: int
    baseline_runs: int
    recent_duration_median: float | None
    baseline_duration_median: float | None
    recent_rows_median: float | None
    baseline_rows_median: float | None


class ObserverState(TypedDict, total=False):
    now: datetime
    thresholds: ObserverThresholds
    expected_symbols: tuple[str, ...]
    database_up: bool
    runs: list[dict[str, Any]]
    seconds_since_success: float | None
    quality: QualityReport | None
    metrics: dict[str, SymbolMetrics]
    run_metrics: RunMetrics | None
    findings: Annotated[list[Finding], operator.add]
    errors: Annotated[list[str], operator.add]
    report: AgentReport
