# Rollout: stage 1 — the knowledge graph on pgvector

**Status:** current — 2026-10-29; step 1 done, step 2 next. This directory holds **one
current rollout at a time**; this document is it, and each working session
should update the progress table below. When the stage ships, this file
moves to `docs/rollout/archive/<date>-<slug>.md` and the next rollout
replaces it.
**Designs this executes:** the five documents under `docs/knowledge/` and
[`../storage/pgvector-coexistence/DESIGN.md`](../storage/pgvector-coexistence/DESIGN.md).
Decisions live there; this file only sequences them. If a step forces a
design change, change the design document in the same step and keep its
Status line truthful.
**Operating procedure per step:** the `extend-knowledge-graph` skill, plus
`add-migration` for step 2 and `add-agent` rules for step 9.

## Progress

| # | Step | Size | Depends on | Status |
|---|---|---|---|---|
| 1 | Database image switch to `timescaledb-ha` | S, risky | — | **done** 2026-10-29 |
| 2 | Knowledge schema migration + statements | M | 1 | not started |
| 3 | Assets and aliases from `tokens.yaml` | S | 2 | not started |
| 4 | Documents: normalise, hash, chunk | S | 2 | not started |
| 5 | Embeddings: Protocol, local provider, HNSW proven | M | 2, 4 | not started |
| 6 | Tavily source, budget guard, `source_call` ledger | M | 2 | not started |
| 7 | Hybrid retrieval with the no-look-ahead bound | M | 3, 4, 5 | not started |
| 8 | `signals --explain`: explain, validate, cache | L | 6, 7 | not started |
| 9 | Observer: `collect_knowledge` + four findings | S | 6, 8 | not started |
| 10 | MCP server (optional extra) + stage closeout | M | 8 | not started |

**Combining steps in one session:** 3+4 combine naturally (both small and
pure); 5 can absorb 4 if the session is long; 6 is independent of 3–5 and
can run in parallel with any of them; 9 and 10 are each their own session.
**Do not combine 1 with anything** — it is the one infrastructure change,
it rebuilds volumes, and it should be verified alone, exactly because of
`docs/incidents/2026-10-28-stack-would-not-start/`.

The exit criterion for the whole stage (roadmap): `candlegraph signals WETH
--strategy bollinger --explain` prints a rationale citing a retrieved
document with a URL and a timestamp, stored with the signal — and it does
so with no LLM key configured, prose-free.

---

## Step 1 — Database image switch

**Goal.** The stack, CI and the testcontainer run
`timescale/timescaledb-ha` (Debian; bundles pgvector and pgvectorscale),
pinned to one exact tag, with everything that passes today still passing.
No schema change, no new code.

**Read first.** `docs/storage/pgvector-coexistence/DESIGN.md` (the
operational table), `docs/incidents/2026-10-28-stack-would-not-start/`.

**Touches.** `docker-compose.yaml`, `.github/workflows/ci.yml`,
`tests/integration/conftest.py` (`POSTGRES_IMAGE`), README's storage
section, `docs/storage/timescaledb/DESIGN.md` (image paragraph).

