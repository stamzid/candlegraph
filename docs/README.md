# Design documents

Why candlegraph is built the way it is, arranged by the part of the system
each decision belongs to. The layout mirrors `.claude/skills/`: one directory
per topic, one document inside it. A skill tells you *how to operate*
something; the document here tells you *why it is that shape*.

```
docs/<area>/<topic>/DESIGN.md
docs/incidents/<date>-<slug>/POSTMORTEM.md
docs/rollout/ROLLOUT.md
```

Incidents are the exception to "why, not how": a post-mortem records the
timeline, the root causes, and the follow-ups, and its Status closes when
every follow-up has landed or been explicitly declined.

The rollout plan is the other exception: it is *how and in what order*,
sequencing decisions that the design documents already made. There is one
current rollout (`rollout/ROLLOUT.md`, Status `current`); its progress
table is updated every working session, and a finished rollout is archived
under `rollout/archive/` when the next one takes its place. A rollout step
never changes a design silently — it changes the design document in the
same step or it follows it.

| Document | Status | Covers |
|---|---|---|
| [`roadmap/`](roadmap/DESIGN.md) | living | What has been delivered, what comes next, and the exit criterion for each stage. |
| [`storage/timescaledb/`](storage/timescaledb/DESIGN.md) | implemented | Adopting the TimescaleDB extension: the image, the migration, and the policies deliberately left off. |
| [`storage/table-placement/`](storage/table-placement/DESIGN.md) | implemented | Which tables are hypertables and which stay plain, with the rule for deciding the next one. |
| [`storage/pgvector-coexistence/`](storage/pgvector-coexistence/DESIGN.md) | in progress: image switch and corpus tables done | How pgvector sits beside TimescaleDB in the one instance: the four table families, the query paths side by side, and what changes operationally. |
| [`rollout/`](rollout/ROLLOUT.md) | current | **The current rollout plan** — ordered, turn-sized steps for stage 1 with a progress table. One current rollout at a time; finished ones move to `rollout/archive/`. |
| [`knowledge/graph/`](knowledge/graph/DESIGN.md) | in progress: schema, asset sync and documents done | Stage 1: the knowledge schema, time-series versus relational placement, the retrieval and explanation flow, and how it attaches to the observer and janitor. |
| [`knowledge/vector-store/`](knowledge/vector-store/DESIGN.md) | proposed | pgvector in the existing database, compared with Qdrant, Chroma, Neo4j and Apache AGE; the image change it implies and the triggers that would revisit it. |
| [`knowledge/embeddings/`](knowledge/embeddings/DESIGN.md) | proposed | Embedding models compared (OpenAI, Voyage, Cohere, Gemini, bge-m3, Qwen3, nomic, …); 1024 dimensions fixed; a local free default; reuse in the harness. |
| [`knowledge/tavily/`](knowledge/tavily/DESIGN.md) | proposed | Web search as a metered source: credit budget guard, `(asset, day)` cache, the `source_call` ledger, REST rather than MCP. |
| [`knowledge/mcp/`](knowledge/mcp/DESIGN.md) | proposed | A read-only MCP server over existing functions, useful now for operating the stack; agents as MCP clients later; not for Tavily. |
| [`analytics/models/`](analytics/models/DESIGN.md) | implemented | The statistics and models, with equations: returns and annualisation, rolling statistics, Bollinger, crossover, ARIMA, pairwise correlation and beta, Sharpe/Sortino/drawdown, Markowitz portfolios and the frontier, the backtester. |
| [`analytics/risk/`](analytics/risk/DESIGN.md) | implemented; Tavily rate source planned | Risk management: systematic versus idiosyncratic risk, VaR and CVaR (three estimators), how to tune them, and the nominal/real risk-free rate seam. |
| [`agents/framework/`](agents/framework/DESIGN.md) | implemented | The conventions every LangGraph agent follows: state, nodes, findings, narration, sinks, scheduling, testing. |
| [`agents/observer/`](agents/observer/DESIGN.md) | implemented | The run observer: stale data, gaps that matter, metric drift. |
| [`agents/janitor/`](agents/janitor/DESIGN.md) | implemented | The CI janitor: run the checks, apply mechanical fixes, draft a PR. |
| [`incidents/2026-10-28-stack-would-not-start/`](incidents/2026-10-28-stack-would-not-start/POSTMORTEM.md) | closed | Post-mortem: the stack would not start after the TimescaleDB move. Two faults in one volume, why neither named itself, and what changed. |
| [`incidents/2026-10-29-compose-startup-robustness/`](incidents/2026-10-29-compose-startup-robustness/POSTMORTEM.md) | closed | Post-mortem: the API container sometimes would not start until Docker was cleaned. Five causes in the compose file; bounded restarts, one image per project, one migrator, TCP probe, one build. |
| [`incidents/2026-10-29-health-probe-cadence/`](incidents/2026-10-29-health-probe-cadence/POSTMORTEM.md) | closed | Post-mortem: the API log was nothing but health probes. By design, wasteful; start-up versus steady-state cadence, one round trip, quiet when healthy. |

Read the document for an area before changing the code it describes, and
update it in the same change — the definition of done in `AGENTS.md` asks
for exactly that.

## Writing one

- Open with **Status** (`proposed`, `implemented`, `superseded`, `living`)
  and the date, so a reader knows whether it describes the code or an idea.
- State the decision, then the reasoning, then **what was rejected and why**.
  The rejected alternatives are usually the most useful part a year later.
- Include a diagram. A `mermaid` flowchart of the control flow, the data
  flow, or the decision tree is worth several paragraphs, and GitHub renders
  it inline. Where a diagram describes a graph that exists in code, pin it
  with a test so the two cannot drift.
- Name the invariant in `AGENTS.md` that the design protects or adds.
- Keep operating instructions in the matching skill, not here. If you find
  yourself writing a command sequence, it belongs in `.claude/skills/`.
