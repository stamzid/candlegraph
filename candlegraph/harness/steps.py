"""The job bodies that turn existing commands into pipeline steps.

Five adapters — ``validate``, ``analyse``, ``signal``, ``explain`` and
``embed`` — each of which loads what its domain function needs, calls it, and
grades the result into a :class:`RunOutcome`. **No arithmetic and no SQL
lives here.** The quality report, the rolling statistics, the strategies and
the embed loop are all unchanged; this module decides only what "the run
succeeded" means for each of them.

They are here rather than in their own packages because that is what they
are. ``ingest`` and ``fetch_news`` keep their bodies beside the machinery
they drive (``ingestion/runner.py``, ``knowledge/news.py``) because those
bodies *are* that machinery's entry point. These five would otherwise put a
``JobContext`` inside ``analytics/``, which only reads the database and only
through ``prices.py`` (invariant 18).

Three rules they share:

- **Causal data only.** Anything that feeds a strategy goes through
  ``load_symbol_series``, which refuses a fill policy that reads across the
  gap it fills (invariant 19). None of these jobs takes a ``fill``
  parameter, so there is no way to ask for one.
- **A token that fails is not a run that fails.** One unreadable symbol makes
  the run ``partial`` and is counted; the rest still run. Nothing here
  decides a position, so a partial pass is genuinely partial, not wrong.
- **The deadline is cooperative.** Each loop checks ``ctx.params['deadline']``
  between tokens and stops cleanly if the step has run out of time. Python
  cannot safely kill a thread, so a step that does not check simply runs on
  (pipelines DESIGN §7.5).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

from candlegraph.harness.jobs import JobContext
from candlegraph.harness.runs import Outcome, RunOutcome
from candlegraph.tokens import Token, TokenRegistry
from candlegraph.utils.logging_utils import service_logger


def _registry(ctx: JobContext) -> TokenRegistry:
    """The deployment's tokens; ``registry`` in params is for tests."""
    from candlegraph.tokens import get_token_registry

    registry = ctx.params.get("registry")
    return registry if isinstance(registry, TokenRegistry) else get_token_registry()


def _tokens(ctx: JobContext) -> list[Token]:
    """The tokens a step works over: those named, else every active one.

    An unknown symbol raises, because a spec naming a token this deployment
    does not track is a mistake someone should hear about rather than a
    quietly shorter run. Symbols are not checked at load time — see
    ``harness/params.py`` for why.
    """
    registry = _registry(ctx)
    wanted = tuple(ctx.params.get("symbols") or ())
    if not wanted:
        return list(registry.active)
    tokens = []
    for symbol in wanted:
        token = registry.by_symbol(symbol)
        if token is None:
            raise ValueError(f"unknown token {symbol!r}; tracked: {', '.join(registry.symbols)}")
        tokens.append(token)
    return tokens


def _days(ctx: JobContext) -> int:
    days = ctx.params.get("days")
    return int(days) if days else int(ctx.settings.analytics_lookback_days)


def _out_of_time(ctx: JobContext) -> bool:
    """Has this step used its timeout? ``None`` means it has no deadline."""
    deadline = ctx.params.get("deadline")
    return isinstance(deadline, datetime) and datetime.now(tz=UTC) >= deadline


def _within_deadline(ctx: JobContext, tokens: Sequence[Token]) -> Iterator[Token]:
    """Walk the tokens, stopping if the step runs out of time."""
    for position, token in enumerate(tokens):
        if _out_of_time(ctx):
            service_logger.warning(
                "%s: stopping after %d of %d token(s); the step's timeout has passed",
                ctx.run.job,
                position,
                len(tokens),
            )
            return
        yield token


def _graded(
    *, done: int, expected: int, errors: dict[str, str], counts: dict[str, int], detail: dict[str, Any]
) -> RunOutcome:
    """The outcome rule every looping step shares.

    Nothing attempted is ``failed``: a step that read no token at all did not
    half-work, it did not work. Some but not all is ``partial``.
    """
    if done == 0 and expected > 0:
        outcome = Outcome.FAILED
    elif done < expected or errors:
        outcome = Outcome.PARTIAL
    else:
        outcome = Outcome.SUCCEEDED
    return RunOutcome(outcome=outcome, counts=counts, errors=errors, detail=detail)


# --- validate ------------------------------------------------------------------


