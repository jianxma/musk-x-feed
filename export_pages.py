#!/usr/bin/env python3
"""Export SQLite posts to paginated static JSON for GitHub Pages.

Writes docs/data/manifest.json and docs/data/page-N.json. Does not rewrite HTML.
Skips the write when post content is unchanged so Actions can avoid empty commits.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import db

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
) -> dict[str, Any]:
    return {
        "posts": posts,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "updated_at_shanghai": updated,
    }


def export_pages(page_size: int = PAGE_SIZE, dest: Path | None = None) -> dict[str, Any]:
    dest = Path(dest) if dest else DEFAULT_DEST
    page_size = max(1, min(100, int(page_size or PAGE_SIZE)))
    db.init_db()
    posts = db.get_all_posts()
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
            payload = _page_payload(chunk, page, page_size, total, total_pages, updated)
            path = dest / f"page-{page}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        manifest = {
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "updated_at_shanghai": updated,
            "pages": [f"page-{i}.json" for i in range(1, total_pages + 1)],
        }
        (dest / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        db.checkpoint()

    print(f"Exported {total} posts, {total_pages} pages -> {dest}")
    print(f"CONTENT_CHANGED={'yes' if changed else 'no'}")
    return {
        "changed": changed,
        "total": total,
        "total_pages": total_pages,
        "updated_at_shanghai": updated,
        "dest": str(dest),
    }


def main() -> int:
    export_pages()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
