"""Summarize + classify each item with Claude (structured tool output).

For every enriched Item we build a compact "document" (the Slack note + whatever
we resolved from its links) and ask Claude to return a structured classification
via a forced tool call: a summary, 1-3 emergent hierarchical categories, tags,
typed entities, and a confidence score.

- **Emergent taxonomy:** categories are free-form `Domain / Subtopic` strings.
  Cross-item consistency is handled later by `skc taxonomy` (consolidation).
- **Prompt caching:** the long system instruction is marked cacheable so bulk
  runs only pay for it once per 5-min window.
- **Idempotent:** items already classified (by id) are reused on re-run.
- **Concurrent:** bounded by ANTHROPIC_MAX_CONCURRENCY.
"""

from __future__ import annotations

import asyncio

from anthropic import AsyncAnthropic
from rich.console import Console

from ..config import Settings
from ..models import Classification, Entity, Item
from ..store import read_items, write_items

console = Console()

_SYSTEM = """You classify items a person saved to their personal Slack \
"knowledge" channel — mostly X/Twitter posts and article links, each with a \
short note. Your output organizes this into an emergent knowledge taxonomy.

Guidelines:
- summary: 1-2 neutral sentences capturing the core idea. No fluff.
- categories: 1-3 hierarchical labels as "Domain / Subtopic" (e.g.
  "AI / Agents", "Startups / Distribution", "Engineering / Developer Tools").
  Prefer reusing common domains; be consistent in casing and wording.
- tags: 3-8 lowercase keywords (techniques, concepts, products).
- entities: the notable people, orgs, technologies, products, concepts named.
- confidence: 0-1, how confident the classification is given the available text.

Classify based on the actual content. If the item is thin (a bare link with no
resolved text), infer conservatively from the title/handle and lower confidence."""

_TOOL = {
    "name": "classify",
    "description": "Return the structured classification for one saved item.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "categories": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 3,
            },
            "tags": {"type": "array", "items": {"type": "string"}},
            "entities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "type": {
                            "type": "string",
                            "enum": [
                                "Person",
                                "Org",
                                "Technology",
                                "Product",
                                "Concept",
                                "Place",
                            ],
                        },
                    },
                    "required": ["name", "type"],
                },
            },
            "confidence": {"type": "number"},
        },
        "required": ["summary", "categories", "tags", "entities", "confidence"],
    },
}


def _render_document(item: Item) -> str:
    """Compose the text Claude classifies: the note plus resolved link content."""
    parts: list[str] = []
    if item.text.strip():
        parts.append(f"Note: {item.text.strip()}")
    for url, e in item.enrichment.items():
        if e.type == "x_post" and (e.text or e.author):
            who = f"@{e.author}" if e.author else "unknown"
            parts.append(f"X post by {who}: {e.text or '(text unavailable)'}")
        elif e.title or e.text:
            label = "Article" if e.status != "derived" else "Article (title inferred)"
            bits = " — ".join(b for b in (e.title, e.text) if b)
            parts.append(f"{label}: {bits} ({url})")
        else:
            parts.append(f"Link (no content): {url}")
    return "\n".join(parts) if parts else "(empty)"


async def _classify_one(
    client: AsyncAnthropic, sem: asyncio.Semaphore, model: str, item: Item
) -> Classification:
    document = _render_document(item)
    async with sem:
        resp = await client.messages.create(
            model=model,
            max_tokens=700,
            system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
            tools=[_TOOL],
            tool_choice={"type": "tool", "name": "classify"},
            messages=[{"role": "user", "content": f"Classify this saved item:\n\n{document}"}],
        )
    data = next((b.input for b in resp.content if b.type == "tool_use"), {})
    return Classification(
        summary=data.get("summary", ""),
        categories=data.get("categories", []),
        tags=data.get("tags", []),
        entities=[Entity(name=e["name"], type=e.get("type", "Concept")) for e in data.get("entities", [])],
        confidence=float(data.get("confidence", 0.0)),
        model=model,
    )


async def _run(settings: Settings, items: list[Item], model: str) -> None:
    sem = asyncio.Semaphore(max(1, settings.anthropic_max_concurrency))
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    done = 0
    total = len(items)

    async def work(it: Item) -> None:
        nonlocal done
        it.classification = await _classify_one(client, sem, model, it)
        done += 1
        if done % 10 == 0 or done == total:
            console.print(f"  classified {done}/{total}")

    await asyncio.gather(*(work(it) for it in items))


def classify_channel(
    settings: Settings,
    channel: str,
    *,
    model: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Classify enriched items: data/enriched → data/classified."""
    settings.ensure_dirs()
    model = model or settings.anthropic_model

    src = settings.enriched_dir / f"{channel}.jsonl"
    items = list(read_items(src))
    if not items:
        raise RuntimeError(
            f"No enriched items for {channel}. Run `skc enrich --channel {channel}` first."
        )

    out_path = settings.classified_dir / f"{channel}.jsonl"
    existing = {it.id: it for it in read_items(out_path)}

    need = [it for it in items if it.id not in existing or existing[it.id].classification is None]
    already = len(items) - len(need)
    todo = need[:limit] if limit else need
    deferred = len(need) - len(todo)

    console.print(
        f"  {len(items)} items · {already} already classified · {len(todo)} to do "
        f"with [bold]{model}[/]"
        + (f" · {deferred} deferred by --limit" if deferred else "")
        + (" [dim](dry-run)[/]" if dry_run else "")
    )
    if dry_run or not todo:
        if dry_run and todo:
            console.print("  [dim]sample document:[/]\n" + _render_document(todo[0])[:500])
        return {"classified": 0, "already": already, "deferred": deferred}

    settings.require_anthropic()
    asyncio.run(_run(settings, todo, model))

    # Write the full set back: fresh classifications on `todo`, prior ones reused.
    todo_ids = {t.id for t in todo}
    for it in items:
        if it.id not in todo_ids and it.id in existing:
            it.classification = existing[it.id].classification
    ordered = sorted(items, key=lambda it: float(it.ts))
    n = write_items(out_path, ordered)
    console.print(f"  [green]wrote {n} items[/] → {out_path}")
    return {"classified": len(todo), "already": already, "deferred": deferred}
