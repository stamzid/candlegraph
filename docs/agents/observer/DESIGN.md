# Run observer

**Status:** implemented — 2026-10-25
**Graph:** `candlegraph/agents/observer/graph.py`
**Command:** `candlegraph observe [--strict]`, `make observe`
**Schedule:** the `agents` compose service, every `OBSERVER_INTERVAL` seconds

## Job

Answer, on a schedule and in writing, the three questions an operator asks of
the data: **is it fresh, is it complete, and does it still look like the
same market?** It reads the ingest ledger, the data-quality report, and a
trailing window of stored rows; it writes a report and nothing else.

## Flow

```mermaid
graph TD
  __start__ --> collect_ledger
  collect_ledger -. database down .-> publish
  collect_ledger --> collect_quality
  collect_quality --> collect_metrics
  collect_metrics --> collect_knowledge
  collect_knowledge --> assess
  assess --> publish
  publish --> __end__
```

`tests/unit/agents/test_observer_graph.py::test_the_graph_has_the_documented_shape`
pins this node and edge set.

| Node | Reads | Writes to state |
|---|---|---|
| `collect_ledger` | `ledger.recent_runs`, `ledger.seconds_since_last_success` | `runs`, `seconds_since_success`, `database_up` |
| `collect_quality` | `quality.build_report` over `OBSERVER_BASELINE_DAYS` | `quality` |
| `collect_metrics` | `prices.load_price_history` for every tracked symbol over baseline + recent window | `metrics` (per-symbol recent vs baseline numbers) |
| `collect_knowledge` | `source_call` spend and recent statuses, the embedding backlog for the configured model, explanation citation counts over the recent window | `knowledge` (a `KnowledgeFacts` of numbers and the caps) |
| `assess` | state only — pure | `findings` |
| `publish` | — | `report`, posted to the sinks; `stages` completed with any skipped node |

Every node except `publish` is wrapped by `agents/stages.py::staged`, which
appends a `StageRecord` to `stages` (see below); `publish` records itself
after the sinks have answered.

## Stages: what the run did, visibly

Before 2026-11-04 a run was a black box until its Markdown arrived. When
`collect_metrics` took a minute over ten tokens and a week of baseline,
nothing said which node was running; the report did not record how long
each read took or what it read; and the database-down short-circuit left
no trace of the nodes it skipped. An operator could not tell a slow run
from a hung one, or a quiet report from one that never looked.

Each node now leaves a `StageRecord`: name, status (`ok`, `error` when the
node recorded an error and the graph went on, `skipped`), start time,
duration, and a small dict of what it did. The records are **facts about
the run, not findings**: severity still comes from the findings alone
(invariant 22), and a slow stage is information, never a warning.

| Stage | Detail recorded |
|---|---|
| `collect_ledger` | `database_up`, `runs` read, `seconds_since_success` |
| `collect_quality` | `tokens` judged, `missing_tokens`, `violations` |
| `collect_metrics` | `symbols` with metrics, `bars` loaded, `runs_judged` |
| `collect_knowledge` | `credits_month`, `calls` read, `backlog`, `explanations` in the window |
| `assess` | `findings` and the count per severity |
| `publish` | `sinks` posted to, `failed` |

They go three places, so the same record answers the question wherever it
is asked:

- **The report** gains a `## Stages` table, first after the findings, with
  skipped stages named and their reason (`database unreachable`).
- **The ledger row** carries them in `agent_run.detail.stages`, which is
  what the Agents dashboard's stage strip reads (one metric per stage:
  status, seconds, detail), so a run recorded before this feature shows an
  empty strip rather than an error.
- **Live**: `candlegraph observe --progress` (what `make observe` passes)
  prints one line per stage to **stderr** as it finishes, so `--json` on
  stdout stays machine-readable; the UI's **Run observer now** button
  streams the same lines into its status box.

```
observer stages:
ok   collect_ledger     0.04s  database_up=True, runs=2016, seconds_since_success=241
ok   collect_quality    0.31s  tokens=9, missing_tokens=0, violations=0
ok   collect_metrics    1.87s  symbols=9, bars=1612, runs_judged=2016
ok   assess             0.00s  findings=7, critical=0, warning=3, info=0, ok=4
ok   publish            0.02s  sinks=log,ledger, failed=-
```

The wrapper is generic (`agents/stages.py`) and the janitor can adopt it
without changing its nodes; it has not yet, because its stages are already
visible as the shell commands it runs.

`assess` is a set of pure functions in `observer/assess.py`, each taking the
collected facts and the thresholds and returning findings. They are the part
worth testing exhaustively, and they are tested without a database.

## How a fact becomes a severity

Every rule below is a pure function in `assess.py`. The two branches worth
reading twice are the ones that stop the observer crying wolf: a gap is
tested against the token's own liquidity before it is a finding, and a
window too short to support a statistic produces `INFO`, never a guess.

