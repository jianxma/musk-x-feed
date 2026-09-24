#!/usr/bin/env python3
"""Export SQLite posts to paginated static JSON for GitHub Pages.

Writes one namespace per watched account:

  docs/data/accounts.json
  docs/data/<handle>/manifest.json
  docs/data/<handle>/page-N.json

Does not rewrite HTML. Skips the write when post content is unchanged so
Actions can avoid empty commits.

``export_pages(dest=...)`` still writes a single flat directory for callers
that pass an explicit destination. ``export_pages()`` with no dest exports
every account in the registry.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import db
from accounts import ensure_accounts_file, load_accounts

ROOT = Path(__file__).resolve().parent
DEFAULT_DEST = ROOT / "docs" / "data"
PAGE_SIZE = 20


def content_fingerprint(posts: list[dict]) -> str:
    slim = []
    for p in posts:
        slim.append(
            {
                "id": p.get("id"),
                "type_label": p.get("type_label"),
                "text": p.get("text"),
                "url": p.get("url"),
                "images": p.get("images"),
                "media": p.get("media"),
                "quote": p.get("quote"),
                "retweet": p.get("retweet"),
                "reply_to": p.get("reply_to"),
                "engagement": p.get("engagement"),
                "author": p.get("author"),
                "author_name": p.get("author_name"),
                "author_avatar": p.get("author_avatar"),
                "author_verified": p.get("author_verified"),
                "created_at_utc": p.get("created_at_utc"),
            }
        )
    return json.dumps(slim, ensure_ascii=False, sort_keys=True)


def _read_disk_posts(dest: Path) -> list[dict]:
    posts: list[dict] = []
    if not dest.exists():
        return posts
    files = []
    for path in dest.glob("page-*.json"):
        try:
            n = int(path.stem.split("-", 1)[1])
        except (IndexError, ValueError):
            continue
        files.append((n, path))
    for _n, path in sorted(files):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        posts.extend(data.get("posts") or [])
    return posts


def _page_payload(
    posts: list[dict],
    page: int,
    page_size: int,
    total: int,
    total_pages: int,
    updated: str,
    handle: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "posts": posts,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "updated_at_shanghai": updated,
    }
    if handle:
        payload["account"] = handle
    return payload


def posts_for_handle(handle: str) -> list[dict] | None:
    """Posts to export for ``handle``, or None when that account has no source.

    @elonmusk is the SQLite feed ``sync.py`` already maintains. Another handle
    stays None until a fetcher returns its posts here; existing static files
    under ``docs/data/<handle>/`` are left untouched.
    """
    if handle == "elonmusk":
        db.init_db()
        return db.get_all_posts()
    return None


def _export_dir(
    page_size: int,
    dest: Path,
    posts: list[dict],
    handle: str = "",
) -> dict[str, Any]:
    page_size = max(1, min(100, int(page_size or PAGE_SIZE)))
    total = len(posts)
    total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
    updated = db.latest_updated_at() or ""
    if not updated:
        from feed_core import now_shanghai

        updated = now_shanghai()

    new_fp = content_fingerprint(posts)
    old_posts = _read_disk_posts(dest)
    old_fp = content_fingerprint(old_posts) if old_posts or (dest / "page-1.json").exists() else None
    stale = []
    if dest.exists():
        keep = {f"page-{i}.json" for i in range(1, total_pages + 1)}
        stale = [p for p in dest.glob("page-*.json") if p.name not in keep]
    changed = new_fp != old_fp or bool(stale) or not (dest / "manifest.json").exists()

    if changed:
        dest.mkdir(parents=True, exist_ok=True)
        for p in stale:
            p.unlink()
        for page in range(1, total_pages + 1):
            chunk = posts[(page - 1) * page_size : page * page_size]
            payload = _page_payload(chunk, page, page_size, total, total_pages, updated, handle)
            path = dest / f"page-{page}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        manifest: dict[str, Any] = {
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "updated_at_shanghai": updated,
            "pages": [f"page-{i}.json" for i in range(1, total_pages + 1)],
        }
        if handle:
            manifest = {"account": handle, **manifest}
        (dest / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        db.checkpoint()

    return {
        "changed": changed,
        "total": total,
        "total_pages": total_pages,
        "updated_at_shanghai": updated,
        "dest": str(dest),
        "handle": handle,
    }


def _remove_legacy_flat(data_root: Path) -> bool:
    """Drop pre-namespace ``docs/data/page-N.json`` once a per-account export exists."""
    removed = False
    if not data_root.is_dir():
        return False
    for path in list(data_root.glob("page-*.json")):
        if path.is_file():
            path.unlink()
            removed = True
    manifest = data_root / "manifest.json"
    if manifest.is_file():
        manifest.unlink()
        removed = True
    return removed


def export_all(page_size: int = PAGE_SIZE, data_root: Path | None = None) -> dict[str, Any]:
    data_root = Path(data_root) if data_root else DEFAULT_DEST
    registry = data_root / "accounts.json"
    watched = load_accounts(registry)
    created_registry = ensure_accounts_file(registry, watched)
    changed = created_registry
    results: list[dict[str, Any]] = []
    for acct in watched:
        handle = acct["handle"]
        posts = posts_for_handle(handle)
        if posts is None:
            print(f"Skip @{handle}: no sync source; static files left in place")
            results.append({"handle": handle, "skipped": True, "changed": False})
            continue
        result = _export_dir(page_size, data_root / handle, posts, handle)
        result["skipped"] = False
        changed = changed or bool(result["changed"])
        results.append(result)
        print(
            f"Exported {result['total']} posts, {result['total_pages']} pages -> {result['dest']}"
        )
    exported_any = any(not item.get("skipped") for item in results)
    if exported_any and _remove_legacy_flat(data_root):
        changed = True
        print(f"Removed legacy flat pages under {data_root}")
    print(f"CONTENT_CHANGED={'yes' if changed else 'no'}")
    primary = next(
        (
            item
            for item in results
            if item.get("handle") == "elonmusk" and not item.get("skipped")
        ),
        None,
    )
    return {
        "changed": changed,
        "total": int(primary["total"]) if primary else 0,
        "total_pages": int(primary["total_pages"]) if primary else 0,
        "updated_at_shanghai": (primary or {}).get("updated_at_shanghai") or "",
        "dest": str(data_root),
        "accounts": results,
    }


def export_pages(page_size: int = PAGE_SIZE, dest: Path | None = None) -> dict[str, Any]:
    if dest is None:
        return export_all(page_size=page_size)
    db.init_db()
    posts = db.get_all_posts()
    result = _export_dir(page_size, Path(dest), posts, "")
    print(f"Exported {result['total']} posts, {result['total_pages']} pages -> {result['dest']}")
    print(f"CONTENT_CHANGED={'yes' if result['changed'] else 'no'}")
    return result


def main() -> int:
    export_pages()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
