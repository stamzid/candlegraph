# System review, 2026-11-16

**Status:** complete. Seven findings fixed in the same pass; nine open
(8–16) with a recommendation each. At the stage 2 closeout (2026-11-19)
all nine were still open; 13 and 15 are touched but not closed — the
dispatcher prunes `run_queue`, and since 2026-11-20 the `prune` job keeps
`run` to `RUN_RETENTION_DAYS` (container logs still grow), and `embed` is a
step of `daily.yaml`, which has no trigger yet. **Finding 8 is half
closed** (2026-11-19): every published port now binds to `127.0.0.1` by
default (`BIND_HOST`), so the exposure is gone on a laptop; there is
still no authentication.
**Scope:** the whole repository, read with production in mind — a single
VM running compose, which is the deployment target settled in
[`../../harness/pipelines/DESIGN.md`](../../harness/pipelines/DESIGN.md) §16.
**Why now:** before pipelines are built, so the harness is not built on
top of something that already leaks or silently skips work.

## How to read this

Severity is about **production on one VM**, not about a laptop:

| | Meaning |
|---|---|
| **High** | Can lose data, spend money without being asked, or expose the box |
| **Medium** | Degrades over time or hides a failure |
| **Low** | Wrong but bounded; fix when nearby |

## Summary

| # | Finding | Severity | State |
|---|---|---|---|
| 1 | HTTP clients were never closed (socket leak per explanation) | Medium | **Fixed** |
| 2 | Missed scheduler firings were silently dropped | Medium | **Fixed** |
| 3 | `EMBEDDING_DIMENSIONS` was not checked against the column | Low | **Fixed** |
| 4 | Redaction missed bare `sk-…` provider keys | Low (security) | **Fixed** |
| 5 | Two threads could each build a connection pool | Low | **Fixed** |
| 6 | A failed ledger open left an ingest run `running` forever | Low | **Fixed** |
| 7 | An explanation counted chunks as documents ("3 documents" for one article) | Medium | **Fixed** |
| 8 | No authentication on the API or the UI, and ports bind to every interface | **High** | Open — needs your decision |
| 9 | No backups | **High** | Open |
| 10 | The chart query has no window or row limit | Medium | Open |
| 11 | No statement timeout: one slow query can hold a connection forever | Medium | Open |
| 12 | `worker` and `agents` have no health check | Medium | Open |
| 13 | Container logs and the `run` table grow without limit | Medium | Open |
| 14 | The UI recomputes everything on every click; a backfill blocks it | Medium | Open |
| 15 | Known and already designed: unusable reserve, budget race, no scheduled embed | Medium | Open (designs exist) |
| 16 | Nothing records what a model cost | Low | Open (design added) |

---

## Fixed in this pass

### 1. HTTP clients were never closed — Medium

**What.** `OllamaEmbedder`, `OpenAIEmbedder` and `TavilySource` each open
an `httpx.Client` (a connection pool) in `__init__`. Nothing closed them.
`services_for()` builds both on **every** explanation, and the GraphQL
`explainSignal` mutation calls that path.

**Why it matters.** The API and the agents process are long-lived. Every
explanation leaked a client; sockets are only released when the garbage
collector happens to run. On a busy day this ends in `Too many open
files`, and the symptom appears far away from the cause.

**Fix.** `close()` on all three, plus a `services(settings)` context
manager that closes them when the block ends; the CLI, the UI and
`explain_signal_now` now use it. A failed close is logged, never raised.

### 2. Missed scheduler firings were silently dropped — Medium

**What.** APScheduler drops a firing it missed by more than
`misfire_grace_time`, whose default is **one second**. The builder never
set it.

**Why it matters.** Anything that pauses the process past one second —
a slow run holding the slot, a suspended VM, a restart, a long stop — made
the due tick vanish with a log line, instead of running late. For ingest
the next tick recovers; for the observer it is a check that never
happened; for the pipelines' schedule trigger (the safety net behind
events) it would be the safety net failing quietly.

**Fix.** `misfire_grace_time = max(60, interval)` per job. With
`coalesce=True` (already set), a long pause produces exactly one
catch-up run, not a burst. Two tests pin it.

### 3. `EMBEDDING_DIMENSIONS` was not checked against the column — Low

**What.** The vector column is `halfvec(1024)`. `COLUMN_DIMENSIONS = 1024`
existed in `embed.py` but was never compared with the setting.

**Why it matters.** `EMBEDDING_DIMENSIONS=768` was accepted at start-up
and failed later, inside the embed loop, as a Postgres type error — after
the model had already done the work.

**Fix.** The constant moved to `settings.py` (one definition, re-exported
where readers look for it) with a validator that refuses any other width
at start-up and names the two ways out.

### 4. Redaction missed bare `sk-…` provider keys — Low, security

**What.** `redact()` covered `Bearer …`, `KEY=…`, DSN passwords and bare
`tvly-…`, but not a bare `sk-ant-…` or `sk-proj-…` with no `KEY=` in
front of it — the shape an SDK error or a pasted config has.

**Why it matters.** Redaction is the last line before text reaches a
report, a ledger row, a GitHub issue or an MCP client. The models design
adds more places these keys can be echoed.

