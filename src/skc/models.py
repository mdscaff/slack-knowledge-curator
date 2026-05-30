"""Pydantic models for the normalized item that flows through the pipeline.

A single ``Item`` shape is read and written at every stage; later stages fill in
the optional ``enrichment`` and ``classification`` blocks. Persisted as JSONL
(one ``Item`` per line) so stages stay decoupled and resumable.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class EnrichedLink(BaseModel):
    """Resolved content behind a single URL found in a message."""

    type: Literal["x_post", "web", "unknown"] = "unknown"
    url: str
    author: Optional[str] = None
    title: Optional[str] = None
    text: Optional[str] = None
    # ok      = real content extracted (oembed / page meta)
    # derived = fetch blocked (e.g. Cloudflare); title inferred from the URL slug
    # failed  = nothing usable; skipped = not attempted
    status: Literal["ok", "derived", "failed", "skipped"] = "ok"
    source: Optional[str] = None  # "oembed" | "page" | "slug"
    error: Optional[str] = None


class Entity(BaseModel):
    name: str
    type: str = "Concept"  # Person | Org | Technology | Concept | ...


class Classification(BaseModel):
    summary: str = ""
    categories: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    entities: list[Entity] = Field(default_factory=list)
    confidence: float = 0.0
    model: str = ""


class Item(BaseModel):
    """One Slack message (plus its thread + enrichment + classification)."""

    id: str  # "{channel}-{ts}"
    channel: str
    ts: str
    author: str = ""
    author_id: str = ""
    text: str = ""
    permalink: Optional[str] = None
    links: list[str] = Field(default_factory=list)
    thread: list["Item"] = Field(default_factory=list)
    enrichment: dict[str, EnrichedLink] = Field(default_factory=dict)
    classification: Optional[Classification] = None


Item.model_rebuild()
