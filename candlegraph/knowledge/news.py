"""The news pipeline: budget, cache and ledger around every source call.

The call count is a function of the token list and the calendar, never of
demand (``docs/knowledge/tavily/DESIGN.md``): the cache key is
``(asset, UTC day)``, checked against the ``source_call`` ledger in the
database, so every signal on WETH today shares one search whoever asks.
Around each call, in order:

1. **Cache** — a successful call for this ``(symbol, day)`` already in the
   ledger means no network at all.
2. **Budget** — the pure guard over the ledger's month and day sums. A
   refusal is itself a ``source_call`` row (``budget_refused``, 0 credits),
   because the observer counts refusals.
3. **The call** — one search; documents land through
   ``documents.store_document`` (upsert on ``url_hash``), chunks join the
   embedding backlog, and the outcome lands in the ledger: ``ok``,
   ``error``, or ``rate_limited``. A rate limit **stops the whole run** —
   the next call would spend budget on the same failure.

This module is a pipeline step, not an agent: invariant 21 stands, and the
observer only ever reads what is written here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from candlegraph.knowledge.assets import sync_assets
from candlegraph.knowledge.budget import Budget, decide
from candlegraph.knowledge.documents import ensure_source, store_document
from candlegraph.knowledge.mentions import alias_dictionary, link_mentions
from candlegraph.knowledge.sources.base import Source
from candlegraph.knowledge.sources.tavily import TavilyError, TavilyRateLimitError, TavilySource
from candlegraph.knowledge.statements import knowledge_statements_for
from candlegraph.settings import Settings
from candlegraph.storage import DatabaseManager
from candlegraph.tokens import Token, TokenRegistry, get_token_registry
from candlegraph.utils.logging_utils import service_logger

Status = Literal["ok", "cached", "budget_refused", "rate_limited", "error"]


class NewsError(RuntimeError):
    """The pipeline cannot run at all — phrased for the operator."""


def news_query(token: Token) -> str:
    """The search query for one token, derived from its aliases.

    Derived, not free-form: the cache key is the *intent* (asset and day),
    and the query is a function of the asset so two callers cannot phrase
    their way into two credits.
    """
    aliases = [a for a in token.aliases if a.casefold() != token.symbol.casefold()]
    names = " ".join(dict.fromkeys([token.symbol, *aliases[:2]]))
    return f"{names} crypto news"


def cache_key(symbol: str, day: datetime) -> str:
    """``SYMBOL:YYYY-MM-DD`` in UTC — one search per asset per day."""
    return f"{symbol}:{day.astimezone(UTC).date().isoformat()}"


def spend_boundaries(now: datetime) -> tuple[datetime, datetime]:
    """The UTC month and day starts the ledger sums run from."""
    moment = now.astimezone(UTC)
    day_start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start.replace(day=1), day_start


@dataclass(frozen=True)
class FetchOutcome:
    """What happened for one token."""

    symbol: str
    status: Status
    credits: int = 0
    results: int = 0
    documents_stored: int = 0
    chunks_written: int = 0
    mentions_linked: int = 0
    message: str = ""


@dataclass(frozen=True)
class NewsFetchSummary:
    """One run of the pipeline over some tokens."""

    day: str
    outcomes: list[FetchOutcome] = field(default_factory=list)

    @property
    def credits_spent(self) -> int:
        return sum(o.credits for o in self.outcomes)

    @property
    def documents_stored(self) -> int:
        return sum(o.documents_stored for o in self.outcomes)

    @property
    def failed(self) -> list[FetchOutcome]:
        """Outcomes a human should look at; a refusal or a cache hit is not one."""
        return [o for o in self.outcomes if o.status in ("error", "rate_limited")]

    @property
    def ok(self) -> bool:
        return not self.failed


def budget_from(settings: Settings) -> Budget:
    return Budget(
        monthly_credits=settings.tavily_monthly_credits,
        daily_credits=settings.tavily_daily_credits,
        reserve=settings.tavily_reserve_credits,
    )


def source_spend(db: DatabaseManager, source_id: str, now: datetime) -> tuple[int, int]:
    """Credits this source spent (this UTC month, today), from the ledger."""
    statements = knowledge_statements_for(db.schema)
    month_start, day_start = spend_boundaries(now)
    rows = db.fetch_all(
        statements.source_spend,
        {"source_id": source_id, "month_start": month_start, "day_start": day_start},
    )
    # The SUM query always yields one row on a real database; a fake with no
    # scripted answer yields none, and an empty ledger has spent nothing.
    if not rows:
        return 0, 0
    return int(rows[0]["month"]), int(rows[0]["today"])


def _record(
    db: DatabaseManager,
    source_id: str,
    now: datetime,
    query: str,
    params: dict[str, Any],
    credits: int,
    results: int | None,
    status: str,
    error: str | None = None,
) -> None:
    statements = knowledge_statements_for(db.schema)
    db.execute(
        statements.insert_source_call,
        {
            "source_id": source_id,
            "called_at": now,
            "query": query,
            "params": json.dumps(params, default=str),
            "credits": credits,
            "results": results,
            "status": status,
            "error": error,
        },
    )


def fetch_news(
    db: DatabaseManager,
    settings: Settings,
    source: Source,
    tokens: list[Token] | None = None,
    now: datetime | None = None,
) -> NewsFetchSummary:
    """One pass over ``tokens``: cache, budget, search, store, ledger.

    Args:
        db: The database.
        settings: Budget caps and the search window width.
        source: The metered source; tests hand in a fake.
        tokens: Defaults to the configured registry.
        now: The bar the window ends at; defaults to the current hour. The
            window is ``[now - KNOWLEDGE_SEARCH_WINDOW_HOURS, now]``.

    Returns:
        A summary with one outcome per token. Never raises for a failing
        call — the failure *is* the ledger row — but a rate limit stops the
        remaining tokens, because repeating it would spend budget on the
        same failure.
    """
    moment = (now or datetime.now(tz=UTC)).astimezone(UTC)
    tokens = list(tokens) if tokens is not None else list(get_token_registry())
    budget = budget_from(settings)
    statements = knowledge_statements_for(db.schema)
    ensure_source(db, source.id, source.kind)
    # Mentions are foreign keys into `asset`; make sure every token this run
    # may link to has its row, and build the alias dictionary once.
    registry = TokenRegistry(tokens=tuple(tokens))
    if tokens:
        sync_assets(db, registry)
    aliases = alias_dictionary(registry)

    summary_day = moment.date().isoformat()
    outcomes: list[FetchOutcome] = []
    halted = False
    for token in tokens:
        if halted:
            outcomes.append(
                FetchOutcome(
                    token.symbol, "rate_limited", message="skipped: an earlier call was rate limited"
                )
            )
            continue

        query = news_query(token)
        key = cache_key(token.symbol, moment)
        (cached,) = db.fetch_all(statements.cached_search_exists, {"source_id": source.id, "cache_key": key})
        if int(cached["hits"]) > 0:
            outcomes.append(FetchOutcome(token.symbol, "cached", message="already searched today"))
            continue

        month, today = source_spend(db, source.id, moment)
        decision = decide(month, today, source.cost, budget)
        if not decision:
            _record(
                db,
                source.id,
                moment,
                query,
                {"cache_key": key, "refused": decision.reason},
                credits=0,
                results=None,
                status="budget_refused",
                error=f"budget refused: {decision.reason} cap",
            )
            outcomes.append(
                FetchOutcome(token.symbol, "budget_refused", message=f"{decision.reason} cap reached")
            )
            continue

        start = moment - timedelta(hours=settings.knowledge_search_window_hours)
        try:
            result = source.search(query, start=start, end=moment, max_results=settings.tavily_max_results)
        except TavilyRateLimitError as exc:
            _record(
                db,
                source.id,
                moment,
                query,
                {"cache_key": key},
                credits=source.cost,
                results=None,
                status="rate_limited",
                error=str(exc),
            )
            outcomes.append(FetchOutcome(token.symbol, "rate_limited", credits=source.cost, message=str(exc)))
            halted = True
            continue
        except TavilyError as exc:
            _record(
                db,
                source.id,
                moment,
                query,
                {"cache_key": key},
                credits=source.cost,
                results=None,
                status="error",
                error=str(exc),
            )
            outcomes.append(FetchOutcome(token.symbol, "error", credits=source.cost, message=str(exc)))
            continue

        stored = chunks = linked = 0
        for document in result.documents:
            written = store_document(db, document, retrieved_at=moment)
            stored += 1
            chunks += written.chunks_written
            linked += link_mentions(db, written.document_id, aliases)
        _record(
            db,
            source.id,
            moment,
            query,
            {"cache_key": key, **result.params, "stored": stored, "skipped": result.skipped},
            credits=result.credits,
            results=result.results,
            status="ok",
        )
        outcomes.append(
            FetchOutcome(
                token.symbol,
                "ok",
                credits=result.credits,
                results=result.results,
                documents_stored=stored,
                chunks_written=chunks,
                mentions_linked=linked,
            )
        )
        service_logger.info(
            "news: %s — %d results, %d documents, %d chunks", token.symbol, result.results, stored, chunks
        )

    return NewsFetchSummary(day=summary_day, outcomes=outcomes)


def build_tavily_source(settings: Settings) -> TavilySource:
    """The configured client, or a readable refusal when there is no key."""
    if settings.tavily_api_key is None:
        raise NewsError(
            "TAVILY_API_KEY is not set; the news pipeline cannot search. "
            "Add it to .env (free tier at tavily.com), or run nothing — stored documents still serve."
        )
    return TavilySource(
        api_key=settings.tavily_api_key.get_secret_value(),
        search_depth=settings.tavily_search_depth,
        timeout=settings.tavily_timeout_seconds,
        max_tries=settings.tavily_max_tries,
    )


def run_news_fetch(
    db: DatabaseManager,
    settings: Settings,
    symbols: list[str] | None = None,
    registry: TokenRegistry | None = None,
    now: datetime | None = None,
) -> NewsFetchSummary:
    """The operator entry point: build the client, resolve symbols, fetch.

    Raises:
        NewsError: no API key, or a symbol that is not tracked.
    """
    registry = registry or get_token_registry()
    if symbols:
        tokens = []
        for symbol in symbols:
            token = registry.by_symbol(symbol)
            if token is None:
                raise NewsError(f"unknown token {symbol!r}; tracked: {', '.join(registry.symbols)}")
            tokens.append(token)
    else:
        tokens = list(registry)
    return fetch_news(db, settings, build_tavily_source(settings), tokens, now=now)
