#!/usr/bin/env python3
"""One-shot incremental sync. Usage: python3 sync.py [--export]"""

from __future__ import annotations

import argparse
import json
import sys

from feed_core import sync_incremental


def main() -> int:
    parser = argparse.ArgumentParser(description="Incrementally sync the Musk X feed into SQLite")
    parser.add_argument(
        "--enrich-limit",
        type=int,
        default=20,
        help="max new/unenriched posts to enrich this run (default 20)",
    )
    parser.add_argument(
        "--export",
        action="store_true",
        help="also write paginated docs/data JSON for GitHub Pages",
    )
    args = parser.parse_args()
    try:
        result = sync_incremental(
            enrich_limit=max(0, args.enrich_limit),
            sleep_between=0.1,
            verbose=True,
        )
    except Exception as e:
        print(f"FATAL: {e}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "inserted": result["inserted"],
                "updated": result["updated"],
                "enriched": result["enriched"],
                "total": result["total"],
                "updated_at_shanghai": result["updated_at_shanghai"],
                "failures": len(result.get("failures") or []),
            },
            ensure_ascii=False,
        )
    )
    if args.export:
        from export_pages import export_pages

        export_pages()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
