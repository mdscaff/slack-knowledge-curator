"""Ingest a Slack channel via the Web API into normalized Item JSONL.

Pulls ``conversations.history`` (paginated), fetches full thread replies for any
message with replies, resolves user IDs to display names, and writes one Item
per top-level message. Supports incremental runs via a per-channel ``oldest``
cursor stored in ``data/cursors.json``.

Rate limits: the SDK retries on HTTP 429 when configured with
``RetryHandler``s; we additionally back off conservatively between pages.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from rich.console import Console
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from ..config import Settings
from ..models import Item
from ..store import load_cursors, read_items, save_cursors, write_items
from ..util import extract_urls

console = Console()

# conversations.history / replies are Tier 3 (~50 req/min). Be polite.
_PAGE_PAUSE_S = 1.2
_PAGE_SIZE = 200


@dataclass
class IngestResult:
    channel: str
    messages: int
    threads_expanded: int
    latest_ts: str | None


class _UserCache:
    """Resolve and memoize Slack user IDs → display names."""

    def __init__(self, client: WebClient) -> None:
        self._client = client
        self._cache: dict[str, str] = {}

    def name(self, user_id: str | None) -> str:
        if not user_id:
            return ""
        if user_id in self._cache:
            return self._cache[user_id]
        try:
            resp = self._client.users_info(user=user_id)
            profile = resp["user"].get("profile", {})
            name = (
                profile.get("display_name")
                or profile.get("real_name")
                or resp["user"].get("name")
                or user_id
            )
        except SlackApiError as exc:
            console.print(f"[yellow]users.info failed for {user_id}: {exc.response['error']}[/]")
            name = user_id
        self._cache[user_id] = name
        return name


def _message_to_item(channel: str, msg: dict, users: _UserCache) -> Item:
    text = msg.get("text", "")
    return Item(
        id=f"{channel}-{msg['ts']}",
        channel=channel,
        ts=msg["ts"],
        author=users.name(msg.get("user")),
        author_id=msg.get("user", ""),
        text=text,
        links=extract_urls(text),
    )


def _fetch_thread(client: WebClient, channel: str, thread_ts: str, users: _UserCache) -> list[Item]:
    """Fetch all replies in a thread (excluding the parent), paginated."""
    replies: list[Item] = []
    cursor: str | None = None
    while True:
        resp = client.conversations_replies(
            channel=channel, ts=thread_ts, cursor=cursor, limit=_PAGE_SIZE
        )
        for msg in resp.get("messages", []):
            if msg.get("ts") == thread_ts:
                continue  # the parent; already captured at top level
            replies.append(_message_to_item(channel, msg, users))
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(_PAGE_PAUSE_S)
    return replies


def ingest_channel(
    settings: Settings,
    channel: str,
    *,
    since: str | None = None,
    incremental: bool = False,
    dry_run: bool = False,
) -> IngestResult:
    """Ingest one channel to ``data/raw/<channel>.jsonl``.

    Args:
        since: only fetch messages newer than this Slack ts (epoch string).
        incremental: start from the stored cursor; merge with existing file.
    """
    settings.require_slack()
    settings.ensure_dirs()

    client = WebClient(token=settings.slack_token)
    users = _UserCache(client)

    cursors = load_cursors(settings.cursor_path)
    oldest = since
    if incremental and not oldest:
        oldest = cursors.get(channel)

    out_path = settings.raw_dir / f"{channel}.jsonl"
    existing: dict[str, Item] = {}
    if incremental:
        existing = {it.id: it for it in read_items(out_path)}

    console.print(
        f"[bold]Ingesting[/] {channel}"
        + (f" since ts={oldest}" if oldest else " (full history)")
        + (" [dim](dry-run)[/]" if dry_run else "")
    )

    collected: dict[str, Item] = {}
    threads_expanded = 0
    latest_ts = oldest
    page_cursor: str | None = None
    page_num = 0

    while True:
        page_num += 1
        try:
            resp = client.conversations_history(
                channel=channel,
                cursor=page_cursor,
                oldest=oldest,
                limit=_PAGE_SIZE,
                inclusive=False,
            )
        except SlackApiError as exc:
            err = exc.response["error"]
            if err == "not_in_channel":
                raise RuntimeError(
                    f"The token's identity is not a member of {channel}. "
                    "Invite the bot/user to the channel, or use a user token that is a member."
                ) from exc
            raise

        messages = resp.get("messages", [])
        console.print(f"  page {page_num}: {len(messages)} messages")

        for msg in messages:
            if msg.get("subtype") in {"channel_join", "channel_leave"}:
                continue
            item = _message_to_item(channel, msg, users)
            if latest_ts is None or float(item.ts) > float(latest_ts):
                latest_ts = item.ts

            if not dry_run and msg.get("reply_count"):
                item.thread = _fetch_thread(client, channel, msg["ts"], users)
                threads_expanded += 1

            collected[item.id] = item

        page_cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not page_cursor or dry_run:
            break
        time.sleep(_PAGE_PAUSE_S)

    merged = {**existing, **collected}
    ordered = sorted(merged.values(), key=lambda it: float(it.ts))

    if dry_run:
        console.print(
            f"  [dim]would write {len(ordered)} items "
            f"({len(collected)} fetched, {threads_expanded} threads)[/]"
        )
    else:
        n = write_items(out_path, ordered)
        if latest_ts:
            cursors[channel] = latest_ts
            save_cursors(settings.cursor_path, cursors)
        console.print(f"  [green]wrote {n} items[/] → {out_path}")

    return IngestResult(
        channel=channel,
        messages=len(collected),
        threads_expanded=threads_expanded,
        latest_ts=latest_ts,
    )
