"""Consolidate the fragmented per-item categories into a coherent taxonomy.

Per-item classification produces many near-duplicate categories (`AI / Prompting`
vs `AI / Prompt Engineering`, the same `Developer Tools` under three domains).
This stage:

1. **discover** — sends the full list of raw categories (with counts) to Claude in
   one call and gets back a clean 2-level taxonomy (Domain → Subcategory, with
   descriptions) plus a mapping from every raw category to a canonical one.
   Written to `data/taxonomy.json`.
2. **assign** — rewrites each item's `classification.categories` to the canonical
   labels using that mapping (deduped). Re-runnable.

`taxonomy.json` is human-editable: edit a `to` mapping or a domain description,
mark a domain `"locked": true`, and re-run `--assign`. A `--discover` re-run won't
overwrite locked domains.
"""

from __future__ import annotations

import json

from anthropic import Anthropic
from rich.console import Console

from ..config import Settings
from ..models import Item
from ..store import read_items, write_items

console = Console()

_SYSTEM = """You are organizing a personal knowledge base's category taxonomy.

You are given the raw `Domain / Subcategory` labels an item-by-item classifier \
produced, with frequency counts. They are fragmented and inconsistent \
(near-duplicates, the same subcategory under different domains, one-off labels).

Produce a clean, coherent taxonomy:
- 6-12 top-level domains that cover the corpus without overlapping.
- Each domain has a focused set of subcategories (merge near-duplicates; fold \
  rare one-offs into the closest fit or a domain-level "General").
- Then map EVERY input category to exactly one canonical "Domain / Subcategory".

Be decisive and consistent. Favor fewer, well-defined categories over many \
granular ones. Keep names short and Title Case."""

_TOOL = {
    "name": "consolidate",
    "description": "Return the consolidated taxonomy and a mapping for every raw category.",
    "input_schema": {
        "type": "object",
        "properties": {
            "domains": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "subcategories": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "description": {"type": "string"},
                                },
                                "required": ["name"],
                            },
                        },
                    },
                    "required": ["name", "subcategories"],
                },
            },
            "mapping": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "from": {"type": "string"},
                        "to": {"type": "string"},
                    },
                    "required": ["from", "to"],
                },
            },
        },
        "required": ["domains", "mapping"],
    },
}


def _raw_categories(items: list[Item]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for it in items:
        if it.classification:
            for c in it.classification.categories:
                counts[c] = counts.get(c, 0) + 1
    return counts


def discover(settings: Settings, channel: str, *, model: str | None = None) -> dict:
    """Build a consolidated taxonomy + mapping from classified categories."""
    settings.require_anthropic()
    model = model or "claude-sonnet-4-6"

    items = [it for it in read_items(settings.classified_dir / f"{channel}.jsonl")]
    counts = _raw_categories(items)
    if not counts:
        raise RuntimeError(
            f"No classified categories for {channel}. Run `skc classify` first."
        )

    listing = "\n".join(f"{c}  (x{n})" for c, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    console.print(f"  consolidating {len(counts)} raw categories with [bold]{model}[/]…")

    client = Anthropic(api_key=settings.anthropic_api_key)
    resp = client.messages.create(
        model=model,
        # Mapping echoes every raw category, so output scales with len(counts).
        # Budget generously (~30 tokens/category + tree) so it isn't truncated.
        max_tokens=min(32000, 4000 + 40 * len(counts)),
        system=_SYSTEM,
        tools=[_TOOL],
        tool_choice={"type": "tool", "name": "consolidate"},
        messages=[{"role": "user", "content": f"Raw categories with counts:\n\n{listing}"}],
    )
    if resp.stop_reason == "max_tokens":
        console.print("  [yellow]warning: response hit max_tokens; mapping may be incomplete[/]")
    data = next((b.input for b in resp.content if b.type == "tool_use"), {})

    # Preserve any human-locked domains across re-discovery.
    prior = _load(settings)
    locked = {d["name"]: d for d in prior.get("domains", []) if d.get("locked")}
    domains = [locked.get(d["name"], d) for d in data.get("domains", [])]
    for name, d in locked.items():
        if name not in {x["name"] for x in domains}:
            domains.append(d)

    mapping = {m["from"]: m["to"] for m in data.get("mapping", [])}
    matched = sum(1 for c in counts if c in mapping)
    # Safety net: any raw category the model skipped maps to itself.
    for c in counts:
        mapping.setdefault(c, c)
    collapse = len(counts) - len({mapping[c] for c in counts})
    console.print(
        f"  model mapped {matched}/{len(counts)} raw categories "
        f"→ {len({mapping[c] for c in counts})} canonical ({collapse} collapsed)"
    )

    taxonomy = {
        "version": prior.get("version", 0) + 1,
        "channel": channel,
        "model": model,
        "domains": domains,
        "mapping": mapping,
    }
    settings.taxonomy_path.parent.mkdir(parents=True, exist_ok=True)
    settings.taxonomy_path.write_text(json.dumps(taxonomy, indent=2), encoding="utf-8")
    console.print(
        f"  [green]wrote taxonomy[/] → {settings.taxonomy_path} "
        f"({len(domains)} domains, {len(counts)} categories mapped)"
    )
    return taxonomy


def assign(settings: Settings, channel: str) -> dict:
    """Rewrite each item's categories to the canonical taxonomy labels."""
    taxonomy = _load(settings)
    mapping = taxonomy.get("mapping")
    if not mapping:
        raise RuntimeError("No taxonomy mapping. Run `skc taxonomy --discover` first.")

    path = settings.classified_dir / f"{channel}.jsonl"
    items = list(read_items(path))
    changed = 0
    for it in items:
        if not it.classification:
            continue
        new: list[str] = []
        for c in it.classification.categories:
            canon = mapping.get(c, c)
            if canon not in new:
                new.append(canon)
        if new != it.classification.categories:
            changed += 1
        it.classification.categories = new[:3]

    write_items(path, items)
    console.print(f"  [green]reassigned[/] {changed} items → {len(set(mapping.values()))} canonical categories")
    return {"changed": changed}


def _load(settings: Settings) -> dict:
    if settings.taxonomy_path.exists():
        return json.loads(settings.taxonomy_path.read_text(encoding="utf-8"))
    return {}
