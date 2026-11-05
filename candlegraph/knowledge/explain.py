"""``signals --explain``: what else was true at the bar, and the citations for it.

Rollout step 8 (``docs/rollout/ROLLOUT.md``); the sequence diagram and the
decisions are in ``docs/knowledge/graph/DESIGN.md``. In order:

1. **News for the day** — through :func:`candlegraph.knowledge.news.fetch_news`,
   which owns the ``(asset, UTC day)`` cache, the budget guard and the
   ``source_call`` row. Two explanations on one asset-day make one network
   call; with the budget spent, or with no ``TAVILY_API_KEY`` at all, the
   stored documents serve and the output says so.
2. **Retrieval as of the bar** — hybrid, ``published_at <= bar``; the
   retrieved chunks become the numbered citations and nothing else may be
   cited.
3. **The numeric context** — the z-score, the moves into the bar, the
   window statistics and the move against a reference asset, computed from
   prices **at or before the bar** only. A missing figure is ``None``,
   never ``0`` (invariant 11's shape).
4. **The narrator** — a template by default, which is a first-class mode;
   a Claude model when configured. Either sees the numbers and the
   citations and nothing else (invariant 22's boundary).
5. **The validator** — a sentence citing an id outside the retrieved set is
   stripped and the explanation says so.
6. **The row** — upserted on the signal's natural key, with what the
   narrator was shown, so the paragraph stays auditable after the rolling
   statistics have moved on.

This module is the one knowledge writer that is not the source pipeline;
it is not an agent (invariant 21) and writes no market data.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import pandas as pd

from candlegraph.analytics.pairwise import align_pair, compare_pair
from candlegraph.analytics.prices import load_price_frame
from candlegraph.knowledge.embed import Embedder, EmbeddingError, build_embedder, embed_pending
from candlegraph.knowledge.news import NewsError, build_tavily_source, fetch_news
from candlegraph.knowledge.retrieve import RetrievalConfig, RetrievedChunk, check_backend, retrieve
from candlegraph.knowledge.sources.base import Source
from candlegraph.knowledge.statements import knowledge_statements_for
from candlegraph.knowledge.validate import Validation, validate_rationale
from candlegraph.settings import Settings
from candlegraph.storage import DatabaseManager
from candlegraph.strategies.base import StrategyResult, run_strategy
from candlegraph.tokens import Token, TokenRegistry, get_token_registry
from candlegraph.utils.logging_utils import service_logger

POSITION_WORDS = {-1: "short", 0: "flat", 1: "long"}
QUOTE_CHARS = 280
# How many pending chunks one explanation will embed before retrieving, so
# the news it just fetched is visible to the dense leg. Bounded: a long
# backlog is `make embed`'s job, not a side effect of one command.
EXPLAIN_EMBED_LIMIT = 256
# Hours back from the bar the context reports a move over.
MOVE_HORIZONS_HOURS: tuple[int, ...] = (1, 3, 24)


class ExplainError(RuntimeError):
    """The explanation cannot be built at all — phrased for the operator."""


# --- citations and the result -------------------------------------------------------


@dataclass(frozen=True)
class Citation:
    """One retrieved chunk, numbered as the prose refers to it."""

    n: int
    chunk_id: int
    document_id: int
    url: str
    title: str | None
    published_at: datetime | None
    retrieved_at: datetime
    quote: str
    legs: str
    score: float

    @classmethod
    def from_chunk(cls, n: int, chunk: RetrievedChunk) -> Citation:
        text = " ".join(chunk.text.split())
        quote = text if len(text) <= QUOTE_CHARS else text[: QUOTE_CHARS - 1].rstrip() + "…"
        return cls(
            n=n,
            chunk_id=chunk.chunk_id,
            document_id=chunk.document_id,
            url=chunk.url,
            title=chunk.title,
            published_at=chunk.published_at,
            retrieved_at=chunk.retrieved_at,
            quote=quote,
            legs=chunk.legs,
            score=chunk.score,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "url": self.url,
            "title": self.title,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "retrieved_at": self.retrieved_at.isoformat(),
            "quote": self.quote,
            "legs": self.legs,
            "score": round(self.score, 6),
        }


@dataclass(frozen=True)
class Explanation:
    """One explained signal: the numbers, the citations, the accepted prose."""

    strategy: str
    symbol: str
    bar: datetime
    position: int
    score: float | None
    numeric_context: dict[str, Any]
    citations: tuple[Citation, ...]
    rationale: str
    model: str | None
    generated_at: datetime
    news_status: str
    """``ok`` | ``cached`` | ``budget_refused`` | ``rate_limited`` | ``error`` | ``skipped``."""
    news_message: str = ""
    dense_used: bool = False
    retrieval_note: str = ""
    rejected_citations: tuple[int, ...] = ()
    stripped_sentences: tuple[str, ...] = ()
    chunks_embedded: int = 0
    """Chunks embedded before retrieval so fresh news joined the dense leg."""

    @property
    def period_start_unix(self) -> int:
        return int(self.bar.timestamp())

    @property
    def position_label(self) -> str:
        return POSITION_WORDS[self.position]

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Explanation:
        """Rebuild a stored explanation from its ``explanation`` row.

        The row has no news or retrieval status — those described the run
        that wrote it — so a re-read says ``stored`` and nothing more.
        """
        citations_raw = row.get("citations") or []
        if isinstance(citations_raw, str):
            citations_raw = json.loads(citations_raw)
        context_raw = row.get("numeric_context") or {}
        if isinstance(context_raw, str):
            context_raw = json.loads(context_raw)
        citations = tuple(
            Citation(
                n=int(c["n"]),
                chunk_id=int(c["chunk_id"]),
                document_id=int(c["document_id"]),
                url=str(c["url"]),
                title=c.get("title"),
                published_at=datetime.fromisoformat(c["published_at"]) if c.get("published_at") else None,
                retrieved_at=datetime.fromisoformat(c["retrieved_at"]),
                quote=str(c.get("quote", "")),
                legs=str(c.get("legs", "")),
                score=float(c.get("score", 0.0)),
            )
            for c in citations_raw
        )
        bar = datetime.fromtimestamp(int(row["period_start_unix"]), tz=UTC)
        return cls(
            strategy=str(row["strategy"]),
            symbol=str(row["symbol"]),
            bar=bar,
            position=int(row["position"]),
            score=_finite(context_raw.get("score")),
            numeric_context=dict(context_raw),
            citations=citations,
            rationale=str(row.get("rationale") or ""),
            model=row.get("model"),
            generated_at=row["generated_at"],
            news_status="stored",
            news_message="read from the explanation table",
            dense_used=any(c.legs in ("dense", "both") for c in citations),
        )

    @property
    def cited(self) -> bool:
        return bool(self.citations)

    def to_row(self) -> dict[str, Any]:
        """The ``explanation`` upsert's parameters."""
        return {
            "strategy": self.strategy,
            "symbol": self.symbol,
            "period_start_unix": self.period_start_unix,
            "position": self.position,
            "numeric_context": json.dumps(self.numeric_context, default=str),
            "citations": json.dumps([c.to_dict() for c in self.citations]),
            "rationale": self.rationale,
            "model": self.model,
            "generated_at": self.generated_at,
        }


