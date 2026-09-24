#!/usr/bin/env python3
"""One-shot incremental sync for every watched account.

Usage: python3 sync.py [--export]

Default run scrapes @elonmusk and @rocketlab. ``--export`` writes
``docs/data/<handle>/`` for each. One account failing does not skip the other
or the export.
"""

from __future__ import annotations

import argparse
import json
import sys

from feed_core import sync_incremental


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Incrementally sync every watched X account into SQLite"
    )
    parser.add_argument(
        "--enrich-limit",
        type=int,
        default=20,
        help="max new/unenriched posts to enrich this run (default 20)",
    )
    parser.add_argument(
        "--export",
        action="store_true",
        help="also write paginated docs/data/<handle> JSON for GitHub Pages",
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
    accounts = result.get("accounts") or []
    print(
        json.dumps(
            {
                "inserted": result["inserted"],
                "updated": result["updated"],
                "enriched": result["enriched"],
                "total": result["total"],
                "updated_at_shanghai": result["updated_at_shanghai"],
                "failures": len(result.get("failures") or []),
                "accounts": [
                    {
                        "handle": part.get("handle"),
                        "inserted": part.get("inserted"),
                        "updated": part.get("updated"),
                        "enriched": part.get("enriched"),
                        "fetch_error": bool(part.get("fetch_error")),
                    }
                    for part in accounts
                ],
            },
            ensure_ascii=False,
        )
    )
    for part in accounts:
        handle = part.get("handle") or "?"
        if part.get("fetch_error"):
            print(f"SYNC_ACCOUNT @{handle} failed")
        else:
            print(
                f"SYNC_ACCOUNT @{handle} inserted={part.get('inserted')} "
                f"updated={part.get('updated')} enriched={part.get('enriched')}"
            )
    if args.export:
        # Publish whichever account changed, even if the other fetch failed.
        from export_pages import export_pages

        try:
            export_pages()
        except Exception as e:
            print(f"FATAL: export failed: {e}", file=sys.stderr)
            return 1
    if any(part.get("fetch_error") for part in accounts):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
