# Backlog: known gaps, deferred work, and what is accepted

**Status:** living — opened 2026-11-20 at the pipelines closeout (rollout
step 4.1). Every entry was found by work that shipped, and was recorded
where it was found: a step's **Found, not fixed** table, a design's §15,
an audit finding, or a post-mortem's follow-up. This page gathers them so
no one has to read five documents to learn what is already known.

**How to use it.** Entries have stable ids (`A3`, `C1`) — say "closes B1"
in a rollout step and the id can be struck here in the same change. An
entry is here because it is **known and deliberate**, not because it is
forgotten. Nothing here is a bug report against code that claims to work:
where behaviour is surprising but intended, it is in "Accepted
behaviours" at the bottom, so it is not re-litigated every session.

**What this is not.** It is not the plan — [`../rollout/ROLLOUT.md`](../rollout/ROLLOUT.md)
is, and it is not the roadmap — [`../roadmap/DESIGN.md`](../roadmap/DESIGN.md)
is. An entry here moves into one of those when it is scheduled.

| Column | Meaning |
|---|---|
| **When** | The trigger that should pull it in, not a date |
| **Found** | Where it was recorded, so the reasoning survives |

---

## A. Harness and pipelines

| id | Gap | When | Found |
|---|---|---|---|
| **A1** | `candlegraph run` executes a pipeline in the foreground instead of queueing it, so invariant 28's "a person never starts a pipeline directly" is true of the UI but not of the CLI. It is safe — it takes the advisory lock — but inconsistent, and `manual_dedup_key` was written for the enqueue that would fix it and is still unused | With any CLI work on `run`; it is a small change and would retire dead code | step 3 |
| **A2** | No MCP `run_pipeline` tool. The UI can ask for a run and an assistant cannot; the enqueue path it would wrap already exists. It is the one unbuilt row of [`../knowledge/mcp/DESIGN.md`](../knowledge/mcp/DESIGN.md) | With A1 — one enqueue seam, three surfaces | step 3 |
| **A3** | A queued request cannot be withdrawn. A mistaken *Run now* runs; harmless while every pipeline is idempotent and cheap | **Before step 6** gives `daily.yaml` a schedule — a queued spending run should be cancellable | step 3 |
| **A4** | `parent_run_id` means "owned by" for a step and "caused by" for an event-triggered run. The Pipelines page separates them (a child named like a pipeline was *started*, not owned); `candlegraph runs` does not, and would indent a caused pipeline as a step; `prune` deletes a caused pipeline with its cause and leaves its steps for the next day | When any spec first triggers on another pipeline. Latent today: none does | steps 2, 3, 4 |
| **A5** | A run family stuck `running` — a process that died mid-run — is never closed and never pruned. The queue has a lease reaper; the ledgers have no equivalent | When a stuck family is first seen in `run_log`, or with any work on process shutdown | step 4 |
| **A6** | No `candlegraph prune [--dry-run]`. Pruning on demand means restarting the agents container, and nothing shows what *would* go. `RUN_RETENTION_DAYS=1` is accepted and would delete almost everything at the next run | With the next CLI pass; a `--dry-run` is the cheap half | step 4 |
| **A7** | `prune_done_queue_rows` says `state IN ('done', 'dead')`, but the only path to `dead` — the lease reaper — never sets `finished_at`, and the prune filters on `finished_at < …`. A NULL comparison is never true, so **the `'dead'` half of that clause can never match**: dead rows accumulate for ever. Keeping them is arguably right ("it needs a person"), but the statement and the behaviour disagree, and nobody has decided which is correct | Decide, then make them agree: either drop `'dead'` from the clause, or set `finished_at` when the reaper kills a row and keep them until acknowledged | step 4.1 |
| **A8** | Deferred by the pipelines design §15, each with its reason: **step-level resume** (§7.4), the **circuit breaker** (§7.6), **metrics** (§8.3), and the **enforced half of timeouts** (§7.5 — today they are cooperative). None has shown a symptom | Resume: when a pipeline is long enough that re-running from the start is expensive (stage 4's walk-forward is the first candidate). Metrics + breaker: together, with stage 5's alerting. Hard timeouts: a real deployment | pipelines §15 |
| **A9** | `pipelines/daily.yaml` has no trigger, so news is fetched and embedded only when someone asks. An unembedded corpus degrades retrieval to its lexical leg — the 2026-11-04 incident's failure mode | **Step 6**, after the budget manager (step 5) makes automatic spending safe | plan, steps 4–6 |
| **A10** | The dispatcher runs at most one pipeline per tick and claims in `available_at` order, so a *Run now* click can wait behind queued work for an extra tick | When a tick's latency matters; raising it trades tick-boundedness for throughput | stage 2 archive, step 3 |

## B. The UI

| id | Gap | When | Found |
|---|---|---|---|
| **B1** | Which pipelines need a spend confirmation is decided by `SPENDING_JOBS`, a hand-kept name list in `ui/viewmodels.py`, because `Job` has no `spends` declaration. A new metered job not added there gets a *Run now* with no confirmation | **Before any second metered job.** The real fix is a `spends` field on `Job` with a registry test, like `writes` | step 3 |
| **B2** | The page judges "the dispatcher is on" from its *own* `EVENT_POLL_INTERVAL`; the agents container reads its own, and the UI cannot see whether that process is alive. The backstop is the warning after a row sits claimable for three intervals | When the two can diverge — a deployment that configures containers separately | step 3 |
| **B3** | While a requested run is unfinished the page re-reads every 5 s; bounded by that run, but unbounded in time if the dispatcher never claims it | With B2's fix, or a poll ceiling | step 3 |
| **B4** | The 5 s refresh has never been watched in a real browser: `AppTest` does not fire `run_every` timers, so the watcher's states were verified by re-running it and on a live stack | Whenever someone has the page open in a browser — confirm, then record it | step 3 |
| **B5** | Every dashboard recomputes on every widget change, and a backfill blocks the page (audit 14) | When a page is actually slow; the Pipelines page costs five reads per rerun | audit 14 |
| **B6** | The Market dashboard's five-year hourly read takes ~610 ms. The daily aggregate cannot serve it without changing what the chart shows — a display decision, not a storage one | Whoever next owns that page | stage 2 step 6, TimescaleDB design |

## C. Operations, security and tooling

| id | Gap | When | Found |
|---|---|---|---|
| **C1** | **No authentication** on the API or the UI (audit 8). Since 2026-11-19 every published port binds to `127.0.0.1` (`BIND_HOST`), which removes the exposure on a laptop — and the UI can now start runs, so the bind is load-bearing | **Before the stack is reachable from another machine.** Its own small rollout, not a step of this plan | audit 8, step 1 |
| **C2** | **No backups** (audit 9). Stored history is re-ingestible only within what the subgraph still serves | When history exceeds what re-ingestion can reproduce — likely with stage 3's vendors | audit 9 |
| **C3** | No statement timeout, and the chart query has no window or row limit (audit 10, 11): one slow query can hold a connection | With C1 — all three are "this box faces something" work | audit 10, 11 |
| **C4** | `worker` and `agents` have no health check; four other services do, so a wedged worker looks fine to Compose | Next time `docker-compose.yaml` is opened — it is a few lines | audit 12, plan |
| **C5** | Container logs grow without limit (the other half of audit 13; the `run` table half is now pruned) | With C4, in the same compose pass | audit 13 |
| **C6** | Nothing records what a model call cost (audit 16) | With step 7's model seam, which is where the number would come from | audit 16 |
| **C7** | The stack guard runs only through `make`. A raw `docker compose up` from the wrong checkout is unchecked | If a raw-compose collision is ever seen again | step 4 |
| **C8** | The draft-migration check reads `git status` for `alembic/versions`, so a draft **committed** and then amended or rebased under a new id is not caught | If a fourth volume is lost this way | step 4 |
| **C9** | A volume whose containers are gone has no recorded owner, so `make stacks` lists it as unattributable rather than guessing. **Declined**, with the reason: labelling volumes in the compose file makes Compose stop and ask whether to recreate — that is, delete — every existing volume | Revisit only if it causes a fault | 2026-11-20 post-mortem |

## D. Knowledge and credits

| id | Gap | When | Found |
|---|---|---|---|
| **D1** | One credit pot serves automatic and manual searches, the reserve is unreachable, and a race can overspend it (audit 15). The design exists: [`../knowledge/budget-manager/DESIGN.md`](../knowledge/budget-manager/DESIGN.md) | **Step 5 — next.** It is what makes A9 safe | audit 15 |
| **D2** | The embeddings evaluation set (`knowledge eval`) and the janitor's incident-similarity finding were named in stage 1 and never built | When retrieval quality is questioned with numbers rather than impressions | stage 1 archive |

---

## Accepted behaviours — surprising, and intended

Recorded so they are not reported as bugs. Each is tested or documented
where it is decided.

| Behaviour | Why it is right |
|---|---|
| A fresh stack's **first observer report is CRITICAL** | It fires before the first ingest finishes and truthfully reports an empty database. The next run is `warning` (GNO coverage). The `run-agents` skill says so |
| **GNO sits below the coverage threshold**, and `report --strict` exits 1 for it | GNO is in the active set *because* its history is gappy. A missing hour is not a fault (invariant 19); do not lower the threshold |
| On a **cold start the safety net fires before any data exists**, so the first `hourly` run fails at `validate` | "There is no data" is a true, useful thing for the ledger to say; the next ingest starts a real run a minute later |
| An **event older than `EVENT_LOOKBACK_MINUTES` is never acted on** | The window *is* the catch-up; the `schedule` trigger is the net behind it |
| A **broken spec stops its own pipeline silently** | The dispatcher skips it so the others still run, and records the error in the tick. `make check-pipelines` is what stops it being merged |
| **A losing backtest** | Usually the honest answer. Read `excess` and costs before touching parameters |
| A pipeline **slower than `QUEUE_LEASE_SECONDS`** is reclaimed while running | The advisory lock stops a second run; the row returns to the queue. Wasteful, not wrong |
| The **UI never executes a pipeline** — *Run now* only queues | Invariant 28. One executor, one lock, one ledger |

## Closed since this page opened

Kept short on purpose; the detail is in the step that closed it.

| id | Closed by |
|---|---|
| The `run` table growing without bound (audit 13, half) | step 4, the `prune` job, 2026-11-20 |
| Ports open on every interface (audit 8, exposure half) | step 1, `BIND_HOST`, 2026-11-19 |
| An integration test that failed instead of skipping without Docker | step 1, 2026-11-19 |
| Invariant 4's delete-target test blind to any DELETE with a quoted literal | step 4, 2026-11-20 |