# --- the numeric context (pure) ----------------------------------------------------------


def _finite(value: Any) -> float | None:
    """A float, or ``None`` for NaN/inf — a figure we do not have is not zero."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else round(number, 6)


def _at_or_before(series: pd.Series, moment: datetime) -> float | None:
    """The last value at or before ``moment``, or ``None`` when there is none."""
    prior = series[series.index <= moment]
    return _finite(prior.iloc[-1]) if len(prior) else None


def _move(series: pd.Series, bar: datetime, hours: int) -> float | None:
    """Fractional change from the last price at or before ``bar - hours`` to the bar."""
    now = _at_or_before(series, bar)
    then = _at_or_before(series, bar - timedelta(hours=hours))
    if now is None or then is None or then == 0:
        return None
    return _finite(now / then - 1.0)


def numeric_context(
    prices: pd.Series,
    bar: datetime,
    *,
    score: float | None,
    window: int,
    reference: pd.Series | None = None,
    interval_hours: int = 1,
) -> dict[str, Any]:
    """The numbers the narrator is shown, computed from bars at or before ``bar``.

    Args:
        prices: The explained token's price series, indexed by UTC timestamp.
        bar: The bar being explained. Later bars are ignored — the context
            must not know what happened next.
        score: The strategy's own continuous quantity at the bar (the
            Bollinger z-score, the forecast return, …).
        window: The strategy's lookback in bars; the window statistics use it.
        reference: A market-wide reference series (``KNOWLEDGE_REFERENCE_SYMBOL``),
            so the prose can say whether the move was idiosyncratic.
        interval_hours: Bar width, for annualisation inside the comparison.

    Returns:
        A JSON-safe dict. Every figure that cannot be computed is ``None``.
    """
    history = prices.astype("float64").dropna()
    history = history[history.index <= bar]
    context: dict[str, Any] = {
        "bar": bar.astimezone(UTC).isoformat(),
        "bars_available": len(history),
        "price": _at_or_before(history, bar),
        "score": _finite(score),
        "window": window,
    }
    for hours in MOVE_HORIZONS_HOURS:
        context[f"move_{hours}h"] = _move(history, bar, hours)

    tail = history.tail(window)
    if len(tail) == window and window >= 2:
        mean = float(tail.mean())
        std = float(tail.std(ddof=1))
        context["window_mean"] = _finite(mean)
        context["window_std"] = _finite(std)
        price = context["price"]
        context["window_zscore"] = _finite((price - mean) / std) if price is not None and std > 0 else None
        context["window_high"] = _finite(tail.max())
        context["window_low"] = _finite(tail.min())
    else:
        for key in ("window_mean", "window_std", "window_zscore", "window_high", "window_low"):
            context[key] = None

    context["reference"] = None
    if reference is not None:
        ref = reference.astype("float64").dropna()
        ref = ref[ref.index <= bar]
        ref_move = _move(ref, bar, 24)
        own_move = context["move_24h"]
        aligned = align_pair(history, ref).tail(window + 1)
        correlation: float | None = None
        beta: float | None = None
        if len(aligned) >= 3:
            comparison = compare_pair(
                aligned.iloc[:, 0], aligned.iloc[:, 1], window=window, interval_hours=interval_hours
            )
            correlation = _finite(comparison.correlation)
            beta = _finite(comparison.beta)
        context["reference"] = {
            "symbol": str(reference.name or ""),
            "move_24h": ref_move,
            "relative_move_24h": _finite(own_move - ref_move)
            if own_move is not None and ref_move is not None
            else None,
            "aligned_bars": len(aligned),
            "correlation": correlation,
            "beta": beta,
        }
    return context


def explain_query(token: Token, position: int) -> str:
    """The dense leg's query: the asset and what the signal says about it.

    Derived, not free-form, like :func:`news.news_query`: the model never
    sees the search and two callers cannot phrase their way to two results.
    """
    aliases = [a for a in token.aliases if a.casefold() != token.symbol.casefold()]
    names = " ".join(dict.fromkeys([token.symbol, *([token.name] if token.name else []), *aliases[:2]]))
    direction = {-1: "price rally and selling pressure", 1: "price drop and buying", 0: "price"}[position]
    return f"{names} {direction} news"


# --- the narrator ---------------------------------------------------------------------------


class ExplanationNarrator(Protocol):
    """Numbers and citations in, a paragraph with ``[n]`` markers out."""

    @property
    def model(self) -> str | None:
        """What to stamp on the row; ``None`` for the template."""
        ...

    def narrate(self, context: Mapping[str, Any], citations: Sequence[Citation]) -> str: ...


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.1%}"


class TemplateExplanationNarrator:
    """Deterministic prose over the numbers, citing every retrieved chunk.

    The first-class mode: the exit criterion holds with no key configured.
    """

    @property
    def model(self) -> str | None:
        return None

    def narrate(self, context: Mapping[str, Any], citations: Sequence[Citation]) -> str:
        symbol = str(context.get("symbol", "The token"))
        position = int(context.get("position", 0))
        sentences: list[str] = []

        z = context.get("window_zscore")
        if z is None:
            z = context.get("score")
        window = context.get("window")
        if z is not None and window:
            side = "above" if z > 0 else "below"
            sentences.append(
                f"{symbol} closed {abs(z):.2f} standard deviations {side} its {window}-bar mean "
                f"(z={z:+.2f}) after a {_pct(context.get('move_3h'))} move over three hours "
                f"and {_pct(context.get('move_24h'))} over 24 hours; the signal is "
                f"{POSITION_WORDS[position]}."
            )
        else:
            sentences.append(
                f"{symbol} moved {_pct(context.get('move_24h'))} over 24 hours; "
                f"the signal is {POSITION_WORDS[position]}."
            )

        reference = context.get("reference")
        if reference and reference.get("move_24h") is not None and context.get("move_24h") is not None:
            own = float(context["move_24h"])
            ref = float(reference["move_24h"])
            # Half the token's own move is the line between "the market did
            # this" and "this token did this". A rule of thumb stated once,
            # here, so the stored numbers can always be re-read another way.
            idiosyncratic = abs(ref) < abs(own) / 2
            verdict = "idiosyncratic rather than market-wide" if idiosyncratic else "largely market-wide"
            sentences.append(
                f"{reference['symbol']} moved {_pct(ref)} over the same window, so the move is {verdict}."
            )

        if citations:
            markers = "".join(f"[{c.n}]" for c in citations)
            noun, verb = ("document", "was") if len(citations) == 1 else ("documents", "were")
            sentences.append(
                f"{len(citations)} {noun} published before the bar {verb} retrieved for context {markers}."
            )
        else:
            sentences.append(
                "No document published before the bar was retrieved; the signal stands on the numbers alone."
            )
        return " ".join(sentences)


_PROMPT = """You are explaining one trading signal for an analyst. Below are the computed
numbers for the bar and the documents retrieved for it, as JSON. Write one short
paragraph (at most four sentences). Use only facts present in the numbers and the
documents. Cite a document by its number in square brackets, like [1]; cite
nothing else, and do not invent numbers, causes or documents. Say whether the
move looks idiosyncratic or market-wide only if the reference figures are present.
Plain text, no Markdown.

