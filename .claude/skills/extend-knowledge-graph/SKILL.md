---
name: extend-knowledge-graph
description: Build or extend candlegraph's knowledge layer (stage 1) — the pgvector-backed document store, Tavily source, embeddings, signal explanations, and the MCP server — in the order the design requires. Use when asked to implement the knowledge graph, add a source or entity type, change the embedding model, explain signals, or expose tools over MCP.
---

# Extend the knowledge graph

The layer is **built** (stage 1, delivered 2026-11-06): the schema, the
asset sync, the document pipeline, embeddings, the Tavily source with its
budget guard and ledger, alias entity linking, hybrid retrieval,
`signals --explain` with its citation validator, the observer's knowledge
findings, and the read-only MCP server. Read
`docs/knowledge/graph/DESIGN.md` first and the four companions as you reach
them. Every rule below exists
because of a decision recorded there; do not reverse one in code without
updating the document and its Status line.

Two pieces were deliberately left unbuilt and are recorded as such: the
embeddings evaluation set (`knowledge eval`, in
`docs/knowledge/embeddings/DESIGN.md`) and the janitor's optional
incident-similarity finding (in `docs/knowledge/graph/DESIGN.md`).

## 0. Before the first line of code

- `docs/knowledge/vector-store/DESIGN.md` — pgvector is not in the Alpine
  TimescaleDB image. Stage 1 starts with a pinned switch to
  `timescale/timescaledb-ha` in **compose, CI and the testcontainer** at
  once (`test_the_database_image_is_pinned_identically_everywhere` holds
  you to it), a preflight check that the `vector` extension is available
  (copy the TimescaleDB one in `alembic/env.py`), and a **rebuilt volume**.
  Read `docs/incidents/2026-10-28-stack-would-not-start/` first; it is the
  same class of change.
- `docs/storage/table-placement/DESIGN.md` already records the knowledge
  tables as plain. The migration follows the `add-migration` skill; the
  tables need no `period_start_unix` in their keys because they are not
  hypertables.

## 1. How it was built, and what proves each part

The sequence and the per-step outcomes are in
`docs/rollout/archive/2026-11-06-stage-1-knowledge-graph.md`. Read the row
for the part you are changing: it says what landed, what was tested, and
what was deliberately left out.

| Step | Lands | Proven by |
|---|---|---|
| a | image switch, `CREATE EXTENSION vector`, the tables, `asset` synced from `tokens.yaml` aliases | integration: extension present, tables plain, aliases loaded |
| b | `documents.py`: URL normalisation, `url_hash`, chunking | unit: pure functions, exhaustive |
| c | `embed.py`: `Embedder` Protocol, local provider (Ollama or sentence-transformers), `halfvec(1024)` writes, `embedding_model` stamped | unit with a tiny fixed model; integration: HNSW index used (`EXPLAIN`) |
| d | `sources/tavily.py` + `budget.py` + `source_call` | unit: `httpx.MockTransport` fixtures, budget decisions at the boundaries (79/80/100%), refusals recorded |
| e | `retrieve.py`: hybrid ANN + `tsvector`, RRF, `published_at ≤ bar` | integration on a seeded corpus; the no-look-ahead test |
| f | `explain.py` + `validate.py` + `explanation` table; `signals --explain` | the exit criterion; the invented-citation rejection test |
| g | observer `collect_knowledge` node and four findings | `add-agent` skill rules: pure assess, graph shape test updated |
| h | `candlegraph mcp` (optional extra) | tools listed over stdio; `data_quality_report` matches `report --json`; no listed tool writes market data |

## 1b. Adding a tool to the MCP server

`candlegraph/mcp/tools.py`, and nowhere else. A tool is a plain function
whose **signature and docstring are its JSON schema**: no `db` or
`settings` parameter (resolve them inside, as the Streamlit script does),
units spelled out, and a note wherever a number is easy to misread. Put
the `@boundary` decorator on it — that redacts the result and every
escaping error — return a JSON-able dict built in `payloads.py` (where
`NaN` becomes `null`), and add a `ToolSpec` to `TOOLS` saying truthfully
whether it writes and what. A read-only tool is then checked by
`test_no_read_only_tool_writes_anything`, which is driven off that
registry, so skipping the entry fails the build rather than slipping
through.

Two rules from `docs/knowledge/mcp/DESIGN.md` do not bend: a tool must
wrap something that already has a CLI subcommand behind it — if it does
not, the capability is missing from the CLI and that is the bug — and no
tool writes market data.

## 2. Adding a source

