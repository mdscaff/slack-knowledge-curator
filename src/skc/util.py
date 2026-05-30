"""Small shared helpers."""

from __future__ import annotations

import re

# Slack renders links as <https://url> or <https://url|display text>.
_SLACK_LINK = re.compile(r"<(https?://[^|>]+)(?:\|[^>]*)?>")
# Bare URLs (when not Slack-wrapped, e.g. in thread text we reconstructed).
_BARE_URL = re.compile(r"(?<![<|])https?://[^\s<>|]+")


def extract_urls(text: str) -> list[str]:
    """Pull all URLs out of Slack message text, de-duplicated, order-preserving."""
    found = _SLACK_LINK.findall(text) + _BARE_URL.findall(text)
    seen: dict[str, None] = {}
    for url in found:
        seen.setdefault(url.rstrip(".,);"), None)
    return list(seen)


def is_x_url(url: str) -> bool:
    return re.search(r"https?://(www\.)?(x\.com|twitter\.com)/", url) is not None
