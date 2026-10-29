# Vector store: pgvector in the database we have, and what would change that

**Status:** proposed — 2026-10-29
**Decision:** pgvector (with pgvectorscale available) inside the existing
PostgreSQL/TimescaleDB. No second store. Revisit triggers are listed at the
end so this is a decision with an expiry condition, not a preference.
**Companion:** [`../graph/DESIGN.md`](../graph/DESIGN.md) for what is stored.

## The question

Stage 1 needs (a) similarity search over text chunks, (b) a small typed
graph between assets, documents and sources, and (c) both joined to the
prices and signals already in PostgreSQL, under the "one database" argument
in `docs/storage/timescaledb/DESIGN.md`. The candidates split into three
families: an extension in the database we run, a dedicated vector engine,
and a graph database with a vector index.

## Comparison

Figures are from vendor documentation and public benchmarks as of this
document's date and should be re-checked before being relied on; the shape
of the comparison is what matters.

| | **pgvector** (+ pgvectorscale) | **Qdrant** | **Chroma / LanceDB** | **Neo4j** (Community) | **Apache AGE** |
|---|---|---|---|---|---|
| What it is | PostgreSQL extension: `vector`/`halfvec` types, HNSW and IVFFlat indexes; pgvectorscale adds a DiskANN-style index and label filtering | Standalone vector engine in Rust, HTTP/gRPC | Embedded libraries (Python process or local files) | Property-graph database with a native vector index since 5.11 | PostgreSQL extension adding openCypher over graph tables |
| Local, free | yes, same container | yes, one more container | yes, in-process | yes, JVM container, ~1 GB RAM to be comfortable | yes, extension |
| Joins to prices/signals | native SQL join, one transaction | application-side; ids round-trip | application-side | application-side, or duplicate the data | native SQL; Cypher for traversals |
| Filtered ANN (by asset, by `published_at`) | HNSW with post-filter; pgvectorscale and 0.8's iterative scans fix the low-recall-under-filter case | first-class payload filtering, very good | basic | good | as pgvector |
| Hybrid lexical + dense | `tsvector` + GIN beside the vector column; RRF in SQL | sparse vectors supported | limited | full-text index + vector index | as pgvector |
| Scale where it strains | tens of millions of vectors on one node | billions, sharded | millions, single process | millions of nodes; vector index is not its strength | as pgvector |
| Operational cost | none new: same backups, same migrations, same Alembic | second service, second backup, consistency is ours | none, but a second source of truth on disk | second service, JVM tuning, two query languages | one extension, but Cypher is a second language in the repo |
| Graph traversal | recursive CTEs; fine for 1–3 hops | none | none | the point of it; arbitrary-depth traversal, path queries | Cypher, in Postgres |
| Testability under our rules | existing testcontainer, existing `FakeDatabase` | second container in the integration tier | in-process, easy | second container | existing container if the image has it |
| Fit for our image | **not in `timescale/timescaledb` (Alpine)**; in `timescale/timescaledb-ha` (Debian) with pgvectorscale and pgai | n/a | n/a | n/a | not in either Timescale image; custom build |

### How the graph is actually used

The decisive input is the query shape. Every retrieval in stage 1 is:

```
chunks ─(similarity or lexical)→ ranked ids
  → join document for url, published_at
  → join mention/asset to filter by asset
  → bound by published_at ≤ bar
```

That is two joins and a window predicate: SQL territory. The "graph" edges
(`trades_in`, `correlated_with`, `co_mentioned`) are read as adjacency —
"what is one hop from WETH" — which a b-tree on `(from_id, relation)`
answers in microseconds. Nothing in stages 1 to 5 asks for variable-depth
paths, shortest paths, or community detection, which are the questions
that justify a graph engine.

### Why not Neo4j, specifically

It is the strongest alternative because it answers (b) better than anything
else. It loses on (c): the prices, signals, ledgers and the hypertables are
in PostgreSQL and are staying there, so Neo4j means either duplicating the
entities that matter into a second store or doing every explanation as two
queries in two languages with the join in Python. For a graph that is two
hops deep and derived from PostgreSQL tables, that is paying the
consistency cost of two databases for traversals we do not run. The
revisit trigger below says exactly when that changes.

