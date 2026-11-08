"""The derived-fill cache: estimates for hours that were never ingested.

``quality.py`` says which hours are missing; this says what a display
surface may draw in their place, and stores the answer so a dashboard does
not re-interpolate on every widget change.

It lives beside ``quality.py`` rather than in ``analytics/`` on purpose.
Invariant 18 keeps the analytics package pure — only ``analytics/prices.py``
touches the database, and only to read — so the arithmetic stays there
(:func:`analytics.prices.apply_fill`) and the I/O lives here.

Three rules this module obeys, all of them load-bearing:

- **It never writes to ``token_hours_data``.** ``docs/roadmap/DESIGN.md`` §0
  rejects filled rows in the market table because coverage would then read
  100% by construction and ``NULL`` would stop meaning "not collected"
  (invariant 11). Only hours with no stored row land here, in their own
  table.
- **A superseded estimate is deleted, not kept.** Once ingestion supplies a
  real row for an hour, the cached fill for it is wrong. Invariant 4 is
  about observations; this is a cache.
- **Writes raise on failure** (invariant 3). A silent partial write would
  leave a chart showing half a line.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd

from candlegraph.analytics.prices import (
    DEFAULT_MAX_FILL,
    FillPolicy,
    apply_fill,
    fill_rule,
    load_price_history,
    to_wide,
)
from candlegraph.ingestion.quality import HOUR, group_missing_hours
from candlegraph.storage import DatabaseManager

# The three estimators this cache can hold. `method` is part of the natural
# key, so all three may hold an estimate for the *same* missing hour — which
# is what makes them comparable. Only one is ever *selected* for display or
# for a read; they are alternatives, not layers.
LINEAR = "linear"
CAUSAL = "causal"
BRIDGE = "bridge"

# Which fill policy each stored method corresponds to, so a reader of the
# cache can ask `fill_rule(...)` whether it is safe to trade on.
METHOD_POLICY: dict[str, FillPolicy] = {
    LINEAR: "interpolate",
    CAUSAL: "causal",
    BRIDGE: "bridge",
}
# Presentation order: cheapest and least honest first, best last.
METHODS: tuple[str, ...] = (LINEAR, CAUSAL, BRIDGE)

METHOD_SUMMARY: dict[str, str] = {
    LINEAR: "straight line across the gap; no error bar, and look-ahead",
    CAUSAL: "Kalman local-level forecast; past data only, band widens as sqrt(hours)",
    BRIDGE: "Brownian bridge pinned to both ends; most accurate, look-ahead",
}


@dataclass(frozen=True)
class FillRun:
    """What one refresh did, per symbol."""

    symbol: str
    filled_hours: int
    gaps: int
    longest_gap_hours: int
    skipped_hours: int = 0
    """Hours left alone because their run was longer than the cap."""


@dataclass
class FillSummary:
    """The result of a refresh across every requested symbol."""

    method: str
    runs: list[FillRun] = field(default_factory=list)
    rows_written: int = 0
    superseded_removed: int = 0

    @property
    def filled_hours(self) -> int:
        return sum(run.filled_hours for run in self.runs)

    @property
    def symbols(self) -> list[str]:
        return [run.symbol for run in self.runs]


def method_is_causal(method: str) -> bool:
    """Whether a stored method may be fed to something that trades.

    ``linear`` is not, and the read path keeps it away from strategies by
    never being called from :func:`analytics.prices.load_symbol_series`.
    An unknown method is treated as unsafe rather than assumed fine.
    """
    policy = METHOD_POLICY.get(method)
    return fill_rule(policy).causal if policy is not None else False


def _finite(value: Any) -> float | None:
    """A float the database can store, or ``None`` for anything that is not."""
    if value is None or pd.isna(value):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def fill_rows(
    symbol: str,
    token_id: str,
    observed: pd.Series,
    synthetic: pd.Series,
    method: str,
    computed_at: datetime,
    sigma: pd.Series | None = None,
) -> list[dict[str, Any]]:
    """Synthetic cells of one symbol's series as rows ready to upsert. Pure.

    ``gap_hours`` is the length of the whole run each filled hour belongs to,
    not the position within it, so a reader can tell a one-hour fill from a
    twenty-hour guess whatever produced it.

    ``sigma_log`` is the estimator's own standard deviation in log space, and
    stays ``NULL`` for the estimators that have none. "No error bar" is not
    "an error bar of zero" — invariant 11's rule, applied to an estimate.
    """
    marked = pd.DatetimeIndex(synthetic.index[synthetic.to_numpy()])
    if marked.empty:
        return []

    unix = [int(ts.timestamp()) for ts in marked]
    span = {}
    for gap in group_missing_hours(unix):
        for moment in range(gap.start_unix, gap.end_unix + HOUR, HOUR):
            span[moment] = gap.hours

    rows = []
    for timestamp, moment in zip(marked, unix, strict=True):
        value = observed.get(timestamp)
        rows.append(
            {
                "token_id": token_id,
                "symbol": symbol,
                "period_start_unix": moment,
                "method": method,
                "price_usd": None if value is None or pd.isna(value) else float(value),
                "close": None,
                "gap_hours": span[moment],
                "sigma_log": _finite(sigma.get(timestamp)) if sigma is not None else None,
                "computed_at": computed_at,
            }
        )
    return rows


def refresh_fill_cache(
    db: DatabaseManager,
    symbols: list[str],
    method: str = LINEAR,
    days: int | None = None,
    max_fill: int | None = DEFAULT_MAX_FILL,
    now: datetime | None = None,
) -> FillSummary:
    """Recompute and store the fills for ``symbols``, replacing what was there.

    The method's rows are dropped for the chosen symbols and rebuilt, because
    a partial refresh cannot tell a row that is still correct from one whose
    gap has since been filled by real data. Rebuilding a bounded table is
    cheaper than reasoning about that.

    Args:
        db: Where to read the observations and write the estimates.
        symbols: Which tokens to refresh. Empty means "nothing to do".
        method: The estimator label; must be one this module knows.
        days: Trailing window to consider. ``None`` is all stored history.
        max_fill: Longest run of hours that may be filled. Beyond it the
            hole stays, whole.
        now: Stamp for ``computed_at``, for deterministic tests.

    Returns:
        A :class:`FillSummary` naming what was written per symbol.

    Raises:
        PriceError: ``method`` is not a known estimator.
    """
    policy = METHOD_POLICY.get(method)
    if policy is None:
        raise ValueError(f"unknown fill method {method!r}; known: {', '.join(sorted(METHOD_POLICY))}")

    summary = FillSummary(method=method)
    if not symbols:
        return summary

    moment = (now or datetime.now(tz=UTC)).astimezone(UTC)
    from candlegraph.analytics.prices import window_bounds

    start_unix, end_unix = window_bounds(days, now=moment)

    # Drop first: a stale estimate for an hour that has since been ingested
    # would otherwise survive this refresh untouched.
    db.execute(db.sql.delete_fills_for_method, {"method": method, "symbols": list(symbols)})

    history = load_price_history(db, list(symbols), start_unix, end_unix)
    if history.empty:
        return summary

    observed = to_wide(history, fill="none")
    result = apply_fill(observed, fill=policy, max_fill=max_fill)

    token_ids = dict(zip(history.frame["symbol"], history.frame["token_id"], strict=True))

    records: list[dict[str, Any]] = []
    for symbol in result.frame.columns:
        token_id = token_ids.get(symbol)
        if token_id is None:
            continue
        rows = fill_rows(
            symbol,
            token_id,
            result.frame[symbol],
            result.synthetic[symbol],
            method,
            moment,
            sigma=result.sigma[symbol] if symbol in result.sigma.columns else None,
        )
        holes = observed[symbol].isna()
        gaps = group_missing_hours([int(ts.timestamp()) for ts in holes.index[holes.to_numpy()]])
        summary.runs.append(
            FillRun(
                symbol=symbol,
                filled_hours=len(rows),
                gaps=len(gaps),
                longest_gap_hours=max((gap.hours for gap in gaps), default=0),
                skipped_hours=int(holes.sum()) - len(rows),
            )
        )
        records.extend(rows)

    if records:
        summary.rows_written = db.upsert_many(
            db.sql.insert_token_hours_fill, db.sql.insert_token_hours_fill_template, records
        )

    # Belt and braces: anything this run produced for an hour that does have
    # a real row goes immediately, so the cache can never shadow an
    # observation even if the window above missed it.
    summary.superseded_removed = db.execute(db.sql.delete_superseded_fills, None)
    return summary


@dataclass(frozen=True)
class MethodScore:
    """How one estimator did on hours whose real price we actually know."""

    method: str
    hours: int
    mae: float
    """Mean absolute error as a fraction of the true price."""
    rmse: float
    """Root mean squared error, same units. Punishes the occasional howler."""
    worst: float
    """Largest single relative error."""
    causal: bool

    @property
    def mae_bps(self) -> float:
        return self.mae * 10_000


def score_methods(
    prices: pd.Series,
    gap_hours: int = 4,
    stride: int = 23,
    max_fill: int | None = DEFAULT_MAX_FILL,
) -> list[MethodScore]:
    """Hold out observed hours, re-estimate them, and measure the error. Pure.

    "Which estimator is best" is otherwise a matter of taste. This answers it
    the only way that is falsifiable: punch synthetic holes of ``gap_hours``
    into a series we have the real prices for, run each estimator over the
    damaged copy, and compare what came back with what was really there.

    ``stride`` spaces the holes out so they never touch, and is deliberately
    coprime-ish with the day so the sample is not all the same hour of the
    clock. Errors are relative, so tokens priced at 3 and at 30,000 are
    comparable.

    The result will usually rank ``bridge`` best, and that is not a reason to
    feed it to a strategy: it wins *because* it reads the bar after the gap.
    ``causal`` is the one to compare against the others honestly, being the
    only one that could have been computed at the time.
    """
    from candlegraph.analytics.prices import apply_fill

    clean = prices.dropna().astype("float64")
    if len(clean) < gap_hours * 3 or gap_hours < 1:
        return []

    # Hole starts: leave a bar at each end so every gap has both endpoints.
    starts = list(range(1, len(clean) - gap_hours - 1, max(stride, gap_hours + 1)))
    if not starts:
        return []

    damaged = clean.copy()
    held_out: list[int] = []
    for start in starts:
        held_out.extend(range(start, start + gap_hours))
    positions = np.array(held_out, dtype="int64")
    damaged.iloc[positions] = float("nan")

    truth = clean.to_numpy(dtype="float64")[positions]
    frame = damaged.to_frame(name="series")

    scores = []
    for method in METHODS:
        estimated = apply_fill(frame, fill=METHOD_POLICY[method], max_fill=max_fill)
        guess = estimated.frame["series"].to_numpy(dtype="float64")[positions]
        usable = np.isfinite(guess) & np.isfinite(truth) & (truth > 0)
        if not usable.any():
            continue
        error = np.abs(guess[usable] - truth[usable]) / truth[usable]
        scores.append(
            MethodScore(
                method=method,
                hours=int(usable.sum()),
                mae=float(np.mean(error)),
                rmse=float(np.sqrt(np.mean(error**2))),
                worst=float(np.max(error)),
                causal=method_is_causal(method),
            )
        )
    return sorted(scores, key=lambda score: score.mae)


def load_fills(
    db: DatabaseManager,
    symbols: list[str],
    method: str = LINEAR,
    start_unix: int | None = None,
    end_unix: int | None = None,
) -> pd.DataFrame:
    """Stored fills for ``symbols``, newest-agnostic, indexed by timestamp.

    Read-only and cheap: this is what a dashboard calls instead of
    interpolating again. An empty frame means the cache has nothing for the
    window, which is not an error — it is what an un-refreshed cache looks
    like.
    """
    columns = [
        "symbol",
        "timestamp",
        "period_start_unix",
        "price_usd",
        "sigma_log",
        "gap_hours",
        "method",
    ]
    if not symbols:
        return pd.DataFrame(columns=columns)

    rows = db.fetch_all(
        db.sql.get_token_hours_fill,
        {
            "symbols": list(symbols),
            "method": method,
            "start_unix": start_unix,
            "end_unix": end_unix,
        },
    )
    if not rows:
        return pd.DataFrame(columns=columns)

    frame = pd.DataFrame(rows)
    frame["price_usd"] = pd.to_numeric(frame["price_usd"], errors="coerce").astype("float64")
    if "sigma_log" not in frame:
        frame["sigma_log"] = float("nan")
    frame["sigma_log"] = pd.to_numeric(frame["sigma_log"], errors="coerce").astype("float64")
    frame["period_start_unix"] = frame["period_start_unix"].astype("int64")
    frame["timestamp"] = pd.to_datetime(frame["period_start_unix"], unit="s", utc=True)
    return frame[columns]


def fill_cache_summary(db: DatabaseManager) -> pd.DataFrame:
    """One row per (symbol, method): how much is cached and how old it is."""
    columns = [
        "symbol",
        "method",
        "filled_hours",
        "hours_with_band",
        "longest_gap_hours",
        "first_unix",
        "last_unix",
        "computed_at",
    ]
    rows = db.fetch_all(db.sql.fill_cache_summary, None)
    return pd.DataFrame(rows, columns=columns) if rows else pd.DataFrame(columns=columns)
