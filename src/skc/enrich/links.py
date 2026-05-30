"""Resolve the content behind links found in Slack messages.

- **X.com / twitter.com** → the free oEmbed endpoint (no API key). We pull the
  author handle and flatten the returned HTML blockquote to plain tweet text.
- **Other URLs** (optional) → a lightweight GET to grab ``<title>`` and the meta
  description. Full-page crawling is out of scope.

Fetches run concurrently behind a semaphore. Each distinct URL is fetched once
and the result is fanned back out to every message that referenced it. Re-runs
are idempotent: links already enriched with ``status == "ok"`` are reused.
"""

from __future__ import annotations

import asyncio
import html
import re
from urllib.parse import quote

import httpx
from rich.console import Console

from ..config import Settings
from ..models import EnrichedLink, Item
from ..store import read_items, write_items
from ..util import is_x_url

console = Console()

_OEMBED = "https://publish.twitter.com/oembed"
_TAG_RE = re.compile(r"<[^>]+>")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_DESC_RE = re.compile(
    r'<meta[^>]+(?:name|property)=["\'](?:description|og:description)["\'][^>]*'
    r'content=["\'](.*?)["\']',
    re.IGNORECASE | re.DOTALL,
)
# A browser-like UA: X's oEmbed (publish.x.com) and many sites 403 the default
# python-httpx agent. Sent on every request via the client's default headers.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _html_to_text(fragment: str) -> str:
    """Strip tags and collapse whitespace from an HTML fragment."""
    text = _TAG_RE.sub(" ", fragment)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


async def _enrich_x(client: httpx.AsyncClient, url: str) -> EnrichedLink:
    params = {"url": url, "dnt": "true", "omit_script": "true"}
    resp = await client.get(_OEMBED, params=params)
    resp.raise_for_status()
    data = resp.json()
    text = _html_to_text(data.get("html", ""))
    return EnrichedLink(
        type="x_post",
        url=data.get("url", url),
        author=data.get("author_name"),
        title=None,
        text=text or None,
        status="ok",
    )


async def _enrich_web(client: httpx.AsyncClient, url: str) -> EnrichedLink:
    resp = await client.get(url)
    resp.raise_for_status()
    body = resp.text[:200_000]  # titles/meta live near the top; cap the parse
    title = _TITLE_RE.search(body)
    desc = _DESC_RE.search(body)
    return EnrichedLink(
        type="web",
        url=str(resp.url),
        title=_html_to_text(title.group(1)) if title else None,
        text=_html_to_text(desc.group(1)) if desc else None,
        status="ok",
    )


async def _resolve(
    client: httpx.AsyncClient, sem: asyncio.Semaphore, url: str, enrich_web: bool
) -> EnrichedLink:
    async with sem:
        try:
            if is_x_url(url):
                return await _enrich_x(client, url)
            if enrich_web:
                return await _enrich_web(client, url)
            return EnrichedLink(type="web", url=url, status="skipped")
        except Exception as exc:  # network, HTTP, parse — keep the URL, flag it
            kind = "x_post" if is_x_url(url) else "web"
            return EnrichedLink(type=kind, url=url, status="failed", error=str(exc)[:200])


def _iter_items(items: list[Item]):
    """Yield every Item including thread replies (each carries its own links)."""
    for item in items:
        yield item
        yield from item.thread


async def _enrich_items(settings: Settings, items: list[Item], existing: dict[str, Item]) -> None:
    # Reuse prior successful enrichments; collect the URLs that still need work.
    reuse: dict[str, EnrichedLink] = {}
    for it in _iter_items(list(existing.values())):
        for url, link in it.enrichment.items():
            if link.status == "ok":
                reuse[url] = link

    pending: list[str] = []
    seen: set[str] = set()
    for it in _iter_items(items):
        for url in it.links:
            if url in reuse or url in seen:
                continue
            seen.add(url)
            pending.append(url)

    results: dict[str, EnrichedLink] = dict(reuse)
    if pending:
        sem = asyncio.Semaphore(8)
        timeout = httpx.Timeout(settings.x_oembed_timeout)
        headers = {"User-Agent": _USER_AGENT}
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, headers=headers
        ) as client:
            resolved = await asyncio.gather(
                *(_resolve(client, sem, u, settings.x_enrich_non_x_links) for u in pending)
            )
        results.update({url: link for url, link in zip(pending, resolved)})

    # Fan results back out onto each item that referenced the URL.
    for it in _iter_items(items):
        it.enrichment = {url: results[url] for url in it.links if url in results}


def enrich_channel(settings: Settings, channel: str, *, dry_run: bool = False) -> dict:
    """Enrich all links for one channel: data/raw → data/enriched."""
    settings.ensure_dirs()
    raw_path = settings.raw_dir / f"{channel}.jsonl"
    out_path = settings.enriched_dir / f"{channel}.jsonl"

    items = list(read_items(raw_path))
    if not items:
        raise RuntimeError(f"No raw items for {channel}. Run `skc ingest --channel {channel}` first.")

    existing = {it.id: it for it in read_items(out_path)}

    asyncio.run(_enrich_items(settings, items, existing))

    # Tally outcomes across all (item, link) pairs.
    counts = {"ok": 0, "failed": 0, "skipped": 0, "x": 0, "web": 0}
    for it in _iter_items(items):
        for link in it.enrichment.values():
            counts[link.status] = counts.get(link.status, 0) + 1
            counts["x" if link.type == "x_post" else "web"] += 1

    if dry_run:
        console.print(f"  [dim]would write {len(items)} items[/] → {out_path}")
    else:
        n = write_items(out_path, items)
        console.print(f"  [green]wrote {n} items[/] → {out_path}")
    console.print(
        f"  links: [green]{counts['ok']} ok[/], "
        f"[red]{counts['failed']} failed[/], {counts['skipped']} skipped "
        f"({counts['x']} x-posts, {counts['web']} web)"
    )
    return counts
