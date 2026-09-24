#!/usr/bin/env python3
"""Backward-compatible entry: incremental SQLite sync + static Pages export.

The live site is no longer one giant HTML file. Prefer:

  python3 sync.py
  python3 app.py

GitHub Actions calls `python3 sync.py --export`, which writes docs/data/<handle>/page-N.json.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from export_pages import export_pages  # noqa: E402
from feed_core import sync_incremental  # noqa: E402


def main() -> int:
    try:
        result = sync_incremental(enrich_limit=20, sleep_between=0.1, verbose=True)
    except Exception as e:
        print(f"FATAL: {e}", file=sys.stderr)
        return 1
    print(
        f"sync inserted={result['inserted']} updated={result['updated']} "
        f"enriched={result['enriched']} total={result['total']}"
    )
    export_pages()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
