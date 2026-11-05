"""The GraphQL surface for signal explanations.

Two reads and one write, with one rule between them
(``docs/knowledge/graph/DESIGN.md``, "Serving explanations"):

- ``signalExplanation`` and ``signalExplanations`` read the ``explanation``
  table. Free, no network, no model: what a client shows before anyone
  decides to spend anything.
- ``explainSignal`` is a **mutation** — it runs the strategy, may call the
  news source, writes a row — and its ``search`` argument defaults to
  ``false``. A credit is spent only when the caller says so, the same rule
  the UI's button follows and the CLI's ``--no-search`` inverts.

Resolver bodies live outside the Strawberry types so tests can call them
with a fake database, the pattern :mod:`candlegraph.api.schema` set.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import strawberry
from strawberry.scalars import JSON

from candlegraph.knowledge.explain import (
    ExplainError,
    Explanation,
    explain_symbol,
    load_explanation,
    recent_explanations,
)
from candlegraph.knowledge.retrieve import RetrievalError
from candlegraph.settings import Settings, get_settings
from candlegraph.storage import DatabaseManager, get_db


@strawberry.type
class Citation:
    """One retrieved chunk the rationale may cite, numbered as the prose refers to it."""

    n: int
    chunk_id: int
    document_id: int
    url: str
    title: str | None
    published_at: str | None
    """ISO 8601 UTC, or null when the source gave no date (never defaulted)."""
    retrieved_at: str
    quote: str
    legs: str
    """``dense``, ``lexical`` or ``both`` — which retrieval leg(s) found it."""
    score: float


@strawberry.type
class SignalExplanation:
    """One explained signal: the numbers, the citations, the accepted prose."""

    strategy: str
    symbol: str
    bar: str
    """The explained bar, ISO 8601 UTC."""
    period_start_unix: int
    position: int
    position_label: str
    score: float | None
    rationale: str
    """Validated prose: every ``[n]`` refers to a citation below, or the sentence was removed."""
    model: str | None
    """The narrator model, or null for the deterministic template."""
    generated_at: str
    numeric_context: JSON
    citations: list[Citation]
    news_status: str
    """``ok`` | ``cached`` | ``budget_refused`` | ``rate_limited`` | ``error`` | ``skipped`` | ``stored``."""
    news_message: str
    dense_used: bool
    rejected_citations: list[int]
    """Citation numbers the narrator invented; their sentences were stripped."""


@strawberry.type
class ExplanationSummary:
    """One row of the explanation ledger, newest first."""

    strategy: str
    symbol: str
    bar: str
    period_start_unix: int
    position: int
    citations: int
    model: str | None
    generated_at: str


def _iso(moment: datetime | None) -> str | None:
    return moment.astimezone(UTC).isoformat() if moment is not None else None


def to_graphql(explanation: Explanation) -> SignalExplanation:
    return SignalExplanation(
        strategy=explanation.strategy,
        symbol=explanation.symbol,
        bar=explanation.bar.astimezone(UTC).isoformat(),
        period_start_unix=explanation.period_start_unix,
        position=explanation.position,
        position_label=explanation.position_label,
        score=explanation.score,
        rationale=explanation.rationale,
        model=explanation.model,
        generated_at=explanation.generated_at.astimezone(UTC).isoformat(),
        numeric_context=cast(JSON, explanation.numeric_context),
        citations=[
            Citation(
                n=c.n,
                chunk_id=c.chunk_id,
                document_id=c.document_id,
                url=c.url,
                title=c.title,
                published_at=_iso(c.published_at),
                retrieved_at=c.retrieved_at.astimezone(UTC).isoformat(),
                quote=c.quote,
                legs=c.legs,
                score=c.score,
            )
            for c in explanation.citations
        ],
        news_status=explanation.news_status,
        news_message=explanation.news_message,
        dense_used=explanation.dense_used,
        rejected_citations=list(explanation.rejected_citations),
    )


def _parse_at(at: str | None) -> datetime | None:
    if at is None:
        return None
    try:
        return datetime.fromisoformat(at).astimezone(UTC)
    except ValueError as exc:
        raise ValueError(f"`at` must be ISO 8601 (got {at!r})") from exc


def fetch_signal_explanation(
    token_symbol: str,
    strategy: str = "bollinger",
    at: str | None = None,
    db: DatabaseManager | None = None,
) -> SignalExplanation | None:
    """The stored explanation of a bar (the latest for the pair when ``at`` is null). Read-only."""
    found = load_explanation(db or get_db(), strategy, token_symbol.upper(), bar=_parse_at(at))
    return to_graphql(found) if found else None


def fetch_signal_explanations(
    token_symbol: str | None = None, limit: int = 20, db: DatabaseManager | None = None
) -> list[ExplanationSummary]:
    """Summary rows of the newest explanations, optionally for one symbol."""
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")
    rows = recent_explanations(db or get_db(), token_symbol.upper() if token_symbol else None, limit)
    return [
        ExplanationSummary(
            strategy=str(row["strategy"]),
            symbol=str(row["symbol"]),
            bar=datetime.fromtimestamp(int(row["period_start_unix"]), tz=UTC).isoformat(),
            period_start_unix=int(row["period_start_unix"]),
            position=int(row["position"]),
            citations=int(row.get("citations") or 0),
            model=row.get("model"),
            generated_at=row["generated_at"].astimezone(UTC).isoformat(),
        )
        for row in rows
    ]


def run_explain_signal(
    token_symbol: str,
    strategy: str = "bollinger",
    at: str | None = None,
    search: bool = False,
    days: int | None = None,
    db: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> SignalExplanation:
    """Run the strategy, explain one bar, store the row. ``search`` spends at most one credit."""
    try:
        explanation = explain_symbol(
            db or get_db(),
            settings or get_settings(),
            token_symbol,
            strategy,
            at=_parse_at(at),
            days=days,
            search=search,
        )
    except (ExplainError, RetrievalError, KeyError) as exc:
        # KeyError is the strategy registry's "unknown strategy" message.
        raise ValueError(str(exc).strip("'\"")) from exc
    return to_graphql(explanation)


@strawberry.type
class ExplanationQuery:
    """Read-only explanation fields, merged into the root ``Query``."""

    @strawberry.field
    def signal_explanation(
        self, token_symbol: str, strategy: str = "bollinger", at: str | None = None
    ) -> SignalExplanation | None:
        """The stored explanation of one bar, or the latest stored for the strategy and symbol.

        Reads the ``explanation`` table only — no network, no model, no
        credit. Null when nothing has been explained yet; ``explainSignal``
        is the mutation that creates one.
        """
        return fetch_signal_explanation(token_symbol, strategy, at)

    @strawberry.field
    def signal_explanations(
        self, token_symbol: str | None = None, limit: int = 20
    ) -> list[ExplanationSummary]:
        """The newest explanations on record, optionally for one symbol."""
        return fetch_signal_explanations(token_symbol, limit)


@strawberry.type
class Mutation:
    """Root mutation type: the operations that write or spend."""

    @strawberry.mutation
    def explain_signal(
        self,
        token_symbol: str,
        strategy: str = "bollinger",
        at: str | None = None,
        search: bool = False,
        days: int | None = None,
    ) -> SignalExplanation:
        """Explain one bar of a strategy on a token and store the result.

        Args:
            token_symbol: A tracked symbol, case-insensitive.
            strategy: A registered strategy name; its default configuration is used.
            at: The bar to explain, ISO 8601 UTC. Default: the latest bar.
            search: Whether the metered news source may be called for the
                asset's UTC day. **Default false**: without it the stored
                corpus serves and ``newsStatus`` says ``skipped``. With it,
                at most one credit is spent per token per day, whoever asks.
            days: Price history to run the strategy over. Default:
                ``ANALYTICS_LOOKBACK_DAYS``.
        """
        return run_explain_signal(token_symbol, strategy, at, search, days)
