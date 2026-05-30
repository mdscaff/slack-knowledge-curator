# Devlog — building a Cognee + pgGraph knowledge graph

A chronological log of decisions, learnings, dead-ends, and "aha" moments while
building the Slack Knowledge Curator. **Purpose:** raw material for a series of
blog posts on using Cognee to build a knowledge graph and on being a power user
of the community **pgGraph** adapter. Write entries as you go; prune later.

Each entry: what we did, *why*, what we learned, and any blog-worthy hook.

---

## 2026-05-30 — Day 1: shape of the thing

### Decisions
- **Goal:** turn a personal Slack "save it for later" channel into a queryable
  knowledge graph, and use it as a public **showcase for Cognee + pgGraph**.
- **Stack** (confirmed with stakeholder): Python 3.11; Slack Web API ingestion;
  X.com enrichment via free **oEmbed** (no key); **Claude** for summarize +
  classify; **emergent** (LLM-discovered) taxonomy; **Cognee** for the graph
  using the community **pgGraph** adapter; **all-Postgres** stack (relational +
  pgvector + graph tables) in local Docker; **OpenAI** embeddings.
- **Architecture:** five decoupled stages, each reading the previous stage's
  JSONL artifact (`ingest → enrich → classify → taxonomy → graph`). Resumable,
  debuggable, and every stage runs standalone.

> **Blog hook:** "Why I built my knowledge pipeline as five JSONL-passing CLI
> stages instead of one monolith" — the resumability/idempotency story.

### Built
- Project scaffold: `pyproject` (base deps + optional `[graph]` extra so the
  heavy Cognee install is opt-in), `typer` CLI, `pydantic-settings` config,
  the `Item` model that flows through every stage, JSONL store, link extractor.
- **M1 ingest** — Slack `conversations.history` + thread expansion + user-name
  resolution + incremental cursor. Verified: CLI, status table, clean no-token
  error, link extraction, 3 unit tests.
- **M2 enrich** — concurrent link resolution. X/Twitter via oEmbed (flatten the
  HTML blockquote to plain text + author); other URLs via a light `<title>` +
  meta-description grab. Each distinct URL fetched once, fanned out to every
  message; idempotent (reuse prior `ok`). Verified live: pulled jack's first
  tweet ("just setting up my twttr") and a web page title.

### Learnings
- **oEmbed is the cheap unlock.** X's paid API tiers are steep, but the public
  oEmbed endpoint (`publish.twitter.com/oembed`) returns author + tweet HTML
  with no auth. Flattening the blockquote HTML to text is enough for
  classification. Caveat: deleted/private tweets fail — we flag, don't crash.

> **Blog hook:** "Enriching X.com links without paying for the X API."

### Cognee 1.1.1 compatibility audit (released 2026-05-29)
Cognee shipped **1.1.1** the day before — a 1.x line with real breaking changes.
We audited the pgGraph adapter against the `v1.1.1` source before trusting it.

**Verdict: the pgGraph adapter is structurally compatible with cognee 1.1.1.**

What we checked and why it holds:
| Adapter dependency | v1.1.1 status |
|--------------------|---------------|
| `use_graph_adapter(name, adapter)` | Same signature; registers into `supported_databases`. ✓ |
| `graph/__init__` exports `use_graph_adapter`, `get_graph_engine` | Still exported. ✓ |
| `PostgresAdapter(connection_string=...)` + inherited methods (`add_nodes`, `add_edges`, `delete_graph`, `get_neighbors`, `get_nodes`, `get_neighborhood`, `_session`, `_parse_node_row`, `initialize`) | All present. ✓ |
| **"IDs → UUIDs" breaking change (1.1.1)** | `graph_node.id` / `source_id` / `target_id` are **still `String`** in `tables.py`. UUIDs are stored as their string form, so our text-based traversal SQL is unaffected. ✓ |
| Factory custom-adapter branch | Now also passes a **new `graph_database_key` kwarg** → harmlessly absorbed by the adapter's `**kwargs`. ✓ |

