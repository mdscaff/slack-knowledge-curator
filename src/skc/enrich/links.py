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
from urllib.parse import unquote, urlparse

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


# Cloudflare/anti-bot interstitials return a generic title instead of content.
_CHALLENGE_TITLES = ("just a moment", "attention required", "access denied", "are you a robot")
# Trailing Medium-style hex id on a slug, e.g. ...-c08c041bb744
_SLUG_HASH = re.compile(r"-[0-9a-f]{8,}$", re.IGNORECASE)


def _is_challenge_title(title: str | None) -> bool:
    return bool(title) and any(c in title.lower() for c in _CHALLENGE_TITLES)


def _slug_title(url: str) -> str | None:
    """Derive a human-readable title from the last meaningful path segment.

    Medium and many blogs use the article title as a kebab-case slug (often with
    a trailing hex id). Good enough for classification when the page is blocked.
    """
    path = urlparse(url).path.rstrip("/")
    if not path:
        return None
    seg = unquote(path.rsplit("/", 1)[-1])
    seg = _SLUG_HASH.sub("", seg)  # drop trailing -<hexid>
    words = [w for w in re.split(r"[-_]+", seg) if w and not w.isdigit()]
    if len(words) < 2:  # "p", "123", single token → not a real title slug
        return None
    title = " ".join(words).strip()
    return title[:1].upper() + title[1:] if title else None


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


def _derived_web_link(url: str, error: str | None = None) -> EnrichedLink:
    """Build a slug-derived link, or a plain failure if no title can be inferred."""
    slug = _slug_title(url)
    if slug:
        return EnrichedLink(type="web", url=url, title=slug, status="derived", source="slug")
    return EnrichedLink(type="web", url=url, status="failed", error=error)


async def _enrich_web(client: httpx.AsyncClient, url: str) -> EnrichedLink:
    resp = await client.get(url)
    resp.raise_for_status()
    body = resp.text[:200_000]  # titles/meta live near the top; cap the parse
    title_m = _TITLE_RE.search(body)
    title = _html_to_text(title_m.group(1)) if title_m else None
    # A 200 that's actually a Cloudflare challenge → fall back to the slug.
    if _is_challenge_title(title):
        return _derived_web_link(url)
    desc = _DESC_RE.search(body)
    return EnrichedLink(
        type="web",
        url=str(resp.url),
        title=title,
        text=_html_to_text(desc.group(1)) if desc else None,
        status="ok",
        source="page",
    )


async def _resolve(
    client: httpx.AsyncClient, sem: asyncio.Semaphore, url: str, enrich_web: bool
) -> EnrichedLink:
    async with sem:
        is_x = is_x_url(url)
        try:
            if is_x:
                return await _enrich_x(client, url)
            if enrich_web:
                return await _enrich_web(client, url)
            return EnrichedLink(type="web", url=url, status="skipped")
        except Exception as exc:  # network, HTTP, parse
            err = str(exc)[:200]
            # A tweet's content can't be inferred from its numeric URL; a web
            # article's usually can — fall back to the slug instead of failing.
            if is_x:
                return EnrichedLink(type="x_post", url=url, status="failed", error=err)
            return _derived_web_link(url, error=err)


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
        f"[cyan]{counts.get('derived', 0)} derived[/], "
        f"[red]{counts['failed']} failed[/], {counts['skipped']} skipped "
        f"({counts['x']} x-posts, {counts['web']} web)"
    )
    return counts