**Fix.** One more pattern, with a test that a too-short `sk-` string is
left alone.

### 5. Two threads could each build a connection pool — Low

**What.** `DatabaseManager.pool` was a lazy `if self._pool is None:` with
no lock. Scheduler jobs run in threads, so the first two concurrent jobs
could both build a pool; one would be orphaned with its connections open.

**Fix.** Double-checked locking, with the fast path reading the attribute
once into a local.

### 6. A failed ledger open left an ingest run `running` forever — Low

**What.** `IngestLedgerHook.finished()` used `self.record`, only ever set
in `started()`. If `started()` raised, `finished()` raised
`AttributeError`, which the hook fan-out swallows — so the row stayed
`running` and looked like a hung process forever.

**Fix.** `record` is initialised to `None` and `finished()` rebuilds one
from the run when it is missing. The two-phase row's whole point is that
`running` means something; it must not be reachable by accident.

### 7. An explanation counted chunks as documents — Medium

**What.** Retrieval returns **chunks**, and several chunks often come
from one article. The template counted citations and called them
documents. Caught on a live stack: one story retrieved three times
produced

> "3 documents published before the bar were retrieved for context [1][2][3]"

with all three references pointing at the same URL.

**Why it matters.** It overstates corroboration — the thing an
explanation exists to establish. A reader, or a model writing the
rationale from this context, infers three independent sources where
there is one. It is the same class of error as a p-value of 0.000: a
number that reads stronger than the evidence behind it.

**Fix.** Count distinct `document_id`s and name passages separately:
"1 document (3 passages) published before the bar was retrieved
[1][2][3]". Every chunk is still cited and still checkable by the
citation validator; only the claim changed. Verified on the stack.

---

## Open: production, in order of severity

### 8. No authentication, and ports bind to every interface — High

**What.** There is no auth anywhere in the API or the UI. Compose
publishes four ports with no host address, which means `0.0.0.0`:

| Port | Service | What an anonymous caller can do |
|---|---|---|
| 8000 | API | run `explainSignal(search: true)` — **spends Tavily credits** and writes rows; run strategies and ARIMA fits (CPU) |
| 8501 | UI | press *Run observer*, *Fetch news*, *Backfill* |
| 5432 | Postgres | attack the database directly (password is `candlegraph` by default) |
| 11434 | Ollama | use your GPU/CPU to run models |

On a laptop this is harmless. On a VM with a public IP it is an open
door. The Tavily budget caps the money at the daily limit, which is the
only thing bounding it today.

**Recommendation**, in order:

1. **Bind to localhost** in compose: `"127.0.0.1:${APP_HOST_PORT:-8000}:8000"`,
   and the same for the other three. Nothing about local development
   changes. One line per port.
2. Put a reverse proxy (Caddy or nginx) in front with TLS and basic auth
   or an OAuth proxy; expose only that.
3. Add `API_REQUIRE_KEY` / `API_KEY` for machine callers, and a rate
   limit on the mutation.
4. Add `EXPLAIN_ALLOW_SEARCH` (default **false** in production) so a
   remote caller cannot spend credits at all.

I did not change compose: the rollout's standing rule is that compose
changes only in a step that says so, and binding to localhost could
break a LAN workflow you rely on (reaching the UI from a phone). Say the
word and it is a five-minute change.

### 9. No backups — High

**What.** No `pg_dump` target, no schedule, no retention. The database is
a Docker volume. `make down` deletes it, and the skills warn about that
repeatedly — which is a sign the risk is real.

**Why it matters.** Market history is the one thing the system cannot
re-create: `LOOKBACK_DAYS` bounds a first fetch, and the subgraph will
not give back arbitrary history cheaply. Invariant 4 promises stored
history is never lost; nothing enforces it against disk loss.

**Recommendation.** A `make backup` target running `pg_dump -Fc` to a
path outside the volume, a daily cron (or a `backup` job in the registry
once pipelines exist), 7 daily + 4 weekly copies, and — the part people
skip — a `make restore-check` that restores the newest dump into a
throwaway container and counts rows. An untested backup is not a backup.

### 10. The chart query has no window or row limit — Medium

**What.** `getChartData` passes `start_unix=None, end_unix=None`: every
stored hour, five series, no `LIMIT`. (`timeUnitInHours` *is* validated,
1–168, and tested.) The MCP `candles` tool does pass a window; GraphQL
does not.

**Why it matters.** Today ~750 rows per token. After a year of five
tokens it is ~44,000 points per request, built in Python and serialised
to JSON — seconds of CPU and tens of MB per call, repeatable by anyone
(see finding 7).

**Recommendation.** Add optional `days`/`limit` arguments with a default
window (`ANALYTICS_LOOKBACK_DAYS`), and a hard cap on points returned.
`make sdl` regenerates the schema; one e2e test asserts the default
window.

### 11. No statement timeout — Medium

**What.** The pool sets `-c timezone=UTC` and nothing else. There is no
`statement_timeout` and no `idle_in_transaction_session_timeout`.

