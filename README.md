# Slack Knowledge Curator (`skc`)

> Turn a personal Slack "save it for later" channel into an organized, queryable
> **knowledge graph** — and showcase how to build one with
> [Cognee](https://www.cognee.ai/) on Postgres using the community
> **[pgGraph](https://github.com/topoteretes/cognee-community/tree/main/packages/graph/pggraph)**
> graph adapter.

A local-first, CLI-driven pipeline:

```
ingest ──▶ enrich ──▶ classify ──▶ taxonomy ──▶ graph ──▶ query
(Slack)    (X oEmbed)  (Claude)     (emergent)   (Cognee/pgGraph + pgvector)
```

Each stage reads the previous stage's JSONL artifact, so the pipeline is
resumable and every stage can be run on its own.

See [PRD.md](PRD.md) for the full product spec and
[docs/cognee-postgres-vs-pggraph.md](docs/cognee-postgres-vs-pggraph.md) for how
the pgGraph adapter relates to native Cognee Postgres support.

## Why this exists

This is both a useful tool and a **reference example for the Cognee open-source
project** — it exercises the all-Postgres Cognee stack (relational + pgvector +
graph tables) and the experimental pgGraph traversal adapter end-to-end on real,
messy, link-heavy data.

## Status

| Stage | Command | Status |
|-------|---------|--------|
| Ingest | `skc ingest` | ✅ implemented |
| Enrich | `skc enrich` | ✅ implemented |
| Classify | `skc classify` | 🔜 M3 |
| Taxonomy | `skc taxonomy` | 🔜 M4 |
| Graph | `skc graph` / `skc query` | 🔜 M5 |
| Orchestration | `skc run` / `skc status` | `run` 🔜 M6, `status` ✅ |

## Quick start

```bash
# 1. Install (Python 3.11+). Base deps only — the graph stage is an extra.
uv venv --python 3.11 && uv pip install -e .
#   add the Cognee graph stage when you reach M5:
#   uv pip install -e '.[graph]'

# 2. Configure
cp .env.example .env        # then fill in SLACK_TOKEN, ANTHROPIC_API_KEY, OPENAI_API_KEY

# 3. Start Postgres (for the graph stage)
docker compose -f docker/docker-compose.yml up -d

# 4. Run the implemented stage
skc ingest --channel C0123ABCD          # full history
skc ingest --channel C0123ABCD --incremental   # later: only new messages
skc status
```

## Configuration

All settings live in `.env` (see [.env.example](.env.example)). You need:

1. **`SLACK_TOKEN`** — a Slack app token with `channels:history`, `channels:read`
   (or `groups:*` for private channels), and `users:read`.
2. **`ANTHROPIC_API_KEY`** — for summarization + classification.
3. **`OPENAI_API_KEY`** — Cognee's default embedding provider (graph stage only).
4. **X.com** — nothing; enrichment uses the free oEmbed endpoint.

## Layout

```
src/skc/
├── cli.py            # typer entrypoint, stage subcommands
├── config.py         # .env via pydantic-settings
├── models.py         # the Item that flows through every stage
├── store.py          # JSONL read/write + cursors
├── util.py           # link extraction
├── ingest/slack.py   # ✅ Slack Web API → data/raw/*.jsonl
├── enrich/           # 🔜 X oEmbed + page titles
├── classify/         # 🔜 Claude structured classification
├── taxonomy/         # 🔜 emergent taxonomy
└── graph/            # 🔜 Cognee + pgGraph loader
```

## License

[MIT](LICENSE) © 2026 Marvin Scaff
