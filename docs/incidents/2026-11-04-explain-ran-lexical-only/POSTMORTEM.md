# Post-mortem: `signals --explain` ran lexical-only on every default stack

**Status:** closed — 2026-11-04. Not an outage: the command answered, the
answer was weaker than designed, and nothing said so loudly enough.
**Severity:** medium. No data was lost or corrupted. Every explanation
produced on a stack started with `make up` was built from the lexical
retrieval leg alone, and news fetched by the command itself could never be
cited by the dense leg at all.

## Executive summary

Step 5 of the stage-1 rollout put the `ollama` embedding service behind a
compose **profile** (`knowledge`), so that `make up` would not download
1.2 GB of model weights for a stack that was only ingesting. `make
up-knowledge` opted in. Step 8 then shipped `signals --explain`, whose
dense retrieval leg needs that service. On every stack started the usual
way the embedder was unreachable, `retrieve()` did what it was designed
to do — fall back to the `tsvector` leg and note it — and the explanation
was printed with a one-line caveat at the bottom. Nobody read the caveat.

Two faults compounded:

1. **The optional service was needed by a default command.** A profile is
   the right tool for a service that only a subset of operators want. The
   embedder stopped being optional the moment `--explain` landed, and the
   rollout step that made it mandatory did not revisit the decision that
   had made it optional.
2. **The pipeline never embedded what it fetched.** `fetch_news` stores
   documents and leaves their chunks on the embedding backlog for
   `make embed`. `explain` called it and then retrieved immediately, so
   even with Ollama running, the day's news was invisible to the dense leg
   until an operator ran a second command the explain output did not
   mention.

The degradation was silent because it was *correct*: the lexical fallback
exists so an explanation is never refused for want of a model server. The
fix keeps the fallback and removes the two reasons it fired on every run.

## What changed

| | Before | After |
|---|---|---|
| Ollama in `make up` | no (`profiles: ["knowledge"]`) | yes; `make up-knowledge` is an alias kept for old notes |
| First `make up` | fast; no model | pulls ~1.2 GB once into the `ollama` volume; `--wait` holds until `ollama show <model>` succeeds; later starts are instant |
| App containers and Ollama | independent | still independent: no `depends_on`, the API and worker start without it and retrieval degrades to lexical, saying so (`test_the_embedding_service_is_in_the_default_stack_…`) |
| News fetched by `explain` | stored unembedded; dense leg blind to it until `make embed` | `explain` embeds up to 256 pending chunks before retrieving; the output reports how many |
| Embedder unreachable during `explain` | one caveat line at the bottom | still one line, now also naming the failed embedding step, and the first `make up` failure prints the Ollama log |

## Why not keep the profile and document it harder

Because the default command of the stage would then depend on the operator
remembering a second command, and the failure mode would still be silent.
The cost moved to where it is paid once — the first `make up` — rather than
on every explanation. `make reset` still deletes exactly one database
volume and one image; the model volume is untouched by it, so a schema
rebuild does not re-download the weights.

## What would have caught it earlier

- The observer's `embedding_backlog` finding (rollout step 9) would have
  reported a growing backlog on a stack with no embedder. It is the next
  step.
- An end-to-end test that runs `--explain` against the compose stack and
  asserts `dense_used`. `make e2e` has the harness; the assertion is added
  with step 10's closeout.

## Changes

`docker-compose.yaml` (profile removed, comment), `Makefile` (`up` prints
the Ollama log on failure; `up-knowledge` aliases `up`; `logs-ollama`),
`candlegraph/knowledge/explain.py` (`EXPLAIN_EMBED_LIMIT`, the embed step,
`chunks_embedded` on the result), `tests/unit/test_compose_stack.py`,
`tests/unit/knowledge/test_explain.py`, and the documents that described
the profile: `docs/knowledge/embeddings/DESIGN.md`, `AGENTS.md`,
`CLAUDE.md`, `README.md`, `.env.example`.