A source is a `Source` Protocol implementation in
`candlegraph/knowledge/sources/` — `base.py` has the Protocol, `tavily.py`
the worked example. It returns `RawDocument`s (source id, URL, text,
optional title, `published_at` only when the source states it) and **never
writes, never decides**: run it through `news.fetch_news`, which owns the
`(asset, UTC day)` cache, the `budget.decide` guard and the `source_call`
row for every call, refusals included. A metered source states its `cost`
property; a free one costs 0 but still gets ledger rows, because a rate
limit is a budget too, and a rate-limited call halts the run rather than
spending more on the same failure. Tests use `httpx.MockTransport` with
recorded responses (invariant 2) and a scripted fake source for the
pipeline (`tests/unit/knowledge/test_news.py` is the pattern).

## 3. Adding an entity type or alias

Aliases live in `config/tokens.yaml` next to the symbol and are loaded into
`asset.aliases` by `candlegraph knowledge sync-assets`. A new *kind* of
entity (an index, an exchange) is a row in `asset` with a new `kind` value
and a line in the graph document's schema table; it is not a new table.
Edges are derived from stored data (`pool`, `pairwise`), never asserted by
a model.

## 3b. Retrieval and its bound

Every retrieval goes through `retrieve.retrieve(db, query, bar=…)`. The
bar is not optional: `published_at <= bar` is the no-look-ahead rule, and
the integration test that appends a later document and asserts the bar is
unchanged is the one the stage leans on — never weaken it. Use
`mode="strategy"` for anything a backtest may read (adds
`retrieved_at <= bar`, never returns undated text); `display` may show
undated chunks only when asked, flagged. Pass `terms=` (aliases, a ticker)
for the lexical leg and `asset_id=` to filter by alias mentions — the
filter only works for documents that have been linked, which the news
pipeline does as it stores and `make link-mentions` backfills. A second
backend is a `RETRIEVAL_BACKEND` value and the same `retrieve()` signature;
`docs/knowledge/ragflow/DESIGN.md` records why RAGFlow is not one yet.

## 4. Changing the embedding model

Read `docs/knowledge/embeddings/DESIGN.md`. The column is 1024 dimensions;
a model must produce 1024 or support truncation to it
(`EMBEDDING_TRUNCATE=true`, Matryoshka models only). Change
`EMBEDDING_MODEL` (the `ollama` service pulls whatever the variable names
on its next start), then `make reembed` (resumable; it skips rows already
stamped with the new name, and `make embed` prints the backlog first). If
the new family needs a query/document prefix, add it to
`embed._FAMILY_PREFIXES` with a test — bge-m3 and Qwen3 need none, nomic
and e5 do. The observer reports rows embedded with any other model as a
backlog until it finishes (step 9). Record the evaluation-set result in the
document when you measure it. To develop without a model server, set
`EMBEDDING_PROVIDER=fake`.

## 4b. Explaining a signal

`signals SYMBOL --explain` is `knowledge/explain.py`. The order is fixed:
news for the `(asset, UTC day)` through `news.fetch_news(…, bar=bar)` (the
cache key and search window follow the bar; the ledger row and
`retrieved_at` follow the clock), retrieval as of the bar filtered to the
asset, the pure `numeric_context` (bars at or before the bar only; a
missing figure is `None`), the narrator, `validate.validate_rationale`,
then the `explanation` upsert. Add a number by extending
`numeric_context` and its tests, not the narrator; add prose by changing
the template or the prompt, never by letting the narrator see the
database. The CLI prints the news and retrieval status every time so a
cache hit, a refusal, a missing key and a lexical-only retrieval are all
visible. Tests: `tests/unit/knowledge/test_explain.py` runs the pipeline on
the replaying fake database from `test_news.py`; the integration tests seed
prices and a corpus and prove the exit criterion and the no-look-ahead rule.

## 5. The rules that do not bend

- **Numbers are computed; prose is optional.** The validator rejects a
  citation id outside the retrieved set. With no LLM key configured the
  command still prints the signal, the numeric context and the citations.
- **Nothing looks ahead.** `published_at ≤ bar` for display; additionally
  `retrieved_at ≤ bar` for anything a strategy consumes. A test appends a
  later document and asserts the earlier bar's explanation is unchanged.
- **One search per `(asset, UTC day)`.** The cache is in the database, not
  in memory. A test asserts two explanations on the same day make one
  network call.
- **Agents never write market data.** The observer reads the knowledge
  tables; only `explain` and the source pipeline write them.
- **Secrets stay in `.env`.** `TAVILY_API_KEY` is a `SecretStr`, redacted
  before it can reach a report, and `.env.example` documents it empty.

- **No tool writes market data either.** The MCP surface inherits the same
  rule (invariant 21): `observe` writes an `agent_run` row,
  `explain_signal` writes an `explanation`, and nothing else writes at all.

## 6. Finish

Keep `docs/knowledge/*/DESIGN.md` Status lines truthful as things change;
update `docs/README.md`, `AGENTS.md` (commands, layout, data model,
invariants), `README.md` and `.env.example` in the same change; then
`make check` and the `release-check` skill. Anything touching the MCP
surface needs `uv sync --extra mcp` locally, or its tests skip rather than
fail — check the skip count before reading a green run as proof.
