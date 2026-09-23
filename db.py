#!/usr/bin/env python3
"""SQLite persistence for the Musk X feed."""

from __future__ import annotations

import json
import math
import os
import sqlite3
from pathlib import Path
from typing import Any

from feed_core import DEFAULT_MUSK_AVATAR, normalize_text, now_shanghai

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("MUSK_FEED_DB", str(ROOT / "musk_feed.db")))
FEED_JSON = ROOT / "docs" / "feed.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY,
    author TEXT,
    author_name TEXT,
    author_avatar TEXT,
    author_verified INTEGER DEFAULT 0,
    author_verified_type TEXT,
    created_at_utc TEXT,
    created_at_shanghai TEXT,
    type_label TEXT,
    text TEXT,
    quote_json TEXT,
    retweet_json TEXT,
    reply_to TEXT,
    engagement_json TEXT,
    url TEXT,
    images_json TEXT,
    enriched INTEGER DEFAULT 0,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at_utc DESC);
"""

_EXTRA_COLUMNS = {
    "author_avatar": "TEXT",
    "author_verified": "INTEGER DEFAULT 0",
    "author_verified_type": "TEXT",
    "retweet_json": "TEXT",
    "reply_to": "TEXT",
}


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    have = {row["name"] for row in conn.execute("PRAGMA table_info(posts)").fetchall()}
    for name, decl in _EXTRA_COLUMNS.items():
        if name not in have:
            conn.execute(f"ALTER TABLE posts ADD COLUMN {name} {decl}")


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
    seed_from_feed_json_if_empty()


def count_posts() -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM posts").fetchone()
        return int(row["c"] if row else 0)


def latest_updated_at() -> str:
    with _connect() as conn:
        row = conn.execute("SELECT MAX(updated_at) AS u FROM posts").fetchone()
        return str(row["u"]) if row and row["u"] else ""


def get_known_ids() -> set[str]:
    with _connect() as conn:
        rows = conn.execute("SELECT id FROM posts").fetchall()
        return {str(r["id"]) for r in rows}


def get_unenriched_ids(limit: int = 20) -> list[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id FROM posts WHERE enriched = 0 "
            "ORDER BY created_at_utc DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [str(r["id"]) for r in rows]


def checkpoint() -> None:
    with _connect() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _loads(s: str | None, default: Any) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except (TypeError, json.JSONDecodeError):
        return default


def _row_to_post(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())

    def col(name: str, default: Any = None) -> Any:
        if name not in keys:
            return default
        return row[name]

    author = row["author"] or "elonmusk"
    avatar = col("author_avatar") or ""
    verified = bool(col("author_verified") or 0)
    vtype = col("author_verified_type") or ""
    # Older rows predate avatar/verified columns. The feed owner is always Musk.
    if author == "elonmusk":
        avatar = avatar or DEFAULT_MUSK_AVATAR
        if not verified:
            verified = True
            vtype = vtype or "individual"
    return {
        "id": row["id"],
        "author": author,
        "author_name": row["author_name"] or "Elon Musk",
        "author_avatar": avatar,
        "author_verified": verified,
        "author_verified_type": vtype,
        "created_at_utc": row["created_at_utc"] or "",
        "created_at_shanghai": row["created_at_shanghai"] or "",
        "type": row["type_label"] or "原文",
        "type_label": row["type_label"] or "原文",
        "text": row["text"] or "",
        "quote": _loads(row["quote_json"], None),
        "retweet": _loads(col("retweet_json"), None),
        "reply_to": col("reply_to") or "",
        "engagement": _loads(row["engagement_json"], {}),
        "url": row["url"] or "",
        "images": _loads(row["images_json"], []),
        "enriched": bool(row["enriched"]),
    }


def _dump(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _material(row: dict[str, Any]) -> str:
    slim = {
        "author": row.get("author") or "",
        "author_name": row.get("author_name") or "",
        "author_avatar": row.get("author_avatar") or "",
        "author_verified": 1 if row.get("author_verified") else 0,
        "author_verified_type": row.get("author_verified_type") or "",
        "created_at_utc": row.get("created_at_utc") or "",
        "created_at_shanghai": row.get("created_at_shanghai") or "",
        "type_label": row.get("type_label") or "",
        "text": row.get("text") or "",
        "quote": row.get("quote"),
        "retweet": row.get("retweet"),
        "reply_to": row.get("reply_to") or "",
        "engagement": row.get("engagement") or {},
        "url": row.get("url") or "",
        "images": row.get("images") or [],
        "enriched": 1 if row.get("enriched") else 0,
    }
    return json.dumps(slim, ensure_ascii=False, sort_keys=True)


def _normalize_quote(quote: Any) -> Any:
    if not isinstance(quote, dict):
        return None
    out = dict(quote)
    out["text"] = normalize_text(out.get("text") or "")
    return out


def _prepare(p: dict) -> dict[str, Any]:
    quote = _normalize_quote(p.get("quote"))
    retweet = _normalize_quote(p.get("retweet")) if isinstance(p.get("retweet"), dict) else None
    if isinstance(retweet, dict):
        retweet["text"] = normalize_text(retweet.get("text") or "")
    images = p.get("images") or []
    if not isinstance(images, list):
        images = []
    images = [str(u) for u in images if u]
    return {
        "id": str(p.get("id") or ""),
        "author": p.get("author") or "elonmusk",
        "author_name": p.get("author_name") or "Elon Musk",
        "author_avatar": p.get("author_avatar") or "",
        "author_verified": 1 if p.get("author_verified") else 0,
        "author_verified_type": p.get("author_verified_type") or "",
        "created_at_utc": p.get("created_at_utc") or "",
        "created_at_shanghai": p.get("created_at_shanghai") or "",
        "type_label": p.get("type_label") or p.get("type") or "原文",
        "text": normalize_text(p.get("text") or ""),
        "quote": quote,
        "retweet": retweet,
        "reply_to": p.get("reply_to") or "",
        "engagement": p.get("engagement") or {},
        "url": p.get("url") or "",
        "images": images,
        "enriched": 1 if p.get("enriched") else 0,
    }


def _merge(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Keep richer enriched fields when a later primary upsert is thinner."""
    inc_enriched = bool(incoming["enriched"])
    ex_enriched = bool(existing["enriched"])
    out = dict(incoming)

    # A primary-only payload must not wipe quote/repost/media already stored,
    # including while a row is queued to be enriched again.
    if not inc_enriched:
        ex_text = existing.get("text") or ""
        if ex_text and (not out["text"] or (ex_enriched and len(out["text"]) < len(ex_text))):
            if ex_enriched or not out["text"]:
                out["text"] = ex_text
        if out["type_label"] == "原文" and existing.get("type_label") not in (None, "", "原文"):
            out["type_label"] = existing["type_label"]
        if not out["quote"] and existing.get("quote"):
            out["quote"] = existing["quote"]
        if not out["retweet"] and existing.get("retweet"):
            out["retweet"] = existing["retweet"]
        if not out["images"] and existing.get("images"):
            out["images"] = existing["images"]
        ex_eng = existing.get("engagement") or {}
        inc_eng = out.get("engagement") or {}
        if inc_eng in ({}, None) and ex_eng:
            out["engagement"] = ex_eng
        if not out["reply_to"] and existing.get("reply_to"):
            out["reply_to"] = existing["reply_to"]
        if ex_enriched:
            out["enriched"] = 1

    if not out["author_avatar"] and existing.get("author_avatar"):
        out["author_avatar"] = existing["author_avatar"]
    if not out["author_verified"] and existing.get("author_verified"):
        out["author_verified"] = 1
        if not out["author_verified_type"]:
            out["author_verified_type"] = existing.get("author_verified_type") or ""
    # Prefer a real remote image list over empty, but let a fresh enrich clear stale local paths.
    if inc_enriched:
        pass
    elif existing.get("images") and not _http_images(out["images"]) and _http_images(existing["images"]):
        out["images"] = existing["images"]
    return out


