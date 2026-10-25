"""A thin, synchronous GraphQL client for the Uniswap v3 subgraph.

Talks to The Graph's decentralised gateway, which needs an API key. Queries use
GraphQL variables rather than string interpolation, and a page loop that cannot
make progress raises instead of spinning: a dead data source must surface as a
failure, not as an infinite retry at full speed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import backoff
import httpx

from candlegraph.settings import Settings, get_settings
from candlegraph.utils.logging_utils import service_logger

TOKEN_HOUR_DATAS_QUERY = """
query tokenHourDatas($token: String!, $since: Int!, $first: Int!) {
  tokenHourDatas(
    first: $first
    orderBy: periodStartUnix
    orderDirection: asc
    where: { token: $token, periodStartUnix_gt: $since }
  ) {
    id
    periodStartUnix
    open
    close
    high
    low
    priceUSD
  }
}
"""

TOKENS_QUERY = """
query tokens($ids: [String!]!) {
  tokens(where: { id_in: $ids }) {
    id
    name
    symbol
    totalSupply
    volumeUSD
    decimals
  }
}
"""

# Retrying a 4xx other than 429 just burns the rate limit; those are our bug.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class SubgraphError(RuntimeError):
    """The subgraph could not be queried, or answered with GraphQL errors."""


@dataclass(frozen=True)
class SubgraphPage:
    """One page of hourly rows, with the cursor to resume from."""

    rows: list[dict[str, Any]]
    last_timestamp: int


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS
    return isinstance(exc, httpx.TransportError)


class SubgraphClient:
    """Issues the two queries the ingester needs.

    The client owns an ``httpx.Client``; use it as a context manager, or call
    :meth:`close`, so the connection pool is released.
    """

    def __init__(self, settings: Settings | None = None, client: httpx.Client | None = None) -> None:
        self.settings = settings or get_settings()
        if self.settings.transport_url is None and self.settings.graph_api_key is None:
            raise SubgraphError(
                "No subgraph endpoint configured: set GRAPH_API_KEY for the gateway "
                "(https://thegraph.com/studio/apikeys/) or TRANSPORT_URL for an explicit endpoint."
            )
        self.url = self.settings.subgraph_url
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=self.settings.subgraph_timeout_seconds,
            headers=self.settings.subgraph_headers,
        )

    def __enter__(self) -> SubgraphClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP client if this instance created it."""
        if self._owns_client:
            self._client.close()

    # --- transport ---------------------------------------------------------

    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """POST a query and return its ``data`` object.

        Retries transport errors and retryable status codes with exponential
        backoff, then gives up by raising :class:`SubgraphError`.
        """
        try:
            payload = self._post(query, variables)
        except httpx.HTTPError as exc:
            raise SubgraphError(f"subgraph request to {self.url} failed: {exc}") from exc

        if errors := payload.get("errors"):
            raise SubgraphError(f"subgraph returned errors: {errors}")

        data = payload.get("data")
        if not isinstance(data, dict):
            raise SubgraphError(f"subgraph response has no data object: {payload!r}")
        return data

    def _post(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        # max_tries is a runtime setting, so the decorator is applied per call.
        @backoff.on_exception(
            backoff.expo,
            httpx.HTTPError,
            max_tries=self.settings.subgraph_max_tries,
            giveup=lambda exc: not _is_retryable(exc),
            logger=service_logger,
        )
        def _send() -> dict[str, Any]:
            response = self._client.post(
                self.url,
                json={"query": query, "variables": variables},
                headers=self.settings.subgraph_headers,
            )
            response.raise_for_status()
            body: dict[str, Any] = response.json()
            return body

        return _send()

    # --- queries -----------------------------------------------------------

    def fetch_token_hour_page(self, token_id: str, since: int) -> SubgraphPage:
        """One page of hourly rows for ``token_id`` strictly after ``since``."""
        data = self.execute(
            TOKEN_HOUR_DATAS_QUERY,
            {"token": token_id, "since": int(since), "first": self.settings.subgraph_page_size},
        )
        rows = data.get("tokenHourDatas") or []
        if not isinstance(rows, list):
            raise SubgraphError(f"tokenHourDatas was {type(rows).__name__}, expected a list")

        last = since
        for row in rows:
            try:
                last = max(last, int(row["periodStartUnix"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise SubgraphError(f"row missing a usable periodStartUnix: {row!r}") from exc
        return SubgraphPage(rows=rows, last_timestamp=last)

    def iter_token_hour_datas(self, token_id: str, since: int) -> Any:
        """Yield pages for ``token_id`` until the subgraph has nothing newer.

        Raises:
            SubgraphError: a page returned rows but did not advance the cursor
                (which would loop forever), or ``SUBGRAPH_MAX_PAGES`` was hit.
        """
        cursor = int(since)
        for page_number in range(self.settings.subgraph_max_pages):
            page = self.fetch_token_hour_page(token_id, cursor)
            if not page.rows:
                return
            if page.last_timestamp <= cursor:
                raise SubgraphError(
                    f"{token_id}: page {page_number} returned {len(page.rows)} rows but the cursor "
                    f"did not advance past {cursor}; refusing to loop"
                )
            cursor = page.last_timestamp
            yield page
            if len(page.rows) < self.settings.subgraph_page_size:
                return
        raise SubgraphError(
            f"{token_id}: stopped after SUBGRAPH_MAX_PAGES={self.settings.subgraph_max_pages} pages"
        )

    def fetch_tokens(self, token_ids: list[str]) -> list[dict[str, Any]]:
        """Metadata for the given token addresses."""
        if not token_ids:
            return []
        data = self.execute(TOKENS_QUERY, {"ids": list(token_ids)})
        rows = data.get("tokens") or []
        if not isinstance(rows, list):
            raise SubgraphError(f"tokens was {type(rows).__name__}, expected a list")
        return rows
