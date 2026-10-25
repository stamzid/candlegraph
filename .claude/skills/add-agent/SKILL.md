---
name: add-agent
description: Add a new LangGraph agent graph to candlegraph following the framework conventions — typed state, pure assess functions, findings, report sinks, scheduling, and tests. Use when asked to add an agent, a graph, a new kind of report, or a scheduled check.
---

# Add an agent

Read `docs/agents/framework/DESIGN.md` first; it explains *why* each step below
exists. The observer (`candlegraph/agents/observer/`) is the reference
implementation for a reader; the janitor (`candlegraph/agents/janitor/`) for
a graph that runs commands.

## 1. Decide what it finds, not what it does

Write the finding kinds and their severities before any code, as a table in
`docs/agents/<name>/DESIGN.md` (copy the structure of
`docs/agents/observer/DESIGN.md`, status `proposed`). Include a `mermaid`
flowchart of the graph and one of the severity decisions — the observer
document has both, and a test pins the first so it cannot drift. If a finding cannot be stated as one sentence with a number
behind it, it is not a finding yet. Remember: `OK` findings are findings too
— the report has to show what was checked.

## 2. Lay out the package

```
candlegraph/agents/<name>/
  __init__.py     exports build_<name>_graph, run_<name>
  state.py        <Name>State(TypedDict, total=False) and a Thresholds dataclass
  collect.py      nodes that read (database, shell); each catches its own errors
  assess.py       pure functions: collected facts + thresholds -> list[Finding]
  graph.py        wiring, the publish node, run_<name>()
```

Rules that the tests will hold you to:

- Accumulating fields (`findings`, `errors`) are
  `Annotated[list[T], operator.add]`. Everything else is set by one node.
- Nodes are `def node(deps, state) -> dict[str, Any]`, bound with
  `functools.partial(node, deps)` in `graph.py`. `deps` is a dataclass
  holding the database, shell, narrator, sinks, run id, and start time.
- `assess.py` imports nothing that does I/O. If it needs the clock, the
  clock is in the state.
- Reads go through `DatabaseManager` and existing `Statements`. New SQL goes
  in `storage/statements.py` with a name, never inline.
- Commands go through `agents.shell.Shell`. Never `subprocess` in a node.
- The last node is `publish`: `finish_report(...)` then `publish(report, deps.sinks)`.
- `run_<name>()` wraps `graph.invoke` in `try/except`, and on failure posts a
  report with `error=` set. Invariant 21: a crashed agent still records.

## 3. Thresholds and configuration

Every knob is a field on the Thresholds dataclass with a `from_settings`
classmethod, and a `<NAME>_*` field in `settings.py` with a comment and a
`.env.example` entry. Tests build thresholds by hand, never from settings.

## 4. Register it

- `candlegraph/agents/__init__.py`: add to `AGENTS`.
- `candlegraph/agents/scheduler.py`: add a `<name>_tick` and a job gated on
  `<NAME>_INTERVAL` (0 disables).
- `candlegraph/cli.py`: a one-shot command with `--strict`, `--json`,
  `--sinks`, dispatched in `main()`.
- `Makefile`: a target. Never document a command that is not a target.

## 5. Test it in the fastest tier that proves it

`tests/unit/agents/test_<name>_assess.py` — every rule in your findings
table, with hand-built inputs, including the boundary (ratio exactly at the
threshold) and the "not enough data" path.

`tests/unit/agents/test_<name>_graph.py` — the compiled graph against
`FakeDatabase` / `FakeShell`: a healthy run is `OK` and writes one
`insert_agent_run`; each short-circuit edge; a crashing graph posts an error
report; and `test_the_graph_has_the_documented_shape`, which pins the node
and edge set so the Mermaid diagram in the doc cannot drift.

`tests/integration/test_agents.py` — one end-to-end run against a seeded
real database if the graph has SQL the fake cannot prove.

Check the unit tier made no network call and needed no database:

```sh
make test-unit
```

## 6. Finish

- `docs/agents/<name>/DESIGN.md` status → `implemented`, and its Mermaid
  flowchart matches `test_the_graph_has_the_documented_shape`.
- `docs/README.md` table, `AGENTS.md` (commands, layout), `README.md`
  (Agents section, commands table), `.claude/skills/run-agents` (how to read
  its findings).
- `make check`, then the `release-check` skill.
