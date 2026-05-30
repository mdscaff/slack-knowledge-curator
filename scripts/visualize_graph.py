"""Thin shim — prefer `skc visualize`. Kept for `python scripts/...` usage.

  .venv-cognee/bin/python scripts/visualize_graph.py [TOP_N] [CHANNEL]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from skc.config import load_settings  # noqa: E402
from skc.graph import build_visualization  # noqa: E402

if __name__ == "__main__":
    top_n = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    settings = load_settings()
    channel = sys.argv[2] if len(sys.argv) > 2 else (settings.channel_ids or ["graph"])[0]
    path = build_visualization(settings, channel, top_n=top_n)
    print(f"wrote {path}")
