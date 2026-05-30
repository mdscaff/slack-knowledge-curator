"""Export enrichment failures for a channel to a clickable HTML page + CSV.

Reads data/enriched/<channel>.jsonl, collects every link with status == "failed",
and writes:
  data/failures/<channel>.html  — clickable links, open in a real browser
  data/failures/<channel>.csv   — for spreadsheet analysis

Usage:
  python scripts/export_failures.py <CHANNEL_ID>
"""

import csv
import html
import re
import sys
from pathlib import Path

# Allow running from the repo root without installing.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from skc.config import load_settings  # noqa: E402
from skc.store import read_items  # noqa: E402


def _status_code(error: str) -> str:
    # httpx errors read: "Client error '403 Forbidden' for url '...'".
    # Match the code inside the quotes so embedded URL digits don't false-match.
    m = re.search(r"(?:Client|Server) error '(\d{3})", error or "")
    if m:
        return m.group(1)
    if "timeout" in (error or "").lower():
        return "timeout"
    return "other"


def collect(channel: str):
    settings = load_settings()
    items = list(read_items(settings.enriched_dir / f"{channel}.jsonl"))
    rows = []
    for it in items:
        for it2 in [it, *it.thread]:
            for url, e in it2.enrichment.items():
                if e.status != "failed":
                    continue
                rows.append(
                    {
                        "url": url,
                        "type": e.type,
                        "status": _status_code(e.error or ""),
                        "error": (e.error or "").strip(),
                        "ts": it2.ts,
                        "author": it2.author,
                        "note": (it2.text or "").replace("\n", " ").strip(),
                    }
                )
    # Deduplicate by URL; stable order by (status, type, url).
    seen, deduped = set(), []
    for r in sorted(rows, key=lambda r: (r["status"], r["type"], r["url"])):
        if r["url"] in seen:
            continue
        seen.add(r["url"])
        deduped.append(r)
    return settings, deduped


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(
            fh, fieldnames=["status", "type", "url", "error", "author", "ts", "note"]
        )
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in w.fieldnames})


def write_html(path: Path, channel: str, rows: list[dict]) -> None:
    from collections import Counter

    by_status = Counter(r["status"] for r in rows)
    summary = ", ".join(f"{k}: {v}" for k, v in sorted(by_status.items()))

    parts = [
        "<!doctype html><meta charset='utf-8'>",
        f"<title>Enrichment failures — {html.escape(channel)}</title>",
        """<style>
          body{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;margin:2rem;max-width:1100px}
          h1{font-size:1.3rem} .sum{color:#555;margin-bottom:1rem}
          table{border-collapse:collapse;width:100%} th,td{text-align:left;padding:.5rem .6rem;border-bottom:1px solid #eee;vertical-align:top}
          th{position:sticky;top:0;background:#fafafa}
          .s403{color:#b26a00}.s404{color:#999}.sother{color:#444}
          .note{color:#666;font-size:.92em} code{background:#f5f5f5;padding:1px 4px;border-radius:3px}
          a{word-break:break-all}
        </style>""",
        f"<h1>Enrichment failures — {html.escape(channel)}</h1>",
        f"<div class='sum'>{len(rows)} failed links · {html.escape(summary)} · "
        "click a link to test it in your browser</div>",
        "<table><thead><tr><th>#</th><th>status</th><th>type</th><th>link</th>"
        "<th>your note</th></tr></thead><tbody>",
    ]
    for i, r in enumerate(rows, 1):
        cls = f"s{r['status']}" if r["status"] in ("403", "404") else "sother"
        note = html.escape(r["note"][:160]) + ("…" if len(r["note"]) > 160 else "")
        url = html.escape(r["url"])
        parts.append(
            f"<tr><td>{i}</td><td class='{cls}'>{html.escape(r['status'])}</td>"
            f"<td>{html.escape(r['type'])}</td>"
            f"<td><a href='{url}' target='_blank' rel='noopener'>{url}</a></td>"
            f"<td class='note'>{note}</td></tr>"
        )
    parts.append("</tbody></table>")
    path.write_text("".join(parts), encoding="utf-8")


def main(channel: str) -> int:
    settings, rows = collect(channel)
    out_dir = settings.data_dir / "failures"
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"{channel}.csv"
    html_path = out_dir / f"{channel}.html"
    write_csv(csv_path, rows)
    write_html(html_path, channel, rows)
    print(f"{len(rows)} unique failed links")
    print(f"  CSV : {csv_path}")
    print(f"  HTML: {html_path}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python scripts/export_failures.py <CHANNEL_ID>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