```mermaid
flowchart TD
  START([collected facts]) --> F{"last successful<br/>ingest?"}
  F -->|"never, empty ledger"| FI["INFO<br/>stack is new"]
  F -->|"never, runs exist"| FC["CRITICAL"]
  F -->|"age > 2x stale window"| FC
  F -->|"age > stale window"| FW["WARNING"]
  F -->|"recent"| FO["OK"]

  START --> Q{"quality report"}
  Q -->|"token has zero rows"| QC["CRITICAL<br/>missing_token"]
  Q -->|"high &lt; low, OHLC out of bar"| QC2["CRITICAL<br/>impossible_rows"]
  Q -->|"coverage &lt; threshold"| QW["WARNING<br/>message says thin or liquid"]
  Q -->|"contiguous gap >= 6h"| L{"mean hourly volume<br/>>= liquid threshold?"}
  L -->|"yes, e.g. WETH"| GW["WARNING<br/>gap"]
  L -->|"no, e.g. GNO"| GN["no finding<br/>invariant 19"]

  START --> D{"bars in both<br/>windows >= min_bars?"}
  D -->|"no"| DI["INFO<br/>not enough data"]
  D -->|"yes"| R{"recent median /<br/>baseline median"}
  R -->|"outside 1/k .. k"| DW["WARNING<br/>drift"]
  R -->|"inside"| DO["OK"]

  classDef crit fill:#ffebee,stroke:#c62828
  classDef warn fill:#fff8e1,stroke:#f9a825
  classDef info fill:#e3f2fd,stroke:#1565c0
  classDef ok fill:#e8f5e9,stroke:#2e7d32
  class FC,QC,QC2 crit
  class FW,QW,GW,DW warn
  class FI,DI info
  class FO,GN,DO ok
```

## What it looks for

### Stale data

`seconds_since_last_success` against `DATA_POLL_INTERVAL`:

| Condition | Severity |
|---|---|
| no successful run ever | `CRITICAL` — unless the ledger is empty too, in which case `INFO`: the stack is new |
| age > `OBSERVER_STALE_INTERVALS` × poll interval | `WARNING` |
| age > 2 × that | `CRITICAL` |

Plus the ledger's own pattern: the last `OBSERVER_FAILURE_STREAK` runs all
`failed` is `CRITICAL`; a `partial` in the most recent run is `WARNING` and
names the targets that failed, read from the run's `errors` JSON.

### Gaps

Invariant 19 governs: **a missing hour is not a fault.** The observer does
not flag gaps as such. It flags:

- a token in `missing_tokens` (configured, zero rows) — `CRITICAL`;
- `IMPOSSIBLE ROWS` — `CRITICAL`;
- coverage below `MIN_EXPECTED_COVERAGE` — `WARNING`, with the same caveat the
  report prints;
- a contiguous gap of at least `OBSERVER_SUSPICIOUS_GAP_HOURS` **on a liquid
  token** — one whose baseline mean hourly volume exceeds
  `OBSERVER_LIQUID_VOLUME_USD`. GNO going quiet for a day is the market; WETH
  going quiet for six hours is a failed run or a subgraph outage. This is the
  distinction `backfill-and-verify` tells a human to make; the observer makes
  it the same way.

### The knowledge layer (stage 1, rollout step 9)

Four findings over the knowledge tables, read-only (invariant 21), graded
by `assess_knowledge` — pure, like every other rule here. A stack that has
never used the knowledge layer comes out all `OK`: nothing spent, nothing
failing, nothing waiting. Absence of news is not a fault.

| kind | severity | rule |
|---|---|---|
| `source_budget` | OK below 80%, WARNING from 80%, CRITICAL from 100% | credits spent this UTC month against `TAVILY_MONTHLY_CREDITS`, summed from `source_call`; the grading is `knowledge.budget.usage_level`, the same function the guard's refusal messages use, so the two cannot drift. Today's spend rides as evidence |
| `source_errors` | WARNING on a streak | the `failed_runs` rule applied to `source_call`: the last `OBSERVER_FAILURE_STREAK` calls all `error`/`rate_limited`. One failure is noise the next day's search retries; a `budget_refused` is the guard working and never counts as a failure |
| `embedding_backlog` | INFO when chunks await a first vector (`make embed`; an explain run drains up to 256 itself); WARNING when any chunk is stamped with another model (`make reembed` — retrieval cannot see those rows until it finishes) | counted against the configured `EMBEDDING_MODEL` |
| `uncited_explanations` | WARNING only when **every** explanation in the window has zero citations — retrieval is returning nothing at all. Some uncited explanations are honest: a token nobody wrote about | counted over `OBSERVER_DRIFT_WINDOW_HOURS` |

### Metric drift

For each tracked symbol, a **recent** window (`OBSERVER_DRIFT_WINDOW_HOURS`,
default 24) is compared with the **baseline** window immediately before it
(`OBSERVER_BASELINE_DAYS`, default 7). Three series:

| Metric | Statistic | Drift when |
|---|---|---|
| hourly `volume_usd` | median | ratio outside `[1/k, k]`, `k = OBSERVER_VOLUME_DRIFT_RATIO` (default 4) |
| realised volatility of hourly log returns | standard deviation, `ddof=1` | ratio outside `[1/k, k]`, `k = OBSERVER_VOLATILITY_DRIFT_RATIO` (default 3) |
| ingest run duration and `token_hours_written` | median over runs in each window | ratio above `OBSERVER_RUN_DRIFT_RATIO` (default 3) |