**Watch for.** Pick the exact `-ha` tag carrying TimescaleDB 2.31 and pin
it in all three places in one commit —
`test_the_database_image_is_pinned_identically_everywhere` holds you to
it. The `-ha` image runs as user `postgres` (uid 1000) with `PGDATA` at
`/home/postgres/pgdata/data`, not `/var/lib/postgresql/data`; the volume
mount moves with it (`test_the_volume_is_mounted_where_the_ha_image_keeps_pgdata`).
Keep the `-c shared_preload_libraries=timescaledb` command line (invariant
25's sibling); pgvector needs no preload entry.

**Done when.** `make test-integration` is green on the new image;
`COMPOSE_PROJECT_NAME=<unique> make up` from a **fresh volume** is healthy;
`SELECT name FROM pg_available_extensions WHERE name='vector'` returns a
row in the stack; old local volumes documented as rebuild-only (README
already says so for the musl→glibc reason).

**Outcome (2026-10-29).** Done, in this order: Docker cleaned to zero
images and no candlegraph volumes; baseline `make check` on the old image
**806 passed, 0 unexpected skips**; the compose robustness fixes landed
first with their own post-mortem
(`docs/incidents/2026-10-29-compose-startup-robustness/`) and were proven on
the old image — fresh `make up` 45 s with an empty build cache, a poisoned
volume now fails fast (container exited after 5 attempts, not looping) with
the preflight's message printed by `make up`, `make reset` recovers; then
the switch to `timescale/timescaledb-ha:pg17.11-ts2.31.0` in compose, CI
and the testcontainer, volume moved to `/home/postgres/pgdata/data`.
Verified on the new image: `make check` **817 passed, 0 unexpected skips**
including a new full-chain round trip (`downgrade base` → `upgrade head`
on populated tables); stack healthy in 17 s; PostgreSQL 17.11, TimescaleDB
2.31.0 preloaded, pgvector 0.8.7 and pgvectorscale 0.9.1 available and not
installed; three hypertables; `alembic heads` = `alembic current` = one
head; database collation `C.UTF-8`; end-to-end, strict report and observer
as before. A sixth robustness fault (four services building one image tag
raced in buildx) surfaced during verification and is fixed and tested.
Step 2 can assume `vector` is available and installs it.

## Step 2 — Knowledge schema migration and statements

**Goal.** The eight corpus tables exist, plain, with `CREATE EXTENSION
vector`, a working `downgrade`, and every statement named.

**Read first.** The schema section of `docs/knowledge/graph/DESIGN.md`;
the placement table already recorded in
`docs/storage/table-placement/DESIGN.md`; the `add-migration` skill.

**Touches.** One Alembic revision (`asset`, `source`, `document`, `chunk`,
`mention`, `edge`, `explanation`, `source_call`; `halfvec(1024)`; HNSW on
`chunk.embedding`, cosine; generated `tsvector` + GIN; unique `url_hash`);
`candlegraph/knowledge/statements.py` (same pattern as
`storage/statements.py`); a `_require_extension` in the revision that
refuses a server without `vector` available, message naming the image.

**Done when.** Integration: extension present, all eight tables absent
from `timescaledb_information.hypertables`
(`test_the_ledger_and_state_tables_are_not_hypertables` extended),
migration round-trips on a populated database (copy the
`test_the_timescale_migration_round_trips` pattern); `make check` green.

## Step 3 — Assets and aliases

**Goal.** `config/tokens.yaml` gains an `aliases` list per token;
`candlegraph knowledge sync-assets` upserts `asset` rows from it.

**Touches.** `config/tokens.yaml`, `candlegraph/tokens.py` (validate
aliases: non-empty strings, unique case-insensitively per file),
`candlegraph/knowledge/assets.py` (pure transform + one upsert), CLI
subcommand, Makefile target, AGENTS.md commands table.

**Done when.** Unit tests on validation and the transform; integration:
sync is idempotent (second run changes nothing); a token with no aliases
still gets an `asset` row keyed by address with `[symbol, name]` as the
default alias set.

## Step 4 — Documents: normalise, hash, chunk

**Goal.** `knowledge/documents.py`: URL normalisation → `url_hash`,
chunking at 300–500 tokens with 50 overlap on paragraph/sentence
boundaries, and the `document`/`chunk` upserts. All transforms pure.

**Done when.** Exhaustive unit tests: tracking parameters stripped, host
case folded, two phrasings of one URL hash equal; chunk boundaries never
mid-sentence; ordinals stable; `published_at` absent stays `NULL`
(invariant 11's shape). Integration: upserting the same document twice
stores it once.

## Step 5 — Embeddings

**Goal.** The `Embedder` Protocol (`name`, `dimensions`,
`embed(texts, kind=document|query)`), the local default provider, writes
into `halfvec(1024)` with `embedding_model` stamped, and
`candlegraph knowledge reembed` (resumable).

**Read first.** `docs/knowledge/embeddings/DESIGN.md` — the decision is
made; this step implements it. Prefer the Ollama compose service over
in-process `sentence-transformers` so the app image stays small; either
way the provider is behind the Protocol.

**Done when.** Unit tier uses a tiny deterministic fake embedder (no
model download — invariant 2); integration proves `EXPLAIN` on a
similarity query shows the HNSW index, and `reembed` skips rows already
stamped with the configured model. `.env.example` gains the
`EMBEDDING_*` block.

## Step 6 — Tavily, the budget, and the ledger

**Goal.** `knowledge/sources/tavily.py` behind the `Source` Protocol;
`knowledge/budget.py` pure; every call and refusal a `source_call` row.

**Read first.** `docs/knowledge/tavily/DESIGN.md` — re-check the current
pricing page and correct the credit numbers in the document if they have
moved, in this step.

**Done when.** Unit: budget decisions at 79/80/100% of both caps and the
reserve; client against `httpx.MockTransport` fixtures including a 429
(recorded, **not retried**) and an empty-content result; `TAVILY_API_KEY`
is a `SecretStr` and never appears in any log or report (reuse the shell
redaction test pattern). Integration: refusals land as
`status=budget_refused` with zero credits.

## Step 7 — Hybrid retrieval

**Goal.** `knowledge/retrieve.py`: ANN CTE ∪ tsvector CTE, reciprocal
rank fusion, filters by asset and `published_at <= bar`. Read-only.

**Done when.** Integration on a seeded corpus with the fake embedder so
similarity is deterministic: the lexical leg finds an exact ticker the
dense leg misses and vice versa; **the no-look-ahead test** — append a
document published after the bar, retrieval for that bar is unchanged.
This is the invariant-17 analogue and the test the whole stage leans on.

## Step 8 — `signals --explain`

**Goal.** `knowledge/explain.py` (the sequence diagram in the graph
document), `validate.py`, the `explanation` upsert on the signal's
natural key, the `(asset, UTC day)` cache, and the CLI flag. Narrator
reuses the agents' pattern; template-only mode is first-class.

**Done when.** The stage exit criterion passes without an LLM key; a
rationale citing an id outside the retrieved set is rejected and the
sentence stripped; two explanations on the same asset-day make **one**
network call (assert on `source_call`); with the budget exhausted,
`explain` serves from cache and says so. README and AGENTS.md command
tables gain the flag; `make sdl` only if GraphQL grew (it should not —
this is CLI-first).

## Step 9 — Observer integration

**Goal.** One `collect_knowledge` node and four findings:
`source_budget` (OK / WARNING at 80% / CRITICAL at 100%),
`source_errors`, `embedding_backlog`, `uncited_explanations`.

**Read first.** The `add-agent` skill's rules — pure assess, graph-shape
test, thresholds dataclass — and the findings table in
`docs/knowledge/graph/DESIGN.md`.

**Done when.** Assess functions unit-tested at the boundaries;
`test_the_graph_has_the_documented_shape` updated together with the
observer document's Mermaid flowchart; `make observe` on a stack with no
knowledge rows reports the new findings as the calm `OK`/`INFO` they are.

## Step 10 — MCP server and closeout

**Goal.** `candlegraph mcp` as an optional extra (`--extra mcp`), stdio
transport, the "now" tools from `docs/knowledge/mcp/DESIGN.md`, `make
mcp`; then close the stage.

**Done when.** Tools list over stdio; `data_quality_report()` matches
`report --json`; no listed tool writes market data. **Closeout:** every
`docs/knowledge/*/DESIGN.md` and the coexistence document flip Status to
`implemented`; AGENTS.md data model, layout, commands and (if any changed)
invariants updated; `docs/README.md` statuses; the roadmap marks stage 1
delivered with its exit criterion; the full `release-check` skill runs
clean; this file moves to `docs/rollout/archive/2026-<date>-stage-1-knowledge-graph.md`
and the Status line at the top of the new current rollout (stage 2) replaces it.

---

## Standing rules for every step

- `make check` green before a step is called done; integration skips are
  not green (check for them).
- Schema changes only through `add-migration`, with a tested `downgrade`.
- New configuration lands in `settings.py` typed **and** `.env.example`
  commented, in the same step that introduces it.
- Secrets: `TAVILY_API_KEY` follows the `GRAPH_API_KEY` rules exactly.
- A step that touches a design decision updates the design document in
  the same change — the rollout never silently diverges from the designs.
- Update the progress table above at the end of each session, including
  "partial" with a note, so the next session starts from the truth.