Behavioral changes to keep an eye on (not breakage):
- **`_GraphEngineHandle`** — the factory now returns a caching proxy, not the
  adapter directly. Attribute access (`graph._pggraph_ready`, `build_graph()`)
  proxies through `__getattr__`, and `__class__` reports the real adapter, so the
  example still works. Worth a runtime confirm.
- **Multi-user Postgres graph** (1.1.0): new `PostgresGraphDatasetDatabaseHandler`
  + `GRAPH_DATASET_DATABASE_HANDLER` env (default `ladybug`). Single-user mode
  (`ENABLE_BACKEND_ACCESS_CONTROL=false`) should bypass this — confirm at runtime.
- The factory passes **no `graph_database_host`** to custom adapters, so the
  adapter falls back to relational `DB_*` env for its connection. That's why
  `.env` sets both `GRAPH_DATABASE_*` and `DB_*`.

**Actions:**
- Pin our project's graph extra to `cognee>=1.1.1,<2` (the version we audited).
- **Upstream opportunity:** the community package still pins `cognee>=0.5.5`.
  Bumping/validating it for 1.1.x is a good community PR — and exactly the kind
  of "pgGraph maintainer" contribution that builds the brand.
- TODO: run the adapter's `example.py` against cognee 1.1.1 on the Docker
  Postgres to turn this source audit into a runtime guarantee.

### Gotcha: the pgGraph adapter isn't published to PyPI
Tried `pip install cognee-community-graph-adapter-pggraph` → **"not found in the
package registry."** The adapter only exists inside the `cognee-community`
monorepo; there's no PyPI distribution. So consumers must install from the git
subdirectory:

```
pip install "cognee-community-graph-adapter-pggraph @ \
  git+https://github.com/topoteretes/cognee-community.git#subdirectory=packages/graph/pggraph"
```

Updated our `[graph]` extra accordingly. **Upstream opportunity #2:** publishing
the adapter to PyPI (with a proper version) would make it dramatically easier to
adopt — a high-leverage, low-effort community contribution and a natural thing
for "the pgGraph expert" to own.

> **Blog hook:** "The community adapter that wasn't on PyPI — packaging a Cognee
> graph adapter so others can actually `pip install` it."

### Runtime verification — PASS ✅
Installed `cognee==1.1.1` + the adapter (from git) into an isolated venv and ran
`scripts/verify_pggraph_cognee111.py` against a throwaway `pgvector/pgvector:pg16`
Postgres (port 5434, removed after). Result:

```
cognee 1.1.1
adapter class: PgGraphAdapter
pgGraph ready: False (False == SQL fallback)
build_graph: None
neighbors of turing: ['Bletchley Park', 'Cryptography']
2-hop node ids: ['bletchley', 'crypto', 'turing']
edges in subgraph: 2
RESULT: PASS ✅
```

This confirms at runtime what the source audit predicted:
- The factory's new **`_GraphEngineHandle`** proxy is transparent — `graph.__class__`
  correctly reports `PgGraphAdapter` and method calls proxy through.
- The new **`graph_database_key`** kwarg is absorbed harmlessly.
- **SQL fallback works** end-to-end on stock Postgres (no `graph` extension):
  neighbor + 2-hop neighborhood traversal return correct results.

So: **the pgGraph adapter is verified working on cognee 1.1.1.** When we point it
at a pgGraph-enabled Postgres, `pgGraph ready` should flip to `True` and the same
calls use `graph.traverse()`.

> **Blog hook:** "From source audit to green checkmark: runtime-verifying a graph
> adapter against a same-week major release."

> **Blog hook:** "When a major dependency ships overnight: how to audit a graph
> adapter against a new release in 30 minutes (and the one breaking change that
> *didn't* bite us)."

### Open threads
- pgGraph acceleration needs a Postgres with the `graph` C extension; managed
  hosts (incl. **Neon**, the chosen host) can't install it → runs in SQL-fallback
  mode there. See `docs/cognee-postgres-vs-pggraph.md`. Accelerated self-managed
  host is a later option.
- Next stage: **M3 classify** (Claude structured output) — first stage needing
  `ANTHROPIC_API_KEY`.