### Why not Qdrant

It is the best pure vector engine in the list, and filtered search is
genuinely better than pgvector's. But the dataset is small (a few thousand
documents a month at stage 1), filters are two columns, and the cost is a
second service whose contents have to agree with `document` rows in
PostgreSQL. Provenance — "this citation exists in the database that holds
the signal" — is easier to guarantee with a foreign key than with two
writes.

## The decision in detail

- **Column type `halfvec(1024)`** rather than `vector(1024)`: half the
  storage and index size, negligible recall difference at this dimension.
  The dimension is fixed by the column, which is the point: one active
  embedding model per deployment (`../embeddings/DESIGN.md`).
- **HNSW index**, `m = 16`, `ef_construction = 64`, cosine distance.
  IVFFlat needs training data that changes as the corpus grows; HNSW does
  not. Build time is irrelevant at this size.
- **A generated `tsvector` column with a GIN index** beside the vector, so
  hybrid retrieval is one SQL statement with two CTEs and a reciprocal rank
  fusion `ORDER BY`.
- **Filtering by `published_at` and asset happens in SQL.** pgvector 0.8's
  iterative index scans mean a selective filter no longer silently returns
  too few rows; pgvectorscale's label-filtered index is available if a
  measured query needs it.
- **pgvectorscale is installed but not required.** Its StreamingDiskANN
  index is what we would reach for at millions of chunks. It arrives with
  the image and costs nothing until used.
- **Plain tables, not hypertables**, for `document` and `chunk`. The ANN
  index has to be global; the table-placement document records it.

## The image change this implies, and the incident it rhymes with

pgvector is **not** in the Alpine `timescale/timescaledb:2.31.0-pg17` image
the stack runs today. The clean path is `timescale/timescaledb-ha:pg17-ts2.31`
(pinned to an exact tag at the time), which bundles TimescaleDB, pgvector,
pgvectorscale and pgai on Debian.

That is a base-image change under a persistent volume, which is exactly the
class of change that caused `docs/incidents/2026-10-28-stack-would-not-start/`.
The lessons from it apply directly:

1. The switch goes from Alpine/musl back to Debian/glibc, so **existing
   volumes must be rebuilt**, not carried across: the collation version
   recorded in the catalogue will not match the new C library.
2. The migration that runs `CREATE EXTENSION vector` gets a **preflight
   check** that the extension is *available*, in the same place and with
   the same kind of message as the TimescaleDB one.
3. The image tag is pinned in **compose, CI and the testcontainer** at once,
   and `test_the_database_image_is_pinned_identically_everywhere` already
   fails if they drift.
4. It is done **now, while nothing is deployed**, which is the same argument
   that justified the TimescaleDB move itself.

The alternative — building pgvector into the Alpine image ourselves — keeps
the image small but makes us the maintainer of a PostgreSQL build, and loses
pgvectorscale. Not worth it.

## Revisit triggers

Change this decision when one of these is measured, not before:

| Trigger | Move to |
|---|---|
| retrieval queries need variable-depth traversal (paths between assets through documents, communities of co-mentioned assets) | Apache AGE first (stays in PostgreSQL); Neo4j if AGE's maturity is the problem |
| `chunk` passes ~10 M rows, or p95 retrieval latency with filters exceeds what the UI tolerates after pgvectorscale | a dedicated engine (Qdrant) fed from PostgreSQL, with PostgreSQL remaining the source of truth |
| the corpus must be shared with a system that is not this one | a dedicated engine with its own API |

## Rejected outright

- **A hosted vector service** (Pinecone and similar). Conflicts with
  "local free deployment first", and the data would leave the machine.
- **Milvus / Weaviate.** Capable, and far more infrastructure than the
  problem has.
- **Storing embeddings as `float[]` and scanning.** Works to a few thousand
  rows and then does not, with no index path forward.
