# Design documents

Why candlegraph is built the way it is, arranged by the part of the system
each decision belongs to. The layout mirrors `.claude/skills/`: one directory
per topic, one document inside it. A skill tells you *how to operate*
something; the document here tells you *why it is that shape*.

```
docs/<area>/<topic>/DESIGN.md
```

| Document | Status | Covers |
|---|---|---|
| [`roadmap/`](roadmap/DESIGN.md) | living | What has been delivered, what comes next, and the exit criterion for each stage. |
| [`storage/timescaledb/`](storage/timescaledb/DESIGN.md) | implemented | Adopting the TimescaleDB extension: the image, the migration, and the policies deliberately left off. |
| [`storage/table-placement/`](storage/table-placement/DESIGN.md) | implemented | Which tables are hypertables and which stay plain, with the rule for deciding the next one. |
| [`agents/framework/`](agents/framework/DESIGN.md) | implemented | The conventions every LangGraph agent follows: state, nodes, findings, narration, sinks, scheduling, testing. |
| [`agents/observer/`](agents/observer/DESIGN.md) | implemented | The run observer: stale data, gaps that matter, metric drift. |
| [`agents/janitor/`](agents/janitor/DESIGN.md) | implemented | The CI janitor: run the checks, apply mechanical fixes, draft a PR. |

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