def _http_images(images: list) -> bool:
    return any(str(u).startswith("http") for u in images or [])


def upsert_posts(posts: list[dict]) -> dict[str, int]:
    """Insert or update. No-op rows are not rewritten (keeps updated_at stable)."""
    if not posts:
        return {"inserted": 0, "updated": 0}
    inserted = 0
    updated = 0
    now = now_shanghai()
    with _connect() as conn:
        _migrate(conn)
        known_rows = {
            str(r["id"]): _row_to_post(r)
            for r in conn.execute("SELECT * FROM posts").fetchall()
        }
        for raw in posts:
            inc = _prepare(raw)
            tid = inc["id"]
            if not tid:
                continue
            if tid in known_rows:
                merged = _merge(known_rows[tid], inc)
                merged["id"] = tid
                if _material(merged) == _material(known_rows[tid]):
                    continue
                conn.execute(
                    """UPDATE posts SET
                        author=?, author_name=?, author_avatar=?, author_verified=?,
                        author_verified_type=?, created_at_utc=?, created_at_shanghai=?,
                        type_label=?, text=?, quote_json=?, retweet_json=?, reply_to=?,
                        engagement_json=?, url=?, images_json=?, enriched=?, updated_at=?
                    WHERE id=?""",
                    (
                        merged["author"],
                        merged["author_name"],
                        merged["author_avatar"],
                        merged["author_verified"],
                        merged["author_verified_type"],
                        merged["created_at_utc"],
                        merged["created_at_shanghai"],
                        merged["type_label"],
                        merged["text"],
                        _dump(merged["quote"]),
                        _dump(merged["retweet"]),
                        merged["reply_to"],
                        _dump(merged["engagement"]),
                        merged["url"],
                        _dump(merged["images"]),
                        merged["enriched"],
                        now,
                        tid,
                    ),
                )
                merged_row = dict(merged)
                known_rows[tid] = merged_row
                updated += 1
            else:
                conn.execute(
                    """INSERT INTO posts (
                        id, author, author_name, author_avatar, author_verified,
                        author_verified_type, created_at_utc, created_at_shanghai,
                        type_label, text, quote_json, retweet_json, reply_to,
                        engagement_json, url, images_json, enriched, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        tid,
                        inc["author"],
                        inc["author_name"],
                        inc["author_avatar"],
                        inc["author_verified"],
                        inc["author_verified_type"],
                        inc["created_at_utc"],
                        inc["created_at_shanghai"],
                        inc["type_label"],
                        inc["text"],
                        _dump(inc["quote"]),
                        _dump(inc["retweet"]),
                        inc["reply_to"],
                        _dump(inc["engagement"]),
                        inc["url"],
                        _dump(inc["images"]),
                        inc["enriched"],
                        now,
                    ),
                )
                known_rows[tid] = dict(inc)
                inserted += 1
        if inserted or updated:
            conn.commit()
    return {"inserted": inserted, "updated": updated}


def get_posts_page(page: int = 1, page_size: int = 20) -> dict[str, Any]:
    page = max(1, int(page or 1))
    page_size = max(1, min(100, int(page_size or 20)))
    total = count_posts()
    total_pages = max(1, math.ceil(total / page_size)) if total else 1
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * page_size
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM posts ORDER BY created_at_utc DESC LIMIT ? OFFSET ?",
            (page_size, offset),
        ).fetchall()
        meta = conn.execute("SELECT MAX(updated_at) AS u FROM posts").fetchone()
    return {
        "posts": [_row_to_post(r) for r in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "updated_at_shanghai": (meta["u"] if meta and meta["u"] else now_shanghai()),
    }


def get_all_posts() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM posts ORDER BY created_at_utc DESC"
        ).fetchall()
    return [_row_to_post(r) for r in rows]


def _seed_needs_reenrich(post: dict) -> bool:
    """Old static feed stored other people's URLs and local media/ paths."""
    tid = str(post.get("id") or "")
    url = str(post.get("url") or "")
    if tid and f"x.com/elonmusk/status/{tid}" not in url:
        return True
    for img in post.get("images") or []:
        if not str(img).startswith("http"):
            return True
    meta = post.get("image_meta") or []
    for item in meta:
        if isinstance(item, dict) and not str(item.get("src") or "").startswith("http"):
            return True
    return False


def seed_from_feed_json_if_empty() -> int:
    if count_posts() > 0:
        return 0
    if not FEED_JSON.exists():
        return 0
    try:
        data = json.loads(FEED_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    posts = data.get("posts") or []
    if not posts:
        return 0
    cleaned: list[dict] = []
    for p in posts:
        item = dict(p)
        item["text"] = normalize_text(item.get("text") or "")
        q = item.get("quote")
        if isinstance(q, dict):
            q = dict(q)
            q["text"] = normalize_text(q.get("text") or "")
            item["quote"] = q
        if _seed_needs_reenrich(item):
            item["enriched"] = False
            item["images"] = [
                u for u in (item.get("images") or []) if str(u).startswith("http")
            ]
        cleaned.append(item)
    result = upsert_posts(cleaned)
    return result["inserted"] + result["updated"]