**Why it matters.** One pathological query (a huge backfill window, a
missing index after a schema change) holds a pooled connection
indefinitely. With `max_connections = 10`, ten of those stop the whole
process, and the health check — one round trip — goes down with it.

**Recommendation.** `DB_STATEMENT_TIMEOUT_MS` (default 30s for the API
and UI, higher for the worker, which does long upserts) and
`idle_in_transaction_session_timeout = 60s` for everyone, both appended
to the pool's `options`. It also makes the pipelines design's "Postgres
errors are transient, retry" true in practice rather than in principle.

### 12. `worker` and `agents` have no health check — Medium

**What.** Only `postgres`, `candlegraph`, `ui` and `ollama` declare one.
The two background processes do not, so compose reports them `running`
whether they are working or wedged.

**Why it matters.** The failure this hides is exactly the one the
two-phase ledger rows were added to expose: a process that stopped doing
work while still being alive. `make up --wait` also cannot wait for them.

**Recommendation.** A cheap check that reads the ledger rather than the
process: *has this process recorded a run within N intervals?* The
`/status` body already computes ingest freshness; the same query, run in
the container, is the health check. Belongs with the pipelines work,
since the executor needs the same thing.

### 13. Logs and `run` rows grow without limit — Medium

**What.** No `logging:` options in compose, so Docker's json-file driver
keeps everything until the disk fills. And the `run` table has no
retention, though you have now set one (one month).

**Recommendation.** `logging: { driver: json-file, options: { max-size: 10m, max-file: 3 } }`
on each service, and the `prune` job the pipelines design now specifies
(`RUN_RETENTION_DAYS`, default 30) covering `run`, `run_queue` and
`run_event`. `ingest_run` and `agent_run` keep their history: the
observer's drift rules read them.

### 14. The UI recomputes on every click, and a backfill blocks it — Medium

**What.** Only the symbol list is cached (`st.cache_data(ttl=300)`).
Every widget change re-runs the page and re-queries, including ARIMA
fits and backtests. `run_backfill` runs in the Streamlit thread, so the
tab is unusable for minutes and a second viewer sees nothing.

**Why it matters.** Fine for one person on a laptop; poor for several
people on one VM, where it is also the easiest way to saturate the box.

**Recommendation.** `st.cache_data` on the view builders, keyed by their
arguments (they are pure and already return plain objects, which is why
this is cheap). Longer term, the backfill button should enqueue a run
through the harness instead of doing the work inline — which is exactly
what the pipeline queue gives, so it is better done then than now.

### 15. Already designed, still open — Medium

| Item | Where the fix is designed |
|---|---|
| The Tavily "manual reserve" cannot be spent by anyone | `knowledge/budget-manager/DESIGN.md`, build step 1 |
| Budget check-then-spend race (two callers can both pass) | same document, build step 2 (advisory lock) |
| Nothing embeds on a schedule, so fresh news is lexical-only until `make embed` | rollout step 4 (`embed` as a pipeline step) |

No new work needed here — they are listed so the count is honest.

### 16. Nothing records what a model cost — Low

**What.** Runs record which model wrote prose only once the models design
lands; nothing records tokens or money, for any provider.

**Why it matters.** Per-step model switching (your answer 3) makes "which
step is expensive?" a question people will ask weekly.

**Recommendation.** Covered by the context-memory section added to
`../../harness/models/DESIGN.md`: `counts.tokens_in/out` and
`counts.cost_micros` on every run that calls a model, which the budget
manager's pots can then meter the same way they meter search credits.

---

## What I checked and found healthy

Worth recording, so the next review need not repeat it:

- **No SQL built with f-strings**; every statement is parameterised and
  schema-bound in one module.
- **The Graph API key travels in an `Authorization` header**, not in the
  URL, so it cannot leak through an error message or an access log.
- **No bare `except: pass`**, no `datetime.now()` without a timezone, no
  `utcnow()` anywhere.
- **The MCP boundary** redacts results and every escaping error, and the
  "writes nothing" registry test still holds.
- **psycopg2's pool discards connections lost to a server restart**, so a
  database bounce costs one failed query per stale connection, not a
  permanently broken process.
- **`timeUnitInHours` is validated and tested** (finding 9 is about
  volume, not injection).
- **Harness coverage is 96%**, with the gaps in error paths that are
  themselves logged-and-skipped.
- **Every write path upserts on a natural key**, which is what makes the
  pipelines design's retry-and-resume model sound.

## Suggested order

1. Finding 7 (bind to localhost) — minutes, and it is the one that can
   cost money.
2. Finding 8 (backups + a restore check) — an afternoon.
3. Findings 10, 11, 12 — small, and they make the single VM observable.
4. Finding 9 — with the next `make sdl` change.
5. Finding 14 — fold into the pipelines work, where the queue already
   exists.

## One more thing, noticed on the stack

On a brand-new volume the observer's first run is CRITICAL: it fires
immediately, before the first ingest has finished, and reports the active
tokens as having no rows. It is expected (the `run-agents` skill says so)
but it is noise on every fresh start, and it is exactly what the
pipelines design removes by starting the observer from the "ingest
finished" event instead of its own clock. No separate fix needed; worth
checking it is gone after rollout step 5.