def validate_job_body(ctx: JobContext) -> RunOutcome:
    """The data-quality report, graded as a gate for the steps behind it.

    ``failed`` when an **active token has no rows at all** or a row is
    impossible (a high below a low), because statistics computed on that are
    not worth having. ``partial`` when coverage is merely under the
    threshold: a thin token legitimately has missing hours (invariant 19), so
    low coverage is a reason to look, not a reason to stop the hour's work.
    """
    from candlegraph.ingestion.quality import build_report

    tokens = _tokens(ctx)
    minimum = ctx.params.get("min_coverage")
    report = build_report(
        ctx.db,
        expected_symbols=tuple(token.symbol for token in tokens),
        min_expected_coverage=(float(minimum) if minimum is not None else ctx.settings.min_expected_coverage),
        now=ctx.params.get("now") or ctx.now,
    )

    under = [token.symbol for token in report.under_covered]
    counts = {
        "tokens": len(report.tokens),
        "rows": report.total_rows,
        "missing_tokens": len(report.missing_tokens),
        "under_covered": len(under),
        "violations": len(report.violations),
    }
    errors: dict[str, str] = dict.fromkeys(report.missing_tokens, "no stored rows at all")
    detail: dict[str, Any] = {
        "min_expected_coverage": report.min_expected_coverage,
        "under_covered": under,
        "coverage": {t.symbol: round(t.coverage, 4) for t in report.tokens},
    }
    if report.violations:
        errors["__rows__"] = f"{len(report.violations)} impossible row(s)"

    if report.missing_tokens or report.violations:
        outcome = Outcome.FAILED
    elif under:
        outcome = Outcome.PARTIAL
    else:
        outcome = Outcome.SUCCEEDED
    return RunOutcome(outcome=outcome, counts=counts, errors=errors, detail=detail, payload=report)


# --- analyse -------------------------------------------------------------------


def analyse_job_body(ctx: JobContext) -> RunOutcome:
    """Rolling statistics over the stored history. Stores nothing.

    The run records what it computed — bars read, the latest volatility per
    token — so "the hour was analysed" is a fact in the ledger rather than an
    assumption. Writing the numbers to a table would duplicate what
    ``prices`` plus a pure function already give on demand.
    """
    from candlegraph.analytics.prices import PriceError, load_symbol_series
    from candlegraph.analytics.rolling import rolling_summary

    registry = _registry(ctx)
    tokens = _tokens(ctx)
    window = int(ctx.params.get("window") or 24)
    days = _days(ctx)

    summaries: dict[str, Any] = {}
    errors: dict[str, str] = {}
    bars = 0
    done = 0
    for token in _within_deadline(ctx, tokens):
        try:
            prices = load_symbol_series(ctx.db, registry, token.symbol, days=days)
        except PriceError as exc:
            errors[token.symbol] = str(exc)
            continue
        frame = rolling_summary(prices, window=window, interval_hours=ctx.settings.analytics_interval_hours)
        bars += len(prices)
        done += 1
        # The newest bar with a complete window. Rolling windows are complete
        # or absent (invariant 16), so a short series yields None rather than
        # a figure computed from fewer bars than asked for.
        tail = frame.dropna()
        summaries[token.symbol] = {
            "bars": len(prices),
            "volatility": round(float(tail["rolling_volatility"].iloc[-1]), 6) if len(tail) else None,
            "mean": round(float(tail["rolling_mean"].iloc[-1]), 6) if len(tail) else None,
        }

    return _graded(
        done=done,
        expected=len(tokens),
        errors=errors,
        counts={"tokens": done, "bars": bars, "window": window},
        detail={"window": window, "days": days, "by_symbol": summaries},
    )


# --- signal --------------------------------------------------------------------


def signal_job_body(ctx: JobContext) -> RunOutcome:
    """Run the strategies over causal prices and upsert what they say.

    The prices come from ``load_symbol_series``, which refuses a non-causal
    fill policy, so no estimated hour can reach a stored signal — and because
    a gap produces no bar at all, a filled hour is not merely excluded from
    the decision, it is absent from the series the strategy ever sees.
    """
    from candlegraph.analytics.prices import PriceError, load_symbol_series
    from candlegraph.strategies.base import available, run_strategy
    from candlegraph.strategies.store import store_signals

    registry = _registry(ctx)
    tokens = _tokens(ctx)
    names = tuple(ctx.params.get("strategies") or ()) or available()
    store = bool(ctx.params.get("store", True))
    days = _days(ctx)

    errors: dict[str, str] = {}
    by_symbol: dict[str, Any] = {}
    written = 0
    done = 0
    for token in _within_deadline(ctx, tokens):
        try:
            prices = load_symbol_series(ctx.db, registry, token.symbol, days=days)
        except PriceError as exc:
            errors[token.symbol] = str(exc)
            continue
        latest: dict[str, str] = {}
        for name in names:
            try:
                result = run_strategy(name, prices, None)
            except Exception as exc:
                # One strategy failing (too little history for ARIMA, say) is
                # not the others failing.
                errors[f"{token.symbol}:{name}"] = f"{type(exc).__name__}: {exc}"
                continue
            latest[name] = result.latest.name.lower()
            if store:
                written += store_signals(ctx.db, result, symbol=token.symbol)
        if latest:
            done += 1
            by_symbol[token.symbol] = {"bars": len(prices), "latest": latest}

    return _graded(
        done=done,
        expected=len(tokens),
        errors=errors,
        counts={"tokens": done, "signals_written": written, "strategies": len(names)},
        detail={"strategies": list(names), "days": days, "stored": store, "by_symbol": by_symbol},
    )


