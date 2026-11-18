"""The five pipeline-step bodies: what they count, and how they grade a run.

Each body is an adapter, so these tests are about the grading — when a step
is ``failed`` rather than ``partial`` decides whether the steps behind it
run, which is the whole reason the pipeline exists. The arithmetic they call
is tested where it lives.

The run-twice proofs for ``Job.idempotent`` are here too, named by
``test_jobs.py::IDEMPOTENCE_PROOFS``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from candlegraph.harness import JOBS, Outcome, run_job
from candlegraph.harness.steps import (
    analyse_job_body,
    explain_job_body,
    validate_job_body,
)
from candlegraph.tokens import Token, TokenRegistry
from tests.conftest import SHIB, WBTC, FakeDatabase, make_settings

HOUR = 3600
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
START = int((NOW - timedelta(hours=60)).timestamp()) // HOUR * HOUR


@pytest.fixture
def registry() -> TokenRegistry:
    return TokenRegistry(
        tokens=(
            Token(address=WBTC, symbol="WBTC", name="Wrapped BTC"),
            Token(address=SHIB, symbol="SHIB", name="SHIBA INU"),
        )
    )


def price_rows(symbol: str, hours: int = 60, skip: set[int] = frozenset()) -> list[dict[str, Any]]:
    """A clean hourly series, optionally with some hours never emitted.

    ``skip`` is how Uniswap behaves for an hour with no swaps: the row is
    absent, not zero (invariant 19).
    """
    rows = []
    for index in range(hours):
        if index in skip:
            continue
        unix = START + index * HOUR
        price = 100.0 + index
        rows.append(
            {
                "symbol": symbol,
                "period_start_unix": unix,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "price_usd": price,
                "volume_usd": 1000.0,
                "total_value_locked_usd": 10_000.0,
            }
        )
    return rows


def coverage_rows(stored: int, expected: int = 60) -> list[dict[str, Any]]:
    """What ``coverage_report`` returns — a different query from the price read."""
    return [
        {
            "token_id": WBTC,
            "symbol": "WBTC",
            "stored_hours": stored,
            "expected_hours": expected,
            "first_unix": START,
            "last_unix": START + (expected - 1) * HOUR,
        },
        {
            "token_id": SHIB,
            "symbol": "SHIB",
            "stored_hours": expected,
            "expected_hours": expected,
            "first_unix": START,
            "last_unix": START + (expected - 1) * HOUR,
        },
    ]


def seeded(skip: set[int] = frozenset(), hours: int = 60) -> FakeDatabase:
    """A database holding a clean series for SHIB and a gapped one for WBTC."""
    return FakeDatabase(
        get_price_history=price_rows("WBTC", hours=hours, skip=skip) + price_rows("SHIB", hours=hours),
        coverage_report=coverage_rows(stored=hours - len(skip), expected=hours),
        missing_hours=[{"period_start_unix": START + index * HOUR} for index in sorted(skip)],
    )


def run(job: str, db: FakeDatabase, registry: TokenRegistry, **params: Any) -> Any:
    settings = make_settings(agent_report_sinks="log,ledger")
    return run_job(JOBS[job], settings, db=db, params={"registry": registry, "now": NOW, **params})


# --- validate ------------------------------------------------------------------


def test_a_token_with_no_rows_at_all_fails_the_run(registry: TokenRegistry) -> None:
    """Which is what stops the steps behind it: nothing worth computing on."""
    result = run("validate", FakeDatabase(), registry)

    assert result.outcome is Outcome.FAILED
    assert set(result.result.errors) == {"WBTC", "SHIB"}


def test_coverage_below_the_threshold_is_partial_not_failed(registry: TokenRegistry) -> None:
    """A thin token legitimately has missing hours (invariant 19).

    Grading that as a failure would stop the hour's signals every time a
    quiet token had a quiet night, which is the opposite of useful.
    """
    db = seeded(skip=set(range(0, 40)))

    result = run("validate", db, registry, min_coverage=0.99)

    assert result.outcome is Outcome.PARTIAL
    assert "WBTC" in result.result.detail["under_covered"]
    assert result.result.counts["under_covered"] == 1


def test_a_healthy_window_succeeds(registry: TokenRegistry) -> None:
    result = run("validate", seeded(), registry, min_coverage=0.5)

    assert result.outcome is Outcome.SUCCEEDED
    assert result.result.counts["missing_tokens"] == 0
    assert result.result.detail["coverage"]["WBTC"] > 0.5


# --- analyse -------------------------------------------------------------------


def test_analyse_records_what_it_computed_and_stores_nothing(registry: TokenRegistry) -> None:
    db = seeded()

    result = run("analyse", db, registry, window=24)

    assert result.outcome is Outcome.SUCCEEDED
    assert result.result.counts["tokens"] == 2
    assert result.result.detail["by_symbol"]["WBTC"]["volatility"] is not None
    # Its whole write surface is the ledger row the harness opened and closed.
    assert {label for label, _ in db.executed} == {"open_run", "close_run"}
    assert db.upserts == []


def test_one_unreadable_token_makes_the_run_partial_not_failed(registry: TokenRegistry) -> None:
    db = FakeDatabase(get_price_history=price_rows("WBTC"))

    result = run("analyse", db, registry)

    assert result.outcome is Outcome.PARTIAL
    assert "SHIB" in result.result.errors
    assert result.result.counts["tokens"] == 1


def test_no_readable_token_at_all_is_a_failure(registry: TokenRegistry) -> None:
    """Nothing attempted is not half-done; the steps behind it should stop."""
    result = run("analyse", FakeDatabase(), registry)

    assert result.outcome is Outcome.FAILED


def test_a_window_longer_than_the_history_reports_no_figure(registry: TokenRegistry) -> None:
    """Rolling windows are complete or absent (invariant 16) — never a short one."""
    result = run("analyse", seeded(), registry, window=5000)

    assert result.result.detail["by_symbol"]["WBTC"]["volatility"] is None


# --- signal --------------------------------------------------------------------


def test_signal_stores_a_row_per_bar_per_strategy(registry: TokenRegistry) -> None:
    db = seeded()

    result = run("signal", db, registry, strategies=("bollinger",), symbols=("WBTC",))

    assert result.outcome is Outcome.SUCCEEDED
    (label, rows) = db.upserts[0]
    assert label == "insert_signal"
    assert len(rows) == 60
    assert {row["strategy"] for row in rows} == {"bollinger"}
    assert result.result.counts["signals_written"] == 60


def test_the_fill_cache_is_never_read_on_the_way_to_a_signal(registry: TokenRegistry) -> None:
    """The rule the whole fill seam exists for (invariant 19).

    ``token_hours_fill`` holds estimates for hours that were never emitted,
    and `linear` and `bridge` read the bar *after* the gap. A signal must
    never be computed from one — and the guarantee here is stronger than
    filtering: the loader a signal uses does not read that table at all.
    """
    db = seeded(skip={10, 11, 12, 13, 14})

    run("signal", db, registry, strategies=("bollinger",), symbols=("WBTC",))

    read = {label for label, _ in db.fetches}
    assert not any("fill" in label for label in read), read


def test_a_gap_is_carried_forward_and_never_backward(registry: TokenRegistry) -> None:
    """A bar inside a gap holds the last *observed* price, not a later one.

    The default policy is `ffill`, which is causal, and this is the test that
    says so: appending a later bar must not change a signal already made.
    Interpolation would change it, which is why `load_symbol_series` refuses
    the non-causal policies outright rather than downgrading them.
    """
    # The gap runs to the end of the shorter window, so its right endpoint
    # exists only in the longer one. That is what makes this test
    # discriminating: an interpolating policy would read bar 45 and give the
    # two runs different values for bars 42-44, and a different z-score with
    # them. Put the gap in the middle and both policies would agree.
    missing = {42, 43, 44}

    def signals_for(hours: int) -> dict[int, int]:
        db = seeded(skip=missing, hours=hours)
        run("signal", db, registry, strategies=("bollinger",), symbols=("WBTC",))
        (_, rows) = db.upserts[0]
        return {int(row["period_start_unix"]): int(row["signal"]) for row in rows}

    shorter = signals_for(45)  # ends inside the gap: nothing after it exists
    longer = signals_for(60)  # the same gap, with 15 bars of future after it

    # Every bar the shorter run saw is scored identically by the longer one.
    assert shorter == {bar: longer[bar] for bar in shorter}
    # And the gap's own bars are there at all, carrying the last observed
    # price — without this the comparison above could pass vacuously.
    assert {START + index * HOUR for index in missing} <= set(shorter)


def test_the_signal_job_offers_no_way_to_ask_for_a_filled_series() -> None:
    """Defence in depth: the loader cannot be talked into a non-causal fill.

    `load_symbol_series` refuses one anyway, but a `fill` parameter would
    invite someone to try, and the refusal would then be a runtime surprise
    rather than a design that never offered it.
    """
    params = JOBS["signal"].params
    assert params is not None
    assert "fill" not in params.model_fields


def test_store_false_computes_without_writing(registry: TokenRegistry) -> None:
    db = seeded()

    result = run("signal", db, registry, store=False, symbols=("WBTC",))

    assert db.upserts == []
    assert result.result.counts["signals_written"] == 0
    assert result.outcome is Outcome.SUCCEEDED


def test_an_unknown_symbol_in_the_spec_fails_the_step(registry: TokenRegistry) -> None:
    """Symbols are deployment config, so the body is where this is caught."""
    result = run("signal", seeded(), registry, symbols=("NOPE",))

    assert result.outcome is Outcome.FAILED
    assert "unknown token" in str(result.exception)


def test_the_step_records_each_token_s_latest_position(registry: TokenRegistry) -> None:
    result = run("signal", seeded(), registry, strategies=("crossover",), symbols=("WBTC",))

    latest = result.result.detail["by_symbol"]["WBTC"]["latest"]
    assert latest["crossover"] in {"long", "short", "flat"}


# --- explain -------------------------------------------------------------------


def test_explain_never_searches(monkeypatch: pytest.MonkeyPatch, registry: TokenRegistry) -> None:
    """The one step that could spend money, and it cannot (DESIGN §16, answer 2)."""
    seen: list[Any] = []

    def fake_explain_symbol(db: Any, settings: Any, symbol: str, strategy: str, **kwargs: Any) -> Any:
        seen.append(kwargs.get("search"))
        raise RuntimeError("stop here; the call is what is under test")

    monkeypatch.setattr("candlegraph.knowledge.explain.explain_symbol", fake_explain_symbol)

    with pytest.raises(RuntimeError):
        explain_job_body(_ctx(seeded(), registry, strategies=("bollinger",), symbols=("WBTC",)))

    assert seen == [False]


def test_the_explain_job_has_no_search_parameter() -> None:
    params = JOBS["explain"].params
    assert params is not None
    assert "search" not in params.model_fields


def test_explain_records_which_narrator_wrote_the_prose(
    monkeypatch: pytest.MonkeyPatch, registry: TokenRegistry
) -> None:
    """A model that silently stopped being used is the worst outcome."""

    class FakeExplanation:
        citations = ()
        model = None  # the template

    monkeypatch.setattr(
        "candlegraph.knowledge.explain.explain_symbol",
        lambda *a, **k: FakeExplanation(),
    )

    outcome = explain_job_body(_ctx(seeded(), registry, strategies=("bollinger",), symbols=("WBTC",)))

    assert outcome.outcome is Outcome.SUCCEEDED
    assert outcome.detail["models"] == ["template"]
    assert outcome.detail["search"] is False


# --- the cooperative deadline ----------------------------------------------------


def _ctx(db: FakeDatabase, registry: TokenRegistry, **params: Any) -> Any:
    from candlegraph.harness.jobs import JobContext
    from candlegraph.harness.runs import MANUAL, Run

    return JobContext(
        settings=make_settings(),
        db=db,
        run=Run(job="test", trigger=MANUAL),
        now=NOW,
        on_stage=None,
        params={"registry": registry, **params},
    )


def test_a_step_past_its_deadline_stops_between_tokens(registry: TokenRegistry) -> None:
    """Cooperative, so it stops cleanly rather than half-writing one token."""
    past = datetime.now(tz=UTC) - timedelta(seconds=1)

    outcome = analyse_job_body(_ctx(seeded(), registry, deadline=past))

    # Nothing was attempted, so the step did not half-work.
    assert outcome.outcome is Outcome.FAILED
    assert outcome.counts["tokens"] == 0


def test_a_deadline_in_the_future_does_not_stop_anything(registry: TokenRegistry) -> None:
    ahead = datetime.now(tz=UTC) + timedelta(hours=1)

    outcome = analyse_job_body(_ctx(seeded(), registry, deadline=ahead))

    assert outcome.outcome is Outcome.SUCCEEDED
    assert outcome.counts["tokens"] == 2


def test_a_step_with_no_deadline_runs_everything(registry: TokenRegistry) -> None:
    outcome = validate_job_body(_ctx(seeded(), registry, min_coverage=0.5))

    assert outcome.outcome is Outcome.SUCCEEDED


# --- the run-twice proofs --------------------------------------------------------
#
# Named by tests/unit/harness/test_jobs.py::IDEMPOTENCE_PROOFS. Each runs the
# job twice against the same database and compares the state it left: the
# claim `idempotent=True` makes is exactly that a step can be re-run after a
# crash, so it is shown rather than asserted.


def _state(db: FakeDatabase) -> tuple[Any, ...]:
    """Everything a job wrote, with its own ledger rows removed.

    A new ledger row per run is what a run *is*; it is not state the next
    run would find different.
    """
    writes = [(label, params) for label, params in db.executed if label not in _LEDGER]
    upserts = [(label, rows) for label, rows in db.upserts]
    return (writes, upserts)


_LEDGER = {"open_run", "close_run", "open_agent_run", "close_agent_run", "start_run", "finish_run"}


def test_running_validate_twice_writes_the_same_state(registry: TokenRegistry) -> None:
    db = seeded()

    first = run("validate", db, registry, min_coverage=0.5)
    after_one = _state(db)
    second = run("validate", db, registry, min_coverage=0.5)

    assert _state(db) == after_one == ([], [])  # it reads; it writes nothing
    assert first.outcome is second.outcome
    assert first.result.counts == second.result.counts


def test_running_analyse_twice_writes_the_same_state(registry: TokenRegistry) -> None:
    db = seeded()

    first = run("analyse", db, registry)
    after_one = _state(db)
    second = run("analyse", db, registry)

    assert _state(db) == after_one == ([], [])
    assert first.result.counts == second.result.counts
    assert first.result.detail["by_symbol"] == second.result.detail["by_symbol"]


def test_running_signal_twice_writes_the_same_state(registry: TokenRegistry) -> None:
    """The second pass upserts the same rows on the same natural key.

    Not "writes nothing the second time" — it writes the same thing, which
    is what idempotent means for an upsert (invariant 4's shape).
    """
    db = seeded()

    first = run("signal", db, registry, strategies=("bollinger",), symbols=("WBTC",))
    (_, rows_one) = db.upserts[0]
    second = run("signal", db, registry, strategies=("bollinger",), symbols=("WBTC",))
    (_, rows_two) = db.upserts[1]

    def keys(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
        return [(r["strategy"], r["symbol"], r["period_start_unix"], r["signal"]) for r in rows]

    assert keys(rows_one) == keys(rows_two)
    assert first.result.counts == second.result.counts
    assert first.outcome is second.outcome


def test_running_explain_twice_writes_the_same_state(
    monkeypatch: pytest.MonkeyPatch, registry: TokenRegistry
) -> None:
    """`explanation` upserts on the signal's natural key, so a re-run refreshes it."""
    calls: list[tuple[str, str]] = []

    class FakeExplanation:
        citations = ()
        model = None

    def fake(db: Any, settings: Any, symbol: str, strategy: str, **kwargs: Any) -> Any:
        calls.append((symbol, strategy))
        return FakeExplanation()

    monkeypatch.setattr("candlegraph.knowledge.explain.explain_symbol", fake)
    db = seeded()

    first = run("explain", db, registry, strategies=("bollinger",), symbols=("WBTC",))
    second = run("explain", db, registry, strategies=("bollinger",), symbols=("WBTC",))

    assert calls == [("WBTC", "bollinger"), ("WBTC", "bollinger")]
    assert first.result.counts == second.result.counts
    assert _state(db) == ([], [])  # the upsert is inside explain_symbol, faked here


def test_running_embed_twice_writes_the_same_state() -> None:
    """The second run finds nothing pending: a chunk is embedded once per model."""
    from candlegraph.knowledge.embed import EmbedSummary

    pending: dict[str, int] = {"left": 7}

    def fake_embed_pending(db: Any, embedder: Any, **kwargs: Any) -> EmbedSummary:
        embedded, pending["left"] = pending["left"], 0
        return EmbedSummary(model="fake", embedded=embedded, batches=1, remaining=0)

    settings = make_settings(embedding_provider="fake")
    db = FakeDatabase(count_embedding_backlog=[{"missing": 7, "stale": 0, "total": 7}])
    import candlegraph.knowledge.embed as embed_module

    original = embed_module.embed_pending
    embed_module.embed_pending = fake_embed_pending  # type: ignore[assignment]
    try:
        first = run_job(JOBS["embed"], settings, db=db)
        second = run_job(JOBS["embed"], settings, db=db)
    finally:
        embed_module.embed_pending = original  # type: ignore[assignment]

    assert first.result.counts["embedded"] == 7
    assert second.result.counts["embedded"] == 0
    assert second.result.counts["remaining"] == 0
    assert first.outcome is second.outcome is Outcome.SUCCEEDED


def test_running_fetch_news_twice_writes_the_same_state(registry: TokenRegistry) -> None:
    """The (asset, UTC day) cache makes the second run of a day free.

    That is what lets the daily pipeline be retried, and what makes a
    container restart cost nothing.
    """
    from candlegraph.knowledge.news import NewsFetchSummary

    calls: list[int] = []

    def fake_run_news_fetch(db: Any, settings: Any, **kwargs: Any) -> NewsFetchSummary:
        calls.append(1)
        return NewsFetchSummary(day="2026-11-17", outcomes=[])

    import candlegraph.knowledge.news as news_module

    original = news_module.run_news_fetch
    news_module.run_news_fetch = fake_run_news_fetch  # type: ignore[assignment]
    try:
        db = FakeDatabase()
        first = run("fetch_news", db, registry)
        second = run("fetch_news", db, registry)
    finally:
        news_module.run_news_fetch = original  # type: ignore[assignment]

    assert len(calls) == 2
    assert first.result.counts == second.result.counts
    assert first.outcome is second.outcome is Outcome.SUCCEEDED
