---
name: extend-knowledge-graph
description: Build or extend candlegraph's knowledge layer (stage 1) — the pgvector-backed document store, Tavily source, embeddings, signal explanations, and the MCP server — in the order the design requires. Use when asked to implement the knowledge graph, add a source or entity type, change the embedding model, explain signals, or expose tools over MCP.
---

# Extend the knowledge graph

The layer is **partly built**: the schema, the asset sync, the document
pipeline, embeddings, and the Tavily source with its budget guard and
ledger (rollout steps 1–6) exist; retrieval onwards does not. Read
`docs/knowledge/graph/DESIGN.md` first and the four companions as you reach
them. Every step below exists
because of a decision recorded there; do not reverse one in code without
updating the document and its Status line.

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

## 1. Build order

**`docs/rollout/ROLLOUT.md` is the authoritative sequence** — it carries
the progress table, per-step entry/exit criteria, and which steps may be
combined in one session. Update its table when a step lands. The summary
below maps the same work; if the two ever disagree, the rollout document
wins and this summary gets fixed.

| Step | Lands | Proven by |
|---|---|---|
| a | image switch, `CREATE EXTENSION vector`, the tables, `asset` synced from `tokens.yaml` aliases | integration: extension present, tables plain, aliases loaded |
| b | `documents.py`: URL normalisation, `url_hash`, chunking | unit: pure functions, exhaustive |
| c | `embed.py`: `Embedder` Protocol, local provider (Ollama or sentence-transformers), `halfvec(1024)` writes, `embedding_model` stamped | unit with a tiny fixed model; integration: HNSW index used (`EXPLAIN`) |
| d | `sources/tavily.py` + `budget.py` + `source_call` | unit: `httpx.MockTransport` fixtures, budget decisions at the boundaries (79/80/100%), refusals recorded |
| e | `retrieve.py`: hybrid ANN + `tsvector`, RRF, `published_at ≤ bar` | integration on a seeded corpus; the no-look-ahead test |
| f | `explain.py` + `validate.py` + `explanation` table; `signals --explain` | the exit criterion; the invented-citation rejection test |
| g | observer `collect_knowledge` node and four findings | `add-agent` skill rules: pure assess, graph shape test updated |
| h | `candlegraph mcp` (optional extra) | tools listed; `data_quality_report` matches `report --json` |

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

## 6. Finish

`docs/knowledge/*/DESIGN.md` Status lines → `implemented` as each lands;
`docs/README.md`, `AGENTS.md` (commands, layout, data model, invariants),
`README.md`, `.env.example`; then `make check` and the `release-check`
skill.
