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

## 2026-05-30 — First live run on real data

Ran ingest + enrich against the real personal channel (a self-DM, `D...` id —
note: DMs need the `im:history` user-token scope, not `channels:history`).

### Numbers
- **392 messages** ingested (2 pages), 0 threads — a DM stream, not threaded.
- **387/392 messages carry a link** — this channel is ~99% "link + a note".
- **383 unique URLs: 279 X/Twitter, 104 other.** It's overwhelmingly an X.com
  reading list. Validates the decision to make X enrichment first-class.
- **Enrichment: 92.0% success** (358/389). Residual failures, all permanent:
  13 X 403 (protected/suspended), 10 web 403 (Cloudflare/auth walls), 4 X 404 +
  3 web 404 (deleted/dead), 1 other.

### Learnings
- **Default httpx User-Agent gets 403'd.** X's oEmbed (`publish.x.com`) and many
  sites reject `python-httpx/*`. Setting a browser-like UA at the client level
  helped — though most X 403s turned out to be protected/suspended accounts, not
  UA-related. Net: a robustness win, not a silver bullet.
- **Idempotent retry paid off as a design choice**, even if these particular
  failures were permanent: re-running only re-hit the 31 non-`ok` links, not all
  389. The cost of a retry scales with failures, not corpus size.
- **The corpus shape should drive the taxonomy.** 279 founder/AI/eng tweets means
  the emergent categories will skew there — good to know before M3/M4.

> **Blog hook:** "What 392 saved Slack messages told me before I built the graph
> — and the 8% the open web simply won't give you."

## 2026-05-30 — Medium is a Cloudflare wall; slug-title fallback

Dug into the 31 enrichment failures. **7 of the 10 web 403s were Medium**
(`medium.com` + `*.medium.com` custom domains + the `gopubby.com` publication).

### What's actually happening
Medium serves a **Cloudflare JS challenge** ("Just a moment…") to any non-browser
client. Confirmed it's not a header problem: even with a full set of browser
headers (UA, Accept, Accept-Language, Referer) we still get `403` with title
`Just a moment...`. A plain HTTP client can't pass it; only a real/headless
browser executing the challenge JS can — and Cloudflare frequently blocks
automated browsers too. (Member-only Medium stories are paywalled on top.)

### The pragmatic fix: derive the title from the URL slug
For **classification** we don't need the full article — Medium slugs *are* the
title: `the-agent-ecosystem-formula-c08c041bb744` → "The Agent ecosystem
formula". So we added a slug fallback:
- New `EnrichedLink.status = "derived"` (+ `source` field) — honest signal that
  the title was inferred, not fetched. Never conflate a guess with real content.
- Detect Cloudflare interstitials by title ("just a moment", "attention
  required", …) even on a `200`, and fall back to the slug.
- On web fetch failure, derive from slug; **X failures stay failed** (a tweet's
  text can't be inferred from its numeric URL). Opaque paths (`/p/<id>`, bare
  domains) correctly yield no title rather than a fake one.

### Result
Re-ran enrich: **358 ok + 12 derived + 19 failed** → **95% of links now carry
usable text** for the graph (was 92%). Remaining 19: 17 protected/deleted X
posts + 2 opaque web URLs. Those need either the original author or a headless
browser, and aren't worth chasing for v1.

> **Blog hook:** "Medium's Cloudflare wall vs. my knowledge graph — and why the
> URL slug was a better answer than a headless browser."

### Optional later: headless-browser fallback
A Playwright pass could try to render the truly-blocked pages, but Cloudflare may
still win and it's heavy. Parking it; the slug fallback covers the classification
need at ~zero cost.

### Open threads
- pgGraph acceleration needs a Postgres with the `graph` C extension; managed
  hosts (incl. **Neon**, the chosen host) can't install it → runs in SQL-fallback
  mode there. See `docs/cognee-postgres-vs-pggraph.md`. Accelerated self-managed
  host is a later option.
- Next stage: **M3 classify** (Claude structured output) — first stage needing
  `ANTHROPIC_API_KEY`.