Numbers:
{context}

Documents:
{citations}
"""

Completion = Callable[[str], str]


class AnthropicExplanationNarrator:
    """A Claude model writes the paragraph; the validator keeps it honest."""

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        complete: Completion | None = None,
        fallback: ExplanationNarrator | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._complete = complete or self._anthropic_completion
        self._fallback = fallback or TemplateExplanationNarrator()

    @property
    def model(self) -> str | None:
        return self._model

    def narrate(self, context: Mapping[str, Any], citations: Sequence[Citation]) -> str:
        prompt = _PROMPT.format(
            context=json.dumps(dict(context), indent=2, default=str),
            citations=json.dumps([c.to_dict() for c in citations], indent=2, default=str),
        )
        try:
            text = self._complete(prompt).strip()
        except Exception as exc:
            service_logger.warning("Explanation via %s failed; using the template: %s", self._model, exc)
            return self._fallback.narrate(context, citations)
        return text or self._fallback.narrate(context, citations)

    def _anthropic_completion(self, prompt: str) -> str:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on the optional extra
            raise RuntimeError("install the `llm` extra: uv sync --extra llm") from exc
        client = anthropic.Anthropic(api_key=self._api_key)
        response = client.messages.create(
            model=self._model, max_tokens=400, messages=[{"role": "user", "content": prompt}]
        )
        return "".join(getattr(block, "text", "") for block in response.content)


def build_narrator(settings: Settings) -> ExplanationNarrator:
    """The configured narrator: a model when named, the template otherwise."""
    model = settings.knowledge_llm_model or settings.agent_llm_model
    if not model:
        return TemplateExplanationNarrator()
    key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
    return AnthropicExplanationNarrator(model, api_key=key)


# --- the pipeline ------------------------------------------------------------------------------


@dataclass(frozen=True)
class ExplainRequest:
    """Everything :func:`explain` needs that is not a service."""

    token: Token
    strategy: str
    bar: datetime
    position: int
    score: float | None
    prices: pd.Series
    window: int
    reference: pd.Series | None = None
    params: dict[str, Any] = field(default_factory=dict)


def explain(
    db: DatabaseManager,
    settings: Settings,
    request: ExplainRequest,
    *,
    source: Source | None = None,
    embedder: Embedder | None = None,
    narrator: ExplanationNarrator | None = None,
    now: datetime | None = None,
) -> Explanation:
    """Build, validate and store the explanation of one signal.

    Args:
        db: The database. Reads the corpus; writes ``explanation`` and, via
            the news pipeline, documents and the ``source_call`` ledger.
        settings: Budget, retrieval depth, the narrator.
        request: The signal and the prices to explain it from.
        source: The metered news source. ``None`` means no search at all —
            stored documents serve and the result says so.
        embedder: The active model for the dense leg. ``None`` runs the
            lexical leg alone.
        narrator: Defaults to :func:`build_narrator`.
        now: The clock, for the ledger and ``generated_at``.

    Returns:
        The :class:`Explanation`, already upserted.
    """
    moment = (now or datetime.now(tz=UTC)).astimezone(UTC)
    bar = request.bar.astimezone(UTC)
    token = request.token
    check_backend(settings.retrieval_backend)

    if source is None:
        news_status, news_message = "skipped", "no news source configured; stored documents only"
    else:
        summary = fetch_news(db, settings, source, [token], now=moment, bar=bar)
        (outcome,) = summary.outcomes
        news_status, news_message = outcome.status, outcome.message
        if outcome.status == "ok":
            news_message = f"searched {token.symbol} for {bar:%Y-%m-%d}: {outcome.documents_stored} documents"
        elif outcome.status == "budget_refused":
            news_message = f"budget refused ({outcome.message}); serving stored documents"

    # What the search just stored has no vector yet; without this the dense
    # leg could never cite today's news and the explanation would quietly
    # rest on the lexical leg (docs/incidents/2026-11-04-explain-ran-lexical-only/).
    chunks_embedded = 0
    embed_note = ""
    if embedder is not None:
        try:
            chunks_embedded = embed_pending(
                db,
                embedder,
                batch_size=settings.embedding_batch_size,
                limit=EXPLAIN_EMBED_LIMIT,
                dimensions=settings.embedding_dimensions,
            ).embedded
        except EmbeddingError as exc:
            embed_note = f"embedding unavailable ({exc}); lexical only"
            service_logger.warning("explain: %s", embed_note)
            embedder = None

    top_k = settings.retrieval_top_k
    result = retrieve(
        db,
        explain_query(token, request.position),
        bar=bar,
        embedder=embedder,
        terms=list(token.aliases) or [t for t in (token.symbol, token.name) if t],
        asset_id=token.address,
        config=RetrievalConfig(top_k=top_k, candidates=max(settings.retrieval_candidates, top_k)),
    )
    citations = tuple(Citation.from_chunk(n, chunk) for n, chunk in enumerate(result.chunks, 1))

    reference = request.reference
    if reference is not None and str(reference.name or "").casefold() == token.symbol.casefold():
        reference = None  # a token is not its own market
    context = numeric_context(
        request.prices,
        bar,
        score=request.score,
        window=request.window,
        reference=reference,
        interval_hours=settings.analytics_interval_hours,
    )
    context = {
        "symbol": token.symbol,
        "strategy": request.strategy,
        "position": request.position,
        "params": request.params,
        **context,
    }

    narrator = narrator or build_narrator(settings)
    validation: Validation = validate_rationale(
        narrator.narrate(context, citations), [c.n for c in citations]
    )
    if not validation.clean:
        service_logger.warning(
            "explain: %s %s @ %s cited %s outside the retrieved set; %d sentence(s) stripped",
            request.strategy,
            token.symbol,
            bar.isoformat(),
            list(validation.rejected),
            len(validation.stripped),
        )

    explanation = Explanation(
        strategy=request.strategy,
        symbol=token.symbol,
        bar=bar,
        position=request.position,
        score=_finite(request.score),
        numeric_context=context,
        citations=citations,
        rationale=validation.rationale,
        model=narrator.model,
        generated_at=moment,
        news_status=news_status,
        news_message=news_message,
        dense_used=result.dense_used,
        retrieval_note=result.note,
        rejected_citations=validation.rejected,
        stripped_sentences=validation.stripped,
        chunks_embedded=chunks_embedded,
    )
    if embed_note:
        # The embed failure is the specific reason; "no embedder" would hide it.
        explanation = replace(explanation, retrieval_note=embed_note)
    statements = knowledge_statements_for(db.schema)
    db.execute(statements.upsert_explanation, explanation.to_row())
    return explanation


# --- reading what is stored --------------------------------------------------------------


def load_explanation(
    db: DatabaseManager, strategy: str, symbol: str, bar: datetime | None = None
) -> Explanation | None:
    """The stored explanation of one bar, or the latest stored for the pair.

    Read-only and free: this is what the API query and the UI show before
    anyone presses the button that runs the pipeline.
    """
    statements = knowledge_statements_for(db.schema)
    if bar is None:
        rows = db.fetch_all(statements.get_latest_explanation, {"strategy": strategy, "symbol": symbol})
    else:
        rows = db.fetch_all(
            statements.get_explanation,
            {
                "strategy": strategy,
                "symbol": symbol,
                "period_start_unix": int(bar.astimezone(UTC).timestamp()),
            },
        )
    return Explanation.from_row(rows[0]) if rows else None


def recent_explanations(
    db: DatabaseManager, symbol: str | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    """Summary rows of the newest explanations, optionally for one symbol."""
    statements = knowledge_statements_for(db.schema)
    return db.fetch_all(statements.get_recent_explanations, {"symbol": symbol, "limit": limit})


# --- the shared entry point for the API and the UI --------------------------------------------


def explain_symbol(
    db: DatabaseManager,
    settings: Settings,
    symbol: str,
    strategy: str = "bollinger",
    *,
    at: datetime | None = None,
    days: int | None = None,
    search: bool = False,
    reference_symbol: str | None = None,
    strategy_config: Any | None = None,
    registry: TokenRegistry | None = None,
    now: datetime | None = None,
) -> Explanation:
    """Run ``strategy`` over stored prices and explain one of its bars.

    What the GraphQL mutation and the UI button call; the CLI reaches the
    same :func:`explain` with its own flag-built strategy config.

    Args:
        search: Whether the metered news source may be called. **False by
            default**: a credit is spent only when a caller says so, and a
            caller that did not gets the stored corpus and a status that
            says ``skipped``.
        reference_symbol: Defaults to ``KNOWLEDGE_REFERENCE_SYMBOL``.

    Raises:
        ExplainError: an untracked symbol, no stored prices, or no bar at ``at``.
    """
    registry = registry or get_token_registry()
    token = registry.by_symbol(symbol)
    if token is None:
        raise ExplainError(f"unknown token {symbol!r}; tracked: {', '.join(registry.symbols)}")
    reference_symbol = reference_symbol or settings.knowledge_reference_symbol
    reference_token = registry.by_symbol(reference_symbol)
    wanted = [token.symbol]
    if reference_token is not None and reference_token.symbol != token.symbol:
        wanted.append(reference_token.symbol)

    window = days if days is not None else settings.analytics_lookback_days
    frame = load_price_frame(db, wanted, days=window, now=now)
    if frame.empty or token.symbol not in frame.columns:
        raise ExplainError(f"no stored price history for {token.symbol} in the last {window} days")
    prices = frame[token.symbol].dropna()
    prices.name = token.symbol
    reference = None
    if len(wanted) > 1 and wanted[1] in frame.columns:
        reference = frame[wanted[1]].dropna()
        reference.name = wanted[1]

    result = run_strategy(strategy, prices, strategy_config)
    request = request_for(result, token, at=at, reference=reference)
    source, embedder, _notes = services_for(settings)
    if not search:
        source = None
    return explain(db, settings, request, source=source, embedder=embedder, now=now)


def bar_of(result: StrategyResult, at: datetime | None = None) -> datetime:
    """The bar to explain: ``at`` when given and present, else the latest.

    Raises:
        ExplainError: the result has no bars, or ``at`` is not one of them.
    """
    index = pd.DatetimeIndex(result.frame.index)
    if len(index) == 0:
        raise ExplainError("the strategy produced no bars to explain")
    if at is None:
        return index[-1].to_pydatetime().astimezone(UTC)
    wanted = pd.Timestamp(at.astimezone(UTC))
    if wanted not in index:
        raise ExplainError(
            f"no {result.name} bar at {wanted.isoformat()}; the series runs "
            f"{index[0].isoformat()} to {index[-1].isoformat()}"
        )
    return wanted.to_pydatetime().astimezone(UTC)


def request_for(
    result: StrategyResult,
    token: Token,
    *,
    at: datetime | None = None,
    reference: pd.Series | None = None,
) -> ExplainRequest:
    """An :class:`ExplainRequest` for one bar of a strategy result."""
    bar = bar_of(result, at)
    at_bar = result.frame.loc[pd.Timestamp(bar)]
    # A duplicate index would hand back a frame; the first row is the bar.
    row = at_bar.iloc[0] if isinstance(at_bar, pd.DataFrame) else at_bar
    window = result.params.get("window")
    if not isinstance(window, int) or window < 2:
        window = 24
    return ExplainRequest(
        token=token,
        strategy=result.name,
        bar=bar,
        position=int(float(row["signal"])),
        score=_finite(row["score"]),
        prices=result.frame["price"].astype("float64"),
        window=window,
        reference=reference,
        params=dict(result.params),
    )


def services_for(settings: Settings) -> tuple[Source | None, Embedder | None, list[str]]:
    """The news source and embedder the configuration allows, plus notes.

    Neither is required: without a Tavily key there is no search, without a
    reachable embedder the lexical leg runs alone. Each absence is a note the
    CLI prints, never an error.
    """
    notes: list[str] = []
    source: Source | None
    try:
        source = build_tavily_source(settings)
    except NewsError:
        source = None
        notes.append("no TAVILY_API_KEY: no search; stored documents only")
    embedder: Embedder | None
    try:
        embedder = build_embedder(settings)
    except EmbeddingError as exc:
        embedder = None
        notes.append(f"no embedder ({exc}); lexical retrieval only")
    return source, embedder, notes


# --- rendering ---------------------------------------------------------------------------


def format_explanation(explanation: Explanation) -> str:
    """Render the explanation the way the CLI prints it."""
    score = f" score={explanation.score:+.2f}" if explanation.score is not None else ""
    lines = [
        f"{explanation.bar:%Y-%m-%d %H:%M} UTC  {POSITION_WORDS[explanation.position]}{score}"
        f"  ({explanation.strategy} on {explanation.symbol})"
    ]
    if explanation.rationale:
        lines.append(f"  {explanation.rationale}")
    else:
        lines.append("  (no rationale survived validation)")
    for c in explanation.citations:
        when = f"{c.published_at:%Y-%m-%d %H:%M} UTC" if c.published_at else "date unknown"
        lines.append(f"  [{c.n}] {c.url}   {when}   (retrieved {c.retrieved_at:%Y-%m-%d %H:%M})")
    if not explanation.citations:
        lines.append("  no documents published before the bar were retrieved")
    legs = "hybrid" if explanation.dense_used else "lexical only"
    embedded = f"; embedded {explanation.chunks_embedded} new chunks" if explanation.chunks_embedded else ""
    lines.append(
        f"  news: {explanation.news_status} — {explanation.news_message}{embedded}; retrieval: {legs}"
    )
    if explanation.retrieval_note:
        lines[-1] += f" ({explanation.retrieval_note})"
    if explanation.rejected_citations:
        lines.append(
            f"  validator: stripped {len(explanation.stripped_sentences)} sentence(s) citing "
            f"{list(explanation.rejected_citations)}, which retrieval never returned"
        )
    lines.append(f"  model: {explanation.model or 'template'}; stored as explanation row")
    return "\n".join(lines)
