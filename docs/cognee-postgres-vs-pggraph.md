# Native Cognee Postgres vs. the pgGraph adapter

A working comparison for the `skc` showcase. It documents what Cognee gives you
natively on Postgres, what the community **pgGraph** adapter adds, where the
adapter is still embryonic, and what that means for choosing a host.

> Sources: Cognee core `cognee/infrastructure/databases/graph/postgres/adapter.py`
> and the community adapter at
> `topoteretes/cognee-community/packages/graph/pggraph/`. Status as of 2026-05.

---

## 1. The three storage roles in Cognee

Cognee always needs three logical stores. On an all-Postgres stack they collapse
into one database:

| Role | What it holds | Postgres mechanism |
|------|---------------|--------------------|
| Relational | datasets, documents, pipeline state | SQLAlchemy tables |
| Vector | embeddings for semantic search | **pgvector** (`vector` extension) |
| Graph | entities + relationships | `graph_node` / `graph_edge` tables |

`skc` uses Postgres for all three (see `docker/docker-compose.yml`, which uses
the `pgvector/pgvector:pg16` image).

---

## 2. Native Cognee graph-on-Postgres (`PostgresAdapter`)

Cognee ships a first-party Postgres **graph** adapter
(`GraphDBInterface` → `PostgresAdapter`). It is "graph-as-tables":

- **Schema:** two tables, `graph_node(id, name, type, properties jsonb, …)` and
  `graph_edge(source_id, target_id, relationship_name, properties jsonb, …)`.
- **CRUD:** batch upsert of nodes/edges, typed accessors (`get_nodes`,
  `get_neighbors`, …), `is_empty`, `delete_graph`. Deadlock-retry via `tenacity`.
- **Traversal:** recursive-CTE SQL over `graph_edge` (the
  `WITH RECURSIVE neighborhood(...)` pattern).
- **Raw queries:** **not supported** — `query()` raises `NotImplementedError`
  ("use a graph-native backend like Neo4j/Ladybug, or the typed methods").

### Native graph backend options (for context)

Cognee core also bundles graph adapters for **kuzu (default), ladybug, neo4j,**
and **neptune**, plus vector adapters including **pgvector, lancedb, chromadb**.
So "native Postgres graph" is one of several first-party choices — chosen here
because it keeps everything in a single Postgres instance.

**Strengths of native Postgres graph:** zero extra services, transactional with
your relational + vector data, trivially hostable on any managed Postgres,
correct for all of Cognee's typed graph operations.

**Native limitation:** traversal is recursive-CTE SQL. It is correct but does
not scale to deep, high-fan-out multi-hop traversals the way a graph-native
engine (or a graph index) does, and there is no Cypher/openCypher surface.

---

## 3. What the pgGraph adapter adds

`PgGraphAdapter` **subclasses** the native `PostgresAdapter`. It does **not**
replace storage — it layers [pgGraph](https://github.com/Evokoa/pgGraph) (the
Postgres `graph` extension) as a **derived traversal index** over the same
`graph_node`/`graph_edge` tables.

| Aspect | Behavior |
|--------|----------|
| Storage / CRUD | **Inherited unchanged** from `PostgresAdapter` (same tables). |
| `initialize()` | Checks `pg_available_extensions`; if `graph` is present, `CREATE EXTENSION`, registers the node/edge tables with pgGraph, and runs `graph.build()`. |
| `get_neighbors` / `get_neighborhood` | **Overridden** to use pgGraph's `graph.traverse()` (BFS, depth, edge-type filters) — accelerated. |
| Fallback | If the extension is absent **or any traversal errors**, it falls back to the native recursive-CTE SQL. Results match; only speed differs. |
| `build_graph()` | Exposes pgGraph's `graph.build()` to (re)materialize the index. |
| Build modes | `PGGRAPH_BUILD_MODE` = `manual` (default), `on_write`, `scheduled`. |
| Activation | `register()` → `use_graph_adapter("pggraph", PgGraphAdapter)`, then `set_graph_database_provider("pggraph")`. |

**The value proposition:** keep the all-Postgres simplicity, but get
graph-native traversal performance for neighbor/neighborhood queries when the
extension is installed — and degrade gracefully to plain SQL when it isn't.

---

## 4. Gaps in the embryonic pgGraph adapter

These are the honest rough edges to flag in the showcase. Each is a real
limitation today, with the practical impact.