# --- explain -------------------------------------------------------------------


def explain_job_body(ctx: JobContext) -> RunOutcome:
    """Cited prose for the newest bar of each strategy, from stored news only.

    ``search=False`` is not a default here, it is the only behaviour: see
    ``harness/params.py``. The narrator is whatever is configured — the
    template with no key, which is a first-class mode and costs nothing.
    """
    from candlegraph.knowledge.explain import ExplainError, explain_symbol
    from candlegraph.knowledge.retrieve import RetrievalError
    from candlegraph.strategies.base import available

    tokens = _tokens(ctx)
    names = tuple(ctx.params.get("strategies") or ()) or available()
    days = ctx.params.get("days")

    errors: dict[str, str] = {}
    explained = 0
    cited = 0
    models: set[str] = set()
    for token in _within_deadline(ctx, tokens):
        for name in names:
            if _out_of_time(ctx):
                break
            try:
                explanation = explain_symbol(
                    ctx.db,
                    ctx.settings,
                    token.symbol,
                    name,
                    days=int(days) if days else None,
                    search=False,
                    registry=_registry(ctx),
                    now=ctx.params.get("now"),
                )
            except (ExplainError, RetrievalError) as exc:
                errors[f"{token.symbol}:{name}"] = str(exc)
                continue
            explained += 1
            cited += len(explanation.citations)
            models.add(explanation.model or "template")

    expected = len(tokens) * len(names)
    return _graded(
        done=explained,
        expected=expected,
        errors=errors,
        counts={"explained": explained, "citations": cited},
        # Which narrator wrote them: a model that silently stopped being used
        # is the worst outcome of a configurable one.
        detail={"strategies": list(names), "models": sorted(models), "search": False},
    )


# --- refresh_aggregates ----------------------------------------------------------


def refresh_aggregates_job_body(ctx: JobContext) -> RunOutcome:
    """Bring `token_daily` up to date with the hourly rows.

    The whole range, every time. The refresh is driven by TimescaleDB's
    invalidation log, so "everything" costs 0.8 ms when nothing changed and
    12 ms after a 90-day backfill — a window would cost the same for the same
    change while leaving a deeper backfill stale, and `backfill --days N` can
    rewrite any hour (invariant 13).

    The run records the aggregate's extent, so the ledger says what the view
    held rather than only that something ran.
    """
    db = ctx.db
    db.execute_outside_transaction(db.sql.refresh_token_daily)
    rows = db.fetch_all(db.sql.aggregate_status)
    status = dict(rows[0]) if rows else {}
    buckets = int(status.get("buckets") or 0)
    return RunOutcome(
        outcome=Outcome.SUCCEEDED,
        counts={
            "buckets": buckets,
            "tokens": int(status.get("tokens") or 0),
            "hours": int(status.get("hours") or 0),
        },
        detail={
            "view": "token_daily",
            "first_day": status.get("first_day"),
            "last_day": status.get("last_day"),
        },
    )


# --- embed ---------------------------------------------------------------------


def embed_job_body(ctx: JobContext) -> RunOutcome:
    """Give pending chunks the active model's vector, bounded per run.

    This is the scheduled embed the pre-work left missing: without it the
    backlog only ever cleared when someone ran ``make embed``, and an
    explanation quietly fell back to the lexical leg
    (``docs/incidents/2026-11-04-explain-ran-lexical-only/``).

    An unreachable embedder is ``failed``, not an exception swallowed: the
    error classifier calls it transient and the step is retried.
    """
    from candlegraph.knowledge.embed import build_embedder, embed_pending, embedding_backlog

    limit = ctx.params.get("limit", 512)
    batch = ctx.params.get("batch_size") or ctx.settings.embedding_batch_size
    embedder = build_embedder(ctx.settings)
    try:
        before = embedding_backlog(ctx.db, ctx.settings.embedding_model)
        summary = embed_pending(
            ctx.db,
            embedder,
            batch_size=int(batch),
            limit=int(limit) if limit else None,
            dimensions=ctx.settings.embedding_dimensions,
        )
    finally:
        close = getattr(embedder, "close", None)
        if callable(close):
            close()

    return RunOutcome(
        outcome=Outcome.SUCCEEDED,
        counts={
            "embedded": summary.embedded,
            "batches": summary.batches,
            "remaining": summary.remaining,
        },
        detail={
            "model": summary.model,
            "backlog_before": before.pending,
            # Not an error: the limit is what keeps one run bounded, and the
            # next run takes the rest.
            "bounded_by_limit": summary.remaining > 0,
        },
        payload=summary,
    )
