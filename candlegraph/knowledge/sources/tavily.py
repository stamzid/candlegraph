"""Tavily: web search as a metered source. The typed REST client.

Shaped like :mod:`candlegraph.ingestion.subgraph`, which has survived three
stages: an ``httpx.Client`` with an explicit timeout, exponential backoff on
transport errors and 5xx up to a retry budget, and typed errors for
everything else. Two rules from the design are load-bearing:

* **429 and 432 are never retried.** They mean the rate limit or the plan
  limit; retrying them is how a budget gets spent on failures. They raise
  :class:`TavilyRateLimitError`, which the pipeline records as a
  ``rate_limited`` ledger row and stops the run.
* **The client never writes and never decides.** It turns one request into
  ``RawDocument``s; the budget, the cache and the ledger live around it in
  :mod:`candlegraph.knowledge.news`.

A result without a ``published_date`` becomes ``published_at = NULL`` —
never "now" — because the no-look-ahead rule reads that column.
"""

from __future__ import annotations

import email.utils
from datetime import UTC, datetime
from typing import Any

import backoff
import httpx

from candlegraph.knowledge.documents import RawDocument
from candlegraph.knowledge.sources.base import SearchResult

SOURCE_ID = "tavily"
BASE_URL = "https://api.tavily.com"

# Credits per search, by depth, as the pricing page stated on 2026-11-01.
# Re-check before relying on them; the budget guard assumes only "scarce".
SEARCH_COST = {"basic": 1, "advanced": 2}

# Retrying any other 4xx burns the rate limit on our own bug; 429/432 are
# the limits themselves and retrying them spends budget on failures.
RETRYABLE_STATUS = frozenset({408, 500, 502, 503, 504})
RATE_LIMIT_STATUS = frozenset({429, 432})


class TavilyError(RuntimeError):
    """The search failed for a reason that is not a rate limit."""


class TavilyRateLimitError(TavilyError):
    """HTTP 429/432: the per-minute or plan limit. Recorded, never retried."""


def parse_published(value: Any) -> datetime | None:
    """Tavily's ``published_date`` in either of its shapes, else ``None``.

    News results carry RFC 2822 dates ("Fri, 24 Oct 2026 09:12:34 GMT");
    some carry ISO 8601. Anything unparseable is an unknown date, and an
    unknown date stays ``NULL`` (invariant 11 / the no-look-ahead rule).
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class TavilySource:
    """The ``Source`` implementation that spends credits."""

    def __init__(
        self,
        api_key: str,
        search_depth: str = "basic",
        timeout: float = 30.0,
        max_tries: int = 3,
        base_url: str = BASE_URL,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if search_depth not in SEARCH_COST:
            raise TavilyError(
                f"unknown TAVILY_SEARCH_DEPTH {search_depth!r}; expected one of {sorted(SEARCH_COST)}"
            )
        self._depth = search_depth
        self._max_tries = max(1, max_tries)
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport,
            # The key travels only in this header. Errors quote response
            # bodies, never requests, so it cannot leak through a message.
            headers={"Authorization": f"Bearer {api_key}"},
        )

    @property
    def id(self) -> str:
        return SOURCE_ID

    @property
    def kind(self) -> str:
        return "search"

    @property
    def cost(self) -> int:
        return SEARCH_COST[self._depth]

    def search(self, query: str, *, start: datetime, end: datetime, max_results: int) -> SearchResult:
        """One news search inside ``[start, end]``, rounded out to whole days.

        Raises:
            TavilyRateLimitError: HTTP 429/432, after zero retries.
            TavilyError: transport failure or 5xx after the retry budget,
                or any other non-200 answer.
        """
        payload: dict[str, Any] = {
            "query": query,
            "topic": "news",
            "search_depth": self._depth,
            "max_results": max_results,
            # The article body comes back with the result, so no separate
            # (metered) extract call is needed.
            "include_raw_content": True,
            "start_date": start.astimezone(UTC).date().isoformat(),
            "end_date": end.astimezone(UTC).date().isoformat(),
        }
        response = self._post(payload)
        if response.status_code in RATE_LIMIT_STATUS:
            raise TavilyRateLimitError(f"Tavily returned HTTP {response.status_code}: rate or plan limit")
        if response.status_code != 200:
            raise TavilyError(f"Tavily returned HTTP {response.status_code}: {response.text[:200]}")

        results = response.json().get("results") or []
        documents: list[RawDocument] = []
        skipped = 0
        for item in results:
            url = str(item.get("url") or "").strip()
            text = str(item.get("raw_content") or item.get("content") or "").strip()
            if not url or not text:
                skipped += 1
                continue
            documents.append(
                RawDocument(
                    source_id=self.id,
                    url=url,
                    raw_text=text,
                    title=str(item["title"]) if item.get("title") else None,
                    published_at=parse_published(item.get("published_date")),
                    meta={"score": item.get("score"), "query": query},
                )
            )
        return SearchResult(
            documents=documents,
            credits=self.cost,
            results=len(results),
            skipped=skipped,
            params=payload,
        )

    def _post(self, payload: dict[str, Any]) -> httpx.Response:
        """POST with backoff on transport errors and 5xx only.

        A 429/432 response is returned to the caller un-retried; backoff sees
        only exceptions, and those statuses never become one here.
        """

        @backoff.on_exception(
            backoff.expo,
            httpx.TransportError,
            max_tries=self._max_tries,
            logger=None,
        )
        def attempt() -> httpx.Response:
            response = self._client.post(f"{self._base_url}/search", json=payload)
            if response.status_code in RETRYABLE_STATUS:
                # Raise through the transport-error channel so backoff counts it.
                raise httpx.TransportError(f"HTTP {response.status_code} from Tavily")
            return response

        try:
            return attempt()
        except httpx.TransportError as exc:
            raise TavilyError(f"Tavily unreachable after {self._max_tries} tries: {exc}") from exc
