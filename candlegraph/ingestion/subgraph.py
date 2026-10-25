"""A thin, synchronous GraphQL client for the Uniswap v3 subgraph.

Talks to The Graph's decentralised gateway, which needs an API key. Queries use
GraphQL variables rather than string interpolation, and a page loop that cannot
make progress raises instead of spinning: a dead data source must surface as a
failure, not as an infinite retry at full speed.
"""

from __future__ import annotations

from collections.abc import Iterator
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
    volume
    volumeUSD
    untrackedVolumeUSD
    totalValueLocked
    totalValueLockedUSD
    feesUSD
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
    volume
    feesUSD
    txCount
    poolCount
    totalValueLocked
    totalValueLockedUSD
    derivedETH
  }
}
"""

# Pools are discovered from either side of the pair, ranked by TVL: the deepest
# pool for a token is the one whose prices and volumes are worth storing.
#
# One token per query, deliberately. Passing every token to a single
# `token0_in` with `first: N` returns the N deepest pools across the whole
# set, so one liquid token starves all the others of pools entirely.
POOLS_FOR_TOKEN_QUERY = """
query poolsForToken($id: String!, $first: Int!) {
  asToken0: pools(
    first: $first
    orderBy: totalValueLockedUSD
    orderDirection: desc
    where: { token0: $id }
  ) { ...poolFields }
  asToken1: pools(
    first: $first
    orderBy: totalValueLockedUSD
    orderDirection: desc
    where: { token1: $id }
  ) { ...poolFields }
}

fragment poolFields on Pool {
  id
  feeTier
  createdAtTimestamp
  liquidity
  volumeUSD
  feesUSD
  txCount
  totalValueLockedUSD
  token0 { id symbol }
  token1 { id symbol }
}
"""

# Transaction counts exist only at pool level in this subgraph, so this is the
# sole source for them.
POOL_HOUR_DATAS_QUERY = """
query poolHourDatas($pool: String!, $since: Int!, $first: Int!) {
  poolHourDatas(
    first: $first
    orderBy: periodStartUnix
    orderDirection: asc
    where: { pool: $pool, periodStartUnix_gt: $since }
  ) {
    id
    periodStartUnix
    open
    high
    low
    close
    liquidity
    sqrtPrice
    token0Price
    token1Price
    tvlUSD
    volumeToken0
    volumeToken1
    volumeUSD
    feesUSD
    txCount
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

    def _rows(self, data: dict[str, Any], key: str) -> list[dict[str, Any]]:
        rows = data.get(key) or []
        if not isinstance(rows, list):
            raise SubgraphError(f"{key} was {type(rows).__name__}, expected a list")
        return rows

    def _hour_page(
        self, query: str, result_key: str, id_variable: str, entity_id: str, since: int
    ) -> SubgraphPage:
        """One page of any ``*HourData`` entity, strictly after ``since``."""
        data = self.execute(
            query,
            {id_variable: entity_id, "since": int(since), "first": self.settings.subgraph_page_size},
        )
        rows = self._rows(data, result_key)

        last = int(since)
        for row in rows:
            try:
                last = max(last, int(row["periodStartUnix"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise SubgraphError(f"row missing a usable periodStartUnix: {row!r}") from exc
        return SubgraphPage(rows=rows, last_timestamp=last)

    def _iter_hour_pages(
        self, query: str, result_key: str, id_variable: str, entity_id: str, since: int
    ) -> Iterator[SubgraphPage]:
        """Page through an hourly entity until the subgraph has nothing newer.

        Shared by tokens and pools so the cursor-safety rule is written once.

        Raises:
            SubgraphError: a page returned rows but did not advance the cursor
                (which would loop forever), or ``SUBGRAPH_MAX_PAGES`` was hit.
        """
        cursor = int(since)
        for page_number in range(self.settings.subgraph_max_pages):
            page = self._hour_page(query, result_key, id_variable, entity_id, cursor)
            if not page.rows:
                return
            if page.last_timestamp <= cursor:
                raise SubgraphError(
                    f"{entity_id}: page {page_number} returned {len(page.rows)} rows but the cursor "
                    f"did not advance past {cursor}; refusing to loop"
                )
            cursor = page.last_timestamp
            yield page
            if len(page.rows) < self.settings.subgraph_page_size:
                return
        raise SubgraphError(
            f"{entity_id}: stopped after SUBGRAPH_MAX_PAGES={self.settings.subgraph_max_pages} pages"
        )

    def fetch_token_hour_page(self, token_id: str, since: int) -> SubgraphPage:
        """One page of hourly rows for ``token_id`` strictly after ``since``."""
        return self._hour_page(TOKEN_HOUR_DATAS_QUERY, "tokenHourDatas", "token", token_id, since)

    def iter_token_hour_datas(self, token_id: str, since: int) -> Iterator[SubgraphPage]:
        """Yield hourly token pages until the subgraph has nothing newer."""
        yield from self._iter_hour_pages(TOKEN_HOUR_DATAS_QUERY, "tokenHourDatas", "token", token_id, since)

    def fetch_pool_hour_page(self, pool_id: str, since: int) -> SubgraphPage:
        """One page of hourly rows for ``pool_id`` strictly after ``since``."""
        return self._hour_page(POOL_HOUR_DATAS_QUERY, "poolHourDatas", "pool", pool_id, since)

    def iter_pool_hour_datas(self, pool_id: str, since: int) -> Iterator[SubgraphPage]:
        """Yield hourly pool pages until the subgraph has nothing newer."""
        yield from self._iter_hour_pages(POOL_HOUR_DATAS_QUERY, "poolHourDatas", "pool", pool_id, since)

    def fetch_tokens(self, token_ids: list[str]) -> list[dict[str, Any]]:
        """Metadata for the given token addresses."""
        if not token_ids:
            return []
        return self._rows(self.execute(TOKENS_QUERY, {"ids": list(token_ids)}), "tokens")

    def fetch_pools_for_token(self, token_id: str, first: int) -> list[dict[str, Any]]:
        """The ``first`` deepest pools holding ``token_id``, on either side.

        Both sides are queried together and merged here, de-duplicated by pool
        id, because a pool can match both halves of the query.
        """
        if first <= 0:
            return []
        data = self.execute(POOLS_FOR_TOKEN_QUERY, {"id": token_id, "first": int(first)})

        merged: dict[str, dict[str, Any]] = {}
        for key in ("asToken0", "asToken1"):
            for pool in self._rows(data, key):
                pool_id = pool.get("id")
                if isinstance(pool_id, str):
                    merged[pool_id.lower()] = pool
        return list(merged.values())

    def fetch_pools_for_tokens(self, token_ids: list[str], first: int) -> list[dict[str, Any]]:
        """The ``first`` deepest pools for *each* token, merged and deduplicated.

        Queried one token at a time so a highly liquid token cannot crowd the
        others out of the ranking.
        """
        if not token_ids or first <= 0:
            return []
        merged: dict[str, dict[str, Any]] = {}
        for token_id in token_ids:
            for pool in self.fetch_pools_for_token(token_id, first):
                merged[str(pool["id"]).lower()] = pool
        return list(merged.values())
