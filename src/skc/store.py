"""JSONL read/write helpers for pipeline artifacts.

Each stage writes ``data/<stage>/<channel>.jsonl`` (one Item per line). Reading
tolerates blank lines so partially written files don't crash a resumed run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Iterator

from .models import Item


def write_items(path: Path, items: Iterable[Item]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as fh:
        for item in items:
            fh.write(item.model_dump_json())
            fh.write("\n")
            count += 1
    return count


def read_items(path: Path) -> Iterator[Item]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            yield Item.model_validate_json(line)


def load_cursors(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_cursors(path: Path, cursors: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cursors, indent=2), encoding="utf-8")
