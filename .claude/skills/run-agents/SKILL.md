---
name: run-agents
description: Run candlegraph's agents — the run observer (stale data, gaps, metric drift) and the CI janitor (checks, mechanical fixes, draft PRs) — read their reports, and tell a real finding from an expected one. Use when asked whether the data is fresh or healthy, to check the agent reports, to run or schedule the agents, or to configure where reports go.
---

# Run the agents

Both agents are LangGraph graphs that only read and post reports. The design
is in `docs/agents/`; this is how to operate them.

## The run observer

Runs every `OBSERVER_INTERVAL` seconds (default 15 minutes) in the `agents`
compose service, and on demand:

```sh
make up
make observe                                   # one run; each stage printed to stderr as it finishes, then the report
make observe ARGS="--strict"                   # exit 1 at WARNING or worse
# A stage line reads: `ok   collect_metrics    1.87s  symbols=9, bars=1612, ...`.
# `skip` means the database-down short-circuit fired; the same rows are the
# report's "Stages" section and the Agents dashboard's stage strip.
# or in the UI: the Agents dashboard has a "Run observer now" button that
# runs once and writes the report to the agent ledger. The same dashboard
# shows whether the recent news (Tavily) runs succeeded; the News dashboard
# has the full ledger and the credit budget.
make agent-runs                                # every report, newest first
make agent-runs ARGS="--agent observer --show" # the newest report in full
make logs-agents                               # scheduled runs as they happen
```

Outside the stack: `uv run python -m candlegraph observe --sinks log`.

### Reading a report

The status is the worst finding. Read the findings in this order:

| kind | severity | Means | Do |
|---|---|---|---|
| `database` | critical | Postgres unreachable | `docker compose ps`, `docker compose logs postgres` |
| `missing_token` | critical | a configured token has no rows at all | `.claude/skills/add-token` step 1: check the address |
| `impossible_rows` | critical | `high < low` or OHLC outside the bar | a transform or upstream bug; never market data |
| `stale` | warning / critical | no successful ingest for 3 / 6 poll intervals | `make runs`, `make logs-worker` |
| `failed_runs`, `partial_run` | warning / critical | the ledger says runs are failing | the message names the targets; `make logs-worker` |
| `gap` | warning | a contiguous gap of `OBSERVER_SUSPICIOUS_GAP_HOURS`+ **on a liquid token** | `make runs` for that window; probably a failed run or a subgraph outage |
| `coverage` | warning | below `MIN_EXPECTED_COVERAGE`; the message says *thin* or *liquid* | thin: expected, tune the threshold. liquid: investigate |
| `drift` | warning | median volume or realised volatility moved by more than the ratio | look at the market; this is news, not a fault |
| `run_drift` | warning | ingest runs got much slower or write a very different amount | `make runs`; a subgraph slowdown or a token list change |
| `drift` | info | not enough bars to judge | expected on a new stack or a thin token |
| `fresh`, `ledger`, `coverage`, `drift` | ok | checked and fine | nothing |

Four knowledge-layer findings ride in every observer report (stage 1,
step 9); read them the same way:

- `source_budget` — WARNING at 80% of the monthly Tavily credits, CRITICAL
  at 100%. The number comes from the `source_call` ledger, the same sums
  the budget guard refuses on.
- `source_errors` — WARNING only on a streak of `error`/`rate_limited`
  calls. A `budget_refused` is the guard working and never counts.
- `embedding_backlog` — INFO means new chunks await `make embed`;
  WARNING means chunks are stamped with another model and retrieval cannot
  see them until `make reembed` finishes.
- `uncited_explanations` — WARNING only when *every* explanation in the
  window has zero citations (retrieval returns nothing); some uncited
  explanations are an honest quiet corpus.

On a stack that has never used the knowledge layer all four are OK — that
is the designed reading, not a bug.

Three things the observer deliberately does **not** do, so do not "fix" them:

- It never flags a gap on a thin token. Invariant 19: an hour with no swaps
  has no row. GNO quiet for a day is the market.
- Drift severity does not escalate with the ratio. A tenfold volume change is
  still a warning for a human to read.
- A window with fewer than `OBSERVER_MIN_BARS` bars produces no drift
  judgement at all.

### Tuning

| Symptom | Knob |
|---|---|
| `stale` fires on a healthy slow poll | `OBSERVER_STALE_INTERVALS` (multiples of `DATA_POLL_INTERVAL`) |
| `gap` fires on a token that is genuinely thin | raise `OBSERVER_LIQUID_VOLUME_USD` |
| `drift` is noisy | raise `OBSERVER_VOLUME_DRIFT_RATIO` / `OBSERVER_VOLATILITY_DRIFT_RATIO`, or widen `OBSERVER_DRIFT_WINDOW_HOURS` |
| too many `not enough bars` | lower `OBSERVER_MIN_BARS`, or wait |

## The CI janitor

Runs daily from `.github/workflows/janitor.yml` (needs `GH_TOKEN`; set
`JANITOR_TOKEN` to a PAT if the PRs it opens should trigger CI). Locally:

```sh
make janitor                              # checks; fixes stay in your tree
make janitor ARGS="--checks fmt-check,lint"
make janitor ARGS="--open-pr --strict"    # branch, commit, push, draft PR
```

Expect:

- `OK`: every check passed.
- `INFO`: something failed, `make fmt` fixed it, the re-check passed. With
  `--open-pr` the finding carries the PR URL; without it, `git diff`.
- `WARNING`: a check still fails. `typecheck` and `test-unit` are never
  fixed by the janitor — read the output section and fix it yourself. Also
  `WARNING` when the tree was dirty and a fix was skipped: commit or set
  aside your work and re-run.
- `CRITICAL`: a check could not run (timeout, `make` missing).

The janitor refuses to commit if `git diff --cached --name-only` shows `.env`
or anything ending in `.pem`/`.key`. That is a feature.

## Where reports go

`AGENT_REPORT_SINKS` (default `log,ledger`):

| sink | needs | note |
|---|---|---|
| `log` | nothing | level follows the status |
| `ledger` | the database | `make agent-runs` reads this |
| `file` | `AGENT_REPORT_DIR` | one Markdown file per run; the janitor workflow uploads it |
| `webhook` | `AGENT_WEBHOOK_URL` | Slack-compatible `{"text": ...}` |
| `github` | `gh` logged in, optionally `AGENT_GITHUB_REPO` | opens an issue at WARNING or worse only |

A sink failing is logged and skipped; the run still records. Set
`AGENT_LLM_MODEL` and `ANTHROPIC_API_KEY` (and `uv sync --extra llm`) to
have a model write the summary paragraph; it changes prose only.

## Verifying the schedule

```sh
make logs-agents          # "Agents starting: observer (observer every 900s ...)"
sleep 5 && make agent-runs
```

The first observer run fires immediately on start, so one row appears within
seconds of the service coming up. On a fresh stack expect `INFO` findings
about thin windows and `CRITICAL` `missing_token` until the first ingest
lands — that is the correct report for an empty database, not a bug.