| # | Gap | Impact |
|---|-----|--------|
| G1 | **Needs a custom Postgres build.** pgGraph is a C extension; stock images (incl. the community demo's `postgres:16` and our `pgvector/pgvector:pg16`) don't ship it. | Out of the box the adapter runs in **SQL-fallback mode** — correct, but no acceleration. Real benefit requires a Postgres image with pgGraph compiled in. |
| G2 | **Hosting.** Managed Postgres (RDS, Cloud SQL, Neon, Supabase) only allows an allow-listed set of extensions; arbitrary C extensions like pgGraph are generally **not installable**. | The accelerated path effectively requires self-managed Postgres (a VM/container you control) — see §6. This is the single biggest adoption blocker. |
| G3 | **Only neighbor/neighborhood traversal is accelerated.** Other graph operations stay on inherited SQL. | Cognee search types that lean on `get_neighbors`/`get_neighborhood` benefit; anything using other access patterns sees no change. |
| G4 | **No raw query / Cypher.** Inherited `NotImplementedError` from `PostgresAdapter`. | Tooling or retrievers expecting Cypher won't work; you're limited to the typed adapter methods. |
| G5 | **Manual build by default.** With `PGGRAPH_BUILD_MODE=manual`, the index isn't refreshed after writes. | The pgGraph index can go **stale** relative to `graph_node`/`graph_edge`. `on_write` fixes correctness but rebuilds on every write (expensive); `scheduled` defers the build but the adapter itself doesn't run the scheduler — you must call `build_graph()` out of band. |
| G6 | **Self-disables on first error.** On any traversal exception it sets `_pggraph_ready = False` for the rest of the adapter's lifetime. | Robust (no error storms) but "sticky": once disabled you silently lose acceleration until the adapter is recreated and re-initialized. |
| G7 | **Not a hybrid adapter.** It's graph-only; vectors still live in pgvector separately. | No single-store vector+graph traversal; cross-store joins happen in Cognee, not in Postgres. |
| G8 | **Young upstream + experimental status.** pgGraph's SQL API surface is small and evolving; the adapter is explicitly marked experimental and not yet upstreamed into `topoteretes/cognee`. | API churn risk; pin versions and watch for a future native `GRAPH_DATABASE_PROVIDER=pggraph`. |

---

## 5. Decision guide

- **Want zero ops / managed host / "just works":** use **native Postgres graph**
  (`GRAPH_DATABASE_PROVIDER=postgres`). Accept recursive-CTE traversal.
- **Want graph-native traversal speed but a Postgres-shaped stack, and can run
  your own Postgres:** use **pggraph** with a pgGraph-enabled image. Falls back
  to native SQL automatically, so it's a safe default to develop against.
- **Need Cypher / very deep traversals / mature graph features now:** use a
  graph-native backend (**neo4j**, **kuzu**, **ladybug**) instead — at the cost
  of an extra service and losing single-database transactionality.

For `skc` we use **pggraph**: it demonstrates the adapter, runs correctly in
fallback mode on the local pgvector image, and lights up acceleration the moment
we point it at a pgGraph-enabled Postgres.

---

## 6. Hosting the pgGraph-enabled Postgres (open task)

Because of G1/G2, the accelerated path needs a Postgres where we control
extensions. Candidates to evaluate (decision pending):

| Option | pgGraph installable? | Notes |
|--------|----------------------|-------|
| Self-managed VM (Fly.io / Hetzner / EC2) + custom image | ✅ | Full control; we build a `Dockerfile FROM pgvector/pgvector:pg16` that compiles pgGraph. Most likely path. |
| Container host (Railway / Render) with custom image | ✅ (likely) | Easier ops than a raw VM if they accept a custom Postgres image. |
| Neon | ❌ (custom C ext) | Great for native Postgres graph + pgvector only. |
| Supabase | ⚠️ allow-listed extensions only | pgvector yes; pgGraph not in the allow-list. |
| AWS RDS / Aurora / Cloud SQL | ❌ | No arbitrary C extensions. |

**Next step:** produce a `docker/Dockerfile.pggraph` that layers the pgGraph
extension onto `pgvector/pgvector:pg16`, validate it locally (so `_pggraph_ready`
flips to `true`), then pick a host from the ✅ rows above.

---

## 7. How `skc` exercises this

1. `docker/docker-compose.yml` → Postgres (pgvector) for relational + vector +
   graph tables.
2. `.env` sets `GRAPH_DATABASE_PROVIDER=pggraph` and the `GRAPH_DATABASE_*` /
   `DB_*` connection vars the adapter reads.
3. The graph stage (M5) calls `register()` + `set_graph_database_provider`,
   loads the classified corpus, and queries via `get_neighborhood` — which uses
   pgGraph when available and SQL otherwise.
4. This doc is the reference for the "native vs. pgGraph" story in the writeup.
