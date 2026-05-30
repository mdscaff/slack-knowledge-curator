# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/); versions follow
[Semantic Versioning](https://semver.org/). A narrative build log (with the
debugging stories behind these entries) lives in [docs/DEVLOG.md](docs/DEVLOG.md).

## [Unreleased]

## [0.1.0] — 2026-05-30

First working end-to-end pipeline: export a personal Slack channel, enrich its
links, classify with Claude into an emergent taxonomy, build a Cognee knowledge
graph on the community pgGraph adapter, and query/visualize it.

### Added
- **`skc ingest`** — Slack Web API export (`conversations.history` + thread
  replies), user-name resolution, incremental cursor.
- **`skc enrich`** — concurrent link resolution: X/Twitter via free oEmbed,
  other URLs via title/meta; idempotent. Slug-title fallback for Cloudflare-blocked
  pages (e.g. Medium), recorded as `derived` rather than failed.
- **`skc classify`** — per-item Claude structured output (summary, emergent
  `Domain / Subtopic` categories, tags, typed entities, confidence). Prompt-cached,
  concurrent, idempotent; `--limit`/`--dry-run`.
- **`skc taxonomy`** — one-shot consolidation of the fragmented per-item
  categories into a clean tree + a `from→to` mapping; `--assign` rewrites items to
  canonical labels. `taxonomy.json` is hand-editable with lockable domains.
- **`skc graph`** — feeds classified items into Cognee `cognify` on the
  all-Postgres backend (relational + pgvector + graph), tagging nodes by category
  via `node_set`, then materializes the pgGraph index.
- **`skc query`** — graph-aware retrieval (Cognee `SearchType`), Markdown-rendered
  output, `--domain` category filtering.
- **`skc visualize`** — interactive HTML graph (vis-network): top-N entities as
  toggle-able hubs, plus **click-to-source** (click a node to list the saved
  posts/articles it came from, as links).
- **`skc status`** — per-stage item counts and the incremental cursor.
- Docs: `PRD.md`, `docs/cognee-postgres-vs-pggraph.md` (native Postgres vs
  pgGraph + gaps), `docs/DEVLOG.md` (running chronology), MIT `LICENSE`.

### Verified
- Compatibility **audited and runtime-tested against Cognee 1.1.1**
  (released 2026-05-29); pinned `cognee>=1.1.1,<2`. See `docs/DEVLOG.md`.
- Full run on a real 392-message channel: 95% of links usable after enrichment,
  331 raw categories consolidated to 47 across 10 domains, a 3,225-node /
  12,923-edge knowledge graph, and successful graph-aware queries + visualization.

### Notes
- The pgGraph adapter is installed from the cognee-community git subdirectory
  (not yet on PyPI) and runs in SQL-fallback mode on a stock pgvector image
  (correct results; no traversal acceleration without the `graph` extension).

[Unreleased]: https://github.com/mdscaff/slack-knowledge-curator/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/mdscaff/slack-knowledge-curator/releases/tag/v0.1.0
