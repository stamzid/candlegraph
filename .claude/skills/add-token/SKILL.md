---
name: add-token
description: Add a new token for candlegraph to ingest, and verify it actually produces candles. Use when asked to track, add, or support another token or trading pair.
---

# Add a token

Tokens are config, not code. Only `config/tokens.yaml` changes.

## 1. Find the address

The address must be the ERC-20 contract **as indexed by the Uniswap v3
subgraph**, lowercase. Confirm it exists and has data before editing anything:

```sh
source .env
curl -s -X POST \
  "https://gateway.thegraph.com/api/subgraphs/id/${SUBGRAPH_ID:-5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV}" \
  -H "Authorization: Bearer $GRAPH_API_KEY" -H 'content-type: application/json' \
  --data '{"query":"{ tokens(where:{id_in:[\"<ADDRESS>\"]}){ id name symbol decimals } }"}'
```

An empty `tokens` array means the address is wrong, or the token has no Uniswap
v3 presence — adding it would ingest nothing.

## 2. Edit the config

```yaml
  - address: "0x…"   # lowercase, 0x + 40 hex
    symbol: SYMBOL   # unique; this is what the API is queried by
    name: Full Name  # informational; real metadata comes from the subgraph
```

`candlegraph/tokens.py` validates on load and rejects a malformed address, a
duplicate address, or a duplicate symbol (case-insensitively).

## 3. Verify

```sh
make test-unit                          # config parses and validates
make up && sleep 25 && make logs        # expect "Ingested N hourly rows for SYMBOL"
curl -s -X POST localhost:8000/graphql -H 'content-type: application/json' \
  --data '{"query":"{ getChartData(tokenSymbol:\"SYMBOL\", timeUnitInHours:24){ tokenMetadata{ symbol decimals } candles{ time priceType value } } }"}'
```

A token with little liquidity may legitimately return fewer hours than the
others — hours with no swaps produce no row, and therefore no candle.

## 4. Update the tests

`tests/e2e/test_endpoint.py` pins the shipped token list. Update
`test_the_configured_tokens_are_served` and the `@pytest.mark.parametrize`
symbols, and `test_repo_tokens_file_loads_and_holds_three_tokens` in
`tests/unit/test_tokens.py`, or they will fail.

Do not touch the `tests/conftest.py` fixtures — those use their own two-token
file on purpose, so the unit tier does not depend on the shipped config.
