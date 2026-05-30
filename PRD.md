# PRD — Slack Knowledge Curator

**Owner:** marvin@sylogic.ai
**Status:** Draft v1
**Last updated:** 2026-05-30
**Language:** Python 3.11+

---

## 1. Summary

A local-first, CLI-driven pipeline that exports posts from a personal Slack
channel, enriches any linked X.com (Twitter) content, uses Claude to summarize
and classify each item into an **emergent, LLM-discovered taxonomy**, and then
loads the curated, categorized corpus into [Cognee](https://www.cognee.ai/) to
build an ontology and knowledge graph.

The goal is to turn an unstructured stream of "stuff I found interesting" into a
queryable, organized knowledge base.

This project doubles as a **reference example for the Cognee open-source
project** — specifically exercising the community **pgGraph** graph adapter
(contributed by this project's author) on a real, messy, link-heavy corpus. See
[docs/cognee-postgres-vs-pggraph.md](docs/cognee-postgres-vs-pggraph.md).

### One-line goal
> Organize and classify all the knowledge curated in my personal Slack channel
> into a navigable ontology + knowledge graph.

---

## 2. Problem & Motivation

A personal Slack channel is a common "save it for later" inbox: links, threads,
half-thoughts, and X.com posts pile up with no structure. Today there is:

- **No retrieval** — you can't ask "what have I saved about X?"
- **No structure** — no categories, no relationships between items.
- **No enrichment** — an X.com link is just a URL; the actual content isn't captured.

This pipeline fixes all three: capture → enrich → classify → graph.

---

## 3. Goals / Non-Goals

### Goals
- Export messages (and thread replies) from one or more Slack channels via the Slack Web API.
- Resolve and capture the content behind X.com links using X's free oEmbed endpoint.
- Use Claude to produce a per-item **summary** and **classification**.
- Build a **rich, emergent taxonomy** (categories discovered from the content, then curated).
- Persist a clean, structured corpus to disk (JSONL + a taxonomy file).
- Feed the corpus into Cognee to generate an ontology and knowledge graph.
- All secrets/config live in `.env`; everything runs from a single CLI.

### Non-Goals (v1)
- Real-time / streaming ingestion (v1 is batch + incremental re-run).
- A web UI or dashboard (CLI + graph store only).
- Multi-user / multi-workspace tenancy.
- Capturing non-X links' full content (we store the URL + title; deep crawling is future work).
- Posting back into Slack.

---

## 4. Users & Use Cases

**Primary user:** a single power user (you) curating a personal knowledge channel.

**Use cases**
1. *Bulk backfill* — process the entire channel history once.
2. *Incremental update* — re-run weekly to pick up new posts only.
3. *Explore* — query the knowledge graph: "show me everything about agents," "what connects these two ideas?"
4. *Curate* — review and rename/merge the auto-discovered categories.

---

## 5. High-Level Architecture

```
                ┌─────────────┐
   Slack Web    │  ingest     │  conversations.history + replies
   API (token)  │  (slack)    │──────────────┐
                └─────────────┘               │ raw messages (JSONL)
                                              ▼
                ┌─────────────┐      ┌──────────────────┐
   X oEmbed ───▶│  enrich     │─────▶│  normalized items │
   (no auth)    │  (links)    │      │   (JSONL)         │
                └─────────────┘      └──────────────────┘
                                              │
                                              ▼
                ┌─────────────┐      ┌──────────────────┐
   Anthropic ──▶│  classify   │─────▶│  summary +        │
   Claude API   │  (claude)   │      │  categories       │
                └─────────────┘      └──────────────────┘
                                              │
                                              ▼
                ┌─────────────┐      ┌──────────────────┐
                │  taxonomy   │─────▶│  taxonomy.json    │  (emergent, curated)
                └─────────────┘      └──────────────────┘
                                              │
                                              ▼
                ┌─────────────┐      ┌──────────────────┐
   Cognee ─────▶│  graph      │─────▶│  ontology +       │
                │  (cognee)   │      │  knowledge graph  │
                └─────────────┘      └──────────────────┘
```

Each stage reads the previous stage's artifact from disk and writes its own.
This makes the pipeline **resumable** and **debuggable** — you can re-run any
single stage.

---

## 6. Components (Detailed)

### 6.1 Ingest — Slack Web API
- Auth: Slack token in `.env` (`SLACK_TOKEN`). A user token (`xoxp-`) is simplest
  for a personal channel; a bot token (`xoxb-`) works if the bot is invited to the channel.
- **Required scopes:** `channels:history`, `channels:read` (public), or
  `groups:history`, `groups:read` (private), plus `users:read` to resolve author names.
- Calls `conversations.history` paginated by cursor; for any message with
  `thread_ts`, calls `conversations.replies` to pull the full thread.
- Resolves user IDs → display names; resolves channel IDs → names.
- Handles Slack rate limits (Tier 3, ~50 req/min) with backoff.
- **Output:** `data/raw/<channel>.jsonl` — one message per line.
- **Incremental mode:** stores the latest `ts` per channel in a cursor file and
  only fetches messages newer than that on re-run.

### 6.2 Enrich — X.com links
- Scans each message for `x.com` / `twitter.com` URLs.
- Calls X's free **oEmbed** endpoint:
  `https://publish.twitter.com/oembed?url=<tweet_url>&dnt=true&omit_script=true`
- Extracts: author handle, tweet text (from the returned HTML), and the canonical URL.
- **No API key required.** Graceful degradation: if oEmbed fails (deleted/private
  tweet, rate-limited), we keep the bare URL and flag `enrichment_status: failed`.
- (Other URLs: we capture `<title>` and meta description via a lightweight fetch;
  full-page crawling is out of scope for v1.)
- **Output:** `data/enriched/<channel>.jsonl` with an `enrichment` block per item.

### 6.3 Classify & Summarize — Claude
- Provider: Anthropic. Model configurable via `.env`
  (`ANTHROPIC_MODEL`, default `claude-haiku-4-5` for cheap bulk, `claude-opus-4-8` for higher quality).
- For each item, Claude returns **structured JSON** (via tool use / forced schema):
  - `summary` — 1–3 sentence neutral summary.
  - `categories` — 1–3 categories drawn from / proposed for the taxonomy.
  - `tags` — free-form keywords/entities (people, companies, technologies).
  - `entities` — typed entities for the graph (Person, Org, Technology, Concept).
  - `confidence` — model's confidence in the classification.
- **Batching & caching:** prompt-cache the taxonomy + instructions; batch items to
  control cost. Skip already-classified items on re-run (idempotent by message id).
- **Output:** `data/classified/<channel>.jsonl`.

### 6.4 Taxonomy — Emergent + curated
Two-pass approach so categories are *discovered from your actual content*, not guessed:

1. **Discovery pass:** sample/cluster items; Claude proposes a candidate category
   tree (top-level domains → subcategories) with descriptions.
2. **Assignment pass:** every item is classified into the discovered taxonomy;
   items that don't fit can spawn new candidate categories.
3. **Curation:** `taxonomy.json` is human-editable — you rename, merge, or prune
   categories, then re-run the assignment pass. The tool respects manual edits
   (locked categories aren't overwritten).

- **Output:** `data/taxonomy.json` — the canonical category tree with descriptions
  and example item ids.

### 6.5 Graph — Cognee (pgGraph adapter, all-Postgres)
- Loads the classified, summarized items + the taxonomy into Cognee.
- Cognee builds the **ontology** (entity types, category hierarchy) and the
  **knowledge graph** (items ↔ categories ↔ entities ↔ each other).
- **All-Postgres stack:** one Postgres instance serves relational + vector
  (**pgvector**) + graph (`graph_node`/`graph_edge`) data. Runs in a local Docker
  container for now (`pgvector/pgvector:pg16`, port 5433); see
  `docker/docker-compose.yml`.
- **Graph adapter:** the community **pgGraph** adapter
  (`cognee-community-graph-adapter-pggraph`) — the one we contributed upstream.
  It layers pgGraph traversal over the native Postgres graph tables and falls
  back to recursive-CTE SQL when the `graph` extension isn't installed.
  Activated via `register()` + `set_graph_database_provider("pggraph")`.
- **Embeddings:** OpenAI (`text-embedding-3-small`), Cognee's default —
  `OPENAI_API_KEY` required. LLM stays Anthropic.
- **Showcase intent:** this stage is a worked example of using Cognee + the
  pgGraph adapter end-to-end. See
  [docs/cognee-postgres-vs-pggraph.md](docs/cognee-postgres-vs-pggraph.md) for the
  native-Postgres-vs-pgGraph comparison and the adapter's current gaps.
- **Output:** a populated Cognee graph store, queryable via `cognee.search(...)`
  and exposed through the CLI `query` command.

---

## 7. CLI Design

Single entrypoint (`skc` = Slack Knowledge Curator), stage subcommands so you can
run end-to-end or one stage at a time.

```bash
skc ingest    --channel C0123ABCD [--since 2026-01-01] [--incremental]
skc enrich    [--channel C0123ABCD]
skc classify  [--channel C0123ABCD] [--model claude-opus-4-8]
skc taxonomy  --discover            # run discovery pass, write taxonomy.json
skc taxonomy  --assign              # (re)assign items to current taxonomy
skc graph     build                 # load everything into Cognee
skc query     "what have I saved about AI agents?"

skc run --channel C0123ABCD         # full pipeline: ingest→enrich→classify→taxonomy→graph
skc status                          # counts per stage, last incremental cursor
```

Conventions:
- All commands read config from `.env` (override with flags).
- `--dry-run` on every stage prints what it would do.
- Verbose logging to stderr; artifacts to `data/`.

---

## 8. Configuration (`.env`)

```dotenv
# --- Slack ---
SLACK_TOKEN=xoxp-...                 # user token (or xoxb- bot token)
SLACK_CHANNELS=C0123ABCD             # comma-separated channel IDs (optional default)

# --- Anthropic (summarize + classify) ---
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-haiku-4-5     # bulk; use claude-opus-4-8 for quality
ANTHROPIC_MAX_CONCURRENCY=4

# --- X.com enrichment ---
# oEmbed needs no key. Optional knobs:
X_OEMBED_TIMEOUT=10
X_ENRICH_NON_X_LINKS=true            # also grab <title> for non-X URLs

# --- Cognee (ontology + graph), all-Postgres via pgGraph ---
GRAPH_DATABASE_PROVIDER=pggraph      # community adapter; SQL-fallback when ext absent
GRAPH_DATABASE_HOST=localhost
GRAPH_DATABASE_PORT=5433
GRAPH_DATABASE_NAME=cognee
GRAPH_DATABASE_USERNAME=cognee
GRAPH_DATABASE_PASSWORD=cognee
PGGRAPH_BUILD_MODE=manual
DB_PROVIDER=postgres                 # relational + vector live in the same Postgres
VECTOR_DB_PROVIDER=pgvector
LLM_PROVIDER=anthropic
LLM_API_KEY=sk-ant-...
EMBEDDING_PROVIDER=openai
OPENAI_API_KEY=sk-...                # Cognee embeddings (text-embedding-3-small)

# See .env.example for the full, authoritative list.

# --- Paths / behavior ---
DATA_DIR=./data
LOG_LEVEL=INFO
```

`.env.example` is committed; `.env` is gitignored.

---

## 9. Data Model

**Normalized item** (post-enrichment, the spine of the pipeline):

```json
{
  "id": "C0123ABCD-1716998400.123456",
  "channel": "C0123ABCD",
  "ts": "1716998400.123456",
  "author": "Marvin",
  "text": "Great thread on agent memory 👇 https://x.com/user/status/123",
  "permalink": "https://slack.com/archives/...",
  "thread": [ /* reply items, same shape */ ],
  "links": ["https://x.com/user/status/123"],
  "enrichment": {
    "https://x.com/user/status/123": {
      "type": "x_post",
      "author": "@user",
      "text": "resolved tweet text...",
      "status": "ok"
    }
  },
  "classification": {
    "summary": "A thread arguing that long-term agent memory...",
    "categories": ["AI / Agents", "AI / Memory & Retrieval"],
    "tags": ["agents", "memory", "RAG"],
    "entities": [
      {"name": "RAG", "type": "Concept"},
      {"name": "user", "type": "Person"}
    ],
    "confidence": 0.86,
    "model": "claude-haiku-4-5"
  }
}
```

**taxonomy.json:**

```json
{
  "version": 3,
  "categories": [
    {
      "name": "AI / Agents",
      "description": "Autonomous LLM agents, tool use, planning.",
      "locked": false,
      "examples": ["C0123ABCD-1716998400.123456"]
    }
  ]
}
```

---

## 10. API Key & Connection Setup

What you actually need to connect, in order:

1. **Slack token** (`SLACK_TOKEN`)
   - Go to <https://api.slack.com/apps> → *Create New App* → *From scratch*.
   - Add the scopes from §6.1 under **OAuth & Permissions**.
   - Install to workspace → copy the **User OAuth Token** (`xoxp-…`) into `.env`.
   - Find your channel ID: open the channel in Slack → *View channel details* →
     ID is at the bottom (`C…`).

2. **Anthropic API key** (`ANTHROPIC_API_KEY`)
   - Go to <https://console.anthropic.com/> → *API Keys* → *Create Key*.
   - Paste into `.env`. (I can wire this up and run a 1-message smoke test once it's in.)

3. **X.com** — **nothing to set up.** oEmbed is public/no-auth. ✅

4. **Cognee embeddings** (`OPENAI_API_KEY` or alt)
   - Cognee defaults to OpenAI embeddings. Either add an OpenAI key, or we configure
     Cognee to use a different embeddings provider during the graph step.

> When you're ready, drop the Slack + Anthropic keys into `.env` and I'll verify
> each connection with a minimal call before running the full pipeline.

---

## 11. Project Layout

```
slack-dump/
├── PRD.md
├── .env.example
├── .env                    # gitignored
├── pyproject.toml
├── data/
│   ├── raw/
│   ├── enriched/
│   ├── classified/
│   └── taxonomy.json
└── src/skc/
    ├── cli.py              # entrypoint + subcommands (typer/click)
    ├── config.py           # loads .env (pydantic-settings)
    ├── ingest/slack.py
    ├── enrich/links.py     # x oembed + generic title fetch
    ├── classify/claude.py
    ├── taxonomy/builder.py
    └── graph/cognee_loader.py
```

Suggested deps: `slack_sdk`, `anthropic`, `httpx`, `typer`, `pydantic-settings`,
`cognee`, `python-dotenv`.

---

## 12. Milestones

| # | Milestone | Deliverable |
|---|-----------|-------------|
| M1 | Ingest | `skc ingest` pulls a channel + threads to JSONL, incremental cursor works |
| M2 | Enrich | X oEmbed resolution + graceful failure; non-X title capture |
| M3 | Classify | Claude structured summary + classification, idempotent re-runs |
| M4 | Taxonomy | Discovery + assignment passes, human-editable `taxonomy.json` |
| M5 | Graph | Cognee load + `skc query` over the knowledge graph |
| M6 | Polish | `skc run`, `skc status`, docs, `.env.example`, cost report |

---

## 13. Risks & Open Questions

- **X oEmbed limits:** rate limits and deleted/private tweets mean some items won't
  enrich. Mitigation: flag + retry queue; optionally upgrade to a paid X path later.
- **Classification cost** at scale: mitigate with Haiku for bulk, prompt caching,
  and batching; report estimated cost in `skc status`.
- **Cognee embeddings provider:** OpenAI (`text-embedding-3-small`) — decided.
- **pgGraph hosting (open):** the accelerated path needs a Postgres with the
  pgGraph C extension, which managed providers (RDS/Neon/Supabase) don't allow.
  Local Docker for now; a self-managed host (custom image) is a future task. See
  the hosting matrix in
  [docs/cognee-postgres-vs-pggraph.md §6](docs/cognee-postgres-vs-pggraph.md).
- **Taxonomy drift:** emergent categories can sprawl. Mitigation: periodic
  merge/curation pass; `locked` categories.
- **Open:** How far back is the channel history (affects M1 cost/time)? Any private
  channels (changes Slack scopes)? Roughly how many X links vs. other links?

---

## 14. Success Criteria

- A full channel backfill runs end-to-end from one `skc run` command.
- ≥90% of items receive a summary + at least one category.
- ≥80% of X links successfully enriched.
- The Cognee graph answers natural-language queries that span multiple items
  and surface non-obvious connections.
- Re-running the pipeline is incremental and idempotent (no duplicate work).