Ratios of medians are used rather than z-scores because hourly crypto series
are heavy-tailed; a single large hour would otherwise drive the baseline's
standard deviation and hide the drift the test exists to catch. A window with
fewer than `OBSERVER_MIN_BARS` bars (default 12) produces no drift finding at
all — "not enough data" is an `INFO` finding, never a guess. Rolling-window
rule 16 applies: a statistic is computed from a complete window or not at
all.

Drift is `WARNING`. It is not an error signal: a four-fold volume change is
news, and the right action is a human looking, not a page. Severity does not
escalate with the ratio.

## Report

```
# observer — 2026-10-25 06:15 UTC — WARNING

observer finished WARNING: 7 findings (3 warning, 4 ok). WETH: 7h gap
2026-10-24 13:00..2026-10-24 19:00 (7h) on a liquid token (mean hourly volume
$4,812,330). SHIB: median hourly volume x5.2 vs baseline ($18,402 → $95,110).
GNO: coverage 47.5% is below 80% (thin token; likely the market)

## Findings
| severity | kind     | subject | message ... |
| warning  | coverage | GNO     | coverage 47.5% is below 80% (thin token; likely the market) |
| warning  | drift    | SHIB    | median hourly volume x5.2 vs baseline (...) |
| warning  | gap      | WETH    | 7h gap ... on a liquid token (...) |
| ok       | fresh    | ingest  | last successful ingest was 4m ago |
| ok       | ledger   | ingest  | 2016 finished runs in the window: 2015 succeeded, 1 partial, 0 failed |
| ok       | drift    | all     | no volume or volatility drift across 9 symbols (limits x4 / x3) |

## Stages
| stage | status | seconds | detail |
| collect_ledger | ok | 0.04 | database_up=True, runs=2016, seconds_since_success=241 |
| ...
## Coverage
| symbol | rows | coverage | gaps | longest | mean hourly volume |
| ...
## Drift
| symbol | bars recent/baseline | volume median recent | baseline | ratio | vol recent | baseline | ratio |
| ...
## Recent ingest runs
| started | trigger | status | token hours | pool hours | secs |
| ...
```

Drift ratios are written `x5.2` rather than with a multiplication sign so
the report is plain ASCII in a terminal and in a webhook body.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `OBSERVER_INTERVAL` | `900` | Seconds between observer runs in `candlegraph agents` |
| `OBSERVER_STALE_INTERVALS` | `3` | Poll intervals without a success before data is stale |
| `OBSERVER_FAILURE_STREAK` | `3` | Consecutive failed runs that is critical |
| `OBSERVER_BASELINE_DAYS` | `7` | Baseline window |
| `OBSERVER_DRIFT_WINDOW_HOURS` | `24` | Recent window |
| `OBSERVER_MIN_BARS` | `12` | Minimum bars in a window before drift is judged |
| `OBSERVER_VOLUME_DRIFT_RATIO` | `4.0` | Median hourly volume ratio that is drift |
| `OBSERVER_VOLATILITY_DRIFT_RATIO` | `3.0` | Realised volatility ratio that is drift |
| `OBSERVER_RUN_DRIFT_RATIO` | `3.0` | Run duration or rows-per-run ratio that is drift |
| `OBSERVER_SUSPICIOUS_GAP_HOURS` | `6` | Contiguous gap length that matters on a liquid token |
| `OBSERVER_LIQUID_VOLUME_USD` | `100000` | Baseline mean hourly volume above which a token counts as liquid |

The knowledge findings deliberately add no knobs: the budget caps are
`TAVILY_MONTHLY_CREDITS`/`TAVILY_DAILY_CREDITS` (what the guard enforces),
the model is `EMBEDDING_MODEL`, the streak is `OBSERVER_FAILURE_STREAK`
and the window is `OBSERVER_DRIFT_WINDOW_HOURS` — the observer grades
against the settings the pipeline actually obeys.

## Tests

`tests/unit/agents/test_observer_assess.py` exercises every rule above with
hand-built inputs: a fresh ledger is `OK`; stale is `WARNING` and doubly
stale `CRITICAL`; a seven-hour gap on WETH is `WARNING` while the same gap on
GNO is not; volume x5 is drift and x2 is not; a thin window yields `INFO`,
never drift; backfills are excluded from run metrics; the budget at 79%,
80% and 100%; a source failure streak against single failures and refusals;
missing versus wrong-model backlog; all-uncited versus some-uncited.
`tests/unit/agents/test_observer_graph.py` runs the compiled graph against
`FakeDatabase`, including the database-down short-circuit (which must mark
the three unrun stages `skipped`), a crashing graph posting an `error`
report, and the stage records reaching the report, the ledger row and the
`on_stage` hook in order. `tests/unit/agents/test_stages.py` covers the
wrapper and the renderings on their own. `tests/integration/test_agents.py` seeds a
real database with a stale ledger and an eight-hour hole on both a liquid and
a thin token, and asserts only the liquid one is a finding.
