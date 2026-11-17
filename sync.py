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


def publish_exit_code(accounts: list[dict], *, exported: bool, export_ok: bool) -> int:
    """Exit 0 after a successful export even if one account fetch failed.

    Callers that publish with ``set -e`` (the local Pages trigger) otherwise
    skip ``git commit`` after ``CONTENT_CHANGED=yes``. A fetch gap is reported
    with ``FETCH_INCOMPLETE=yes`` and does not roll back rows already upserted.
    Export itself failing is still fatal. A fetch-only run (no export) stays
    non-zero so the monitor notices the gap.
    """
    if exported and not export_ok:
        return 1
    fetch_failed = any(part.get("fetch_error") for part in accounts)
    if fetch_failed and not (exported and export_ok):
        return 1
    return 0


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
    parser.add_argument(
        "--export-only",
        action="store_true",
        help="write docs/data from the local SQLite DB without fetching",
    )
    args = parser.parse_args()
    if args.export_only:
        from export_pages import export_pages

        try:
            export_pages()
        except Exception as e:
            print(f"FATAL: export failed: {e}", file=sys.stderr)
            return 1
        return 0
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
    exported = False
    export_ok = True
    if args.export:
        # Publish whichever account changed, even if the other fetch failed.
        from export_pages import export_pages

        exported = True
        try:
            export_pages()
        except Exception as e:
            print(f"FATAL: export failed: {e}", file=sys.stderr)
            export_ok = False
    if any(part.get("fetch_error") for part in accounts):
        print("FETCH_INCOMPLETE=yes")
    return publish_exit_code(accounts, exported=exported, export_ok=export_ok)


if __name__ == "__main__":
    raise SystemExit(main())
