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

# One row per watched account + status id. Retweet rows from a timeline can
# reuse the original status id, so id alone is not unique across accounts.
_CREATE_POSTS = """
CREATE TABLE IF NOT EXISTS posts (
    account TEXT NOT NULL DEFAULT 'elonmusk',
    id TEXT NOT NULL,
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
    media_json TEXT,
    enriched INTEGER DEFAULT 0,
    updated_at TEXT,
    PRIMARY KEY (account, id)
)
"""
_CREATE_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_posts_account_created "
    "ON posts(account, created_at_utc DESC)"
)
SCHEMA = _CREATE_POSTS + ";\n" + _CREATE_INDEX + ";\n"
_POST_COLUMNS = (
    "account",
    "id",
    "author",
    "author_name",
    "author_avatar",
    "author_verified",
    "author_verified_type",
    "created_at_utc",
    "created_at_shanghai",
    "type_label",
    "text",
    "quote_json",
    "retweet_json",
    "reply_to",
    "engagement_json",
    "url",
    "images_json",
    "media_json",
    "enriched",
    "updated_at",
)

_EXTRA_COLUMNS = {
    "author_avatar": "TEXT",
    "author_verified": "INTEGER DEFAULT 0",
    "author_verified_type": "TEXT",
    "retweet_json": "TEXT",
    "reply_to": "TEXT",
    "media_json": "TEXT",
}


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _pk_columns(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("PRAGMA table_info(posts)").fetchall()
    pk = sorted((int(row["pk"]), str(row["name"])) for row in rows if row["pk"])
    return [name for _order, name in pk]


def _rebuild_posts(conn: sqlite3.Connection) -> None:
    """Move an id-only table onto PRIMARY KEY (account, id).

    Rows written before accounts were split belong to @elonmusk.
    """
    have = {row["name"] for row in conn.execute("PRAGMA table_info(posts)").fetchall()}
    conn.execute("ALTER TABLE posts RENAME TO posts_legacy")
    conn.execute(_CREATE_POSTS)
    conn.execute(_CREATE_INDEX)
    selects: list[str] = []
    for col in _POST_COLUMNS:
        if col == "account":
            if "account" in have:
                selects.append("COALESCE(NULLIF(account, ''), 'elonmusk')")
            else:
                selects.append("'elonmusk'")
        elif col in have:
            selects.append(col)
        elif col in {"author_verified", "enriched"}:
            selects.append("0")
        else:
            selects.append("NULL")
    conn.execute(
        f"INSERT INTO posts ({', '.join(_POST_COLUMNS)}) "
        f"SELECT {', '.join(selects)} FROM posts_legacy"
    )
    conn.execute("DROP TABLE posts_legacy")


def _migrate(conn: sqlite3.Connection) -> None:
    have = {row["name"] for row in conn.execute("PRAGMA table_info(posts)").fetchall()}
    if not have:
        return
    if _pk_columns(conn) != ["account", "id"]:
        _rebuild_posts(conn)
        have = {row["name"] for row in conn.execute("PRAGMA table_info(posts)").fetchall()}
    for name, decl in _EXTRA_COLUMNS.items():
        if name not in have:
            conn.execute(f"ALTER TABLE posts ADD COLUMN {name} {decl}")
    conn.execute("UPDATE posts SET account = 'elonmusk' WHERE account IS NULL OR account = ''")


def init_db() -> None:
    with _connect() as conn:
        # Old databases are keyed by id only and have no account column.
        # Creating the account index before migrate would fail on those files.
        conn.execute(_CREATE_POSTS)
        _migrate(conn)
        conn.execute(_CREATE_INDEX)
    seed_from_feed_json_if_empty()


def count_posts(account: str | None = None) -> int:
    with _connect() as conn:
        if account:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM posts WHERE account = ?",
                (account,),
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS c FROM posts").fetchone()
        return int(row["c"] if row else 0)


def latest_updated_at(account: str | None = None) -> str:
    with _connect() as conn:
        if account:
            row = conn.execute(
                "SELECT MAX(updated_at) AS u FROM posts WHERE account = ?",
                (account,),
            ).fetchone()
        else:
            row = conn.execute("SELECT MAX(updated_at) AS u FROM posts").fetchone()
        return str(row["u"]) if row and row["u"] else ""


def get_known_ids(account: str | None = None) -> set[str]:
    with _connect() as conn:
        if account:
            rows = conn.execute(
                "SELECT id FROM posts WHERE account = ?",
                (account,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT id FROM posts").fetchall()
        return {str(r["id"]) for r in rows}


def get_unenriched_ids(limit: int = 20, account: str | None = None) -> list[str]:
    with _connect() as conn:
        if account:
            rows = conn.execute(
                "SELECT id FROM posts WHERE enriched = 0 AND account = ? "
                "ORDER BY created_at_utc DESC LIMIT ?",
                (account, int(limit)),
            ).fetchall()
        else:
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
    # Older rows predate avatar/verified columns. Musk rows keep his defaults.
    if author == "elonmusk":
        avatar = avatar or DEFAULT_MUSK_AVATAR
        if not verified:
            verified = True
            vtype = vtype or "individual"
    name_fallback = "Elon Musk" if author == "elonmusk" else author
    return {
        "id": row["id"],
        "author": author,
        "author_name": row["author_name"] or name_fallback,
        "author_avatar": avatar,
        "author_verified": verified,
        "author_verified_type": vtype,
        "created_at_utc": row["created_at_utc"] or "",
        "created_at_shanghai": row["created_at_shanghai"] or "",
        "type": row["type_label"] or "原文",
        "type_label": row["type_label"] or "原文",
        "text": normalize_text(row["text"]),
        "quote": _normalize_quote(_loads(row["quote_json"], None)),
        "retweet": _normalize_quote(_loads(col("retweet_json"), None)),
        "reply_to": col("reply_to") or "",
        "engagement": _loads(row["engagement_json"], {}),
        "url": row["url"] or "",
        "images": _loads(row["images_json"], []),
        "media": _clean_media(_loads(col("media_json"), [])),
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
        "media": row.get("media") or [],
        "enriched": 1 if row.get("enriched") else 0,
    }
    return json.dumps(slim, ensure_ascii=False, sort_keys=True)


def _normalize_quote(quote: Any) -> Any:
    if not isinstance(quote, dict):
        return None
    out = dict(quote)
    out["text"] = normalize_text(out.get("text") or "")
    return out


def _account_of(p: dict) -> str:
    raw = str(p.get("account") or "elonmusk").strip()
    if raw.startswith("@"):
        raw = raw[1:]
    raw = raw.strip().lower()
    return raw or "elonmusk"


def _prepare(p: dict) -> dict[str, Any]:
    quote = _normalize_quote(p.get("quote"))
    retweet = _normalize_quote(p.get("retweet")) if isinstance(p.get("retweet"), dict) else None
    if isinstance(retweet, dict):
        retweet["text"] = normalize_text(retweet.get("text") or "")
    images = p.get("images") or []
    if not isinstance(images, list):
        images = []
    images = [str(u) for u in images if u]
    media = _clean_media(p.get("media"))
    account = _account_of(p)
    author_default = account
    name_default = "Elon Musk" if account == "elonmusk" else account
    return {
        "account": account,
        "id": str(p.get("id") or ""),
        "author": p.get("author") or author_default,
        "author_name": p.get("author_name") or name_default,
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
        "media": media,
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
        if not out.get("media") and existing.get("media"):
            out["media"] = existing["media"]
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


def _clean_media(value: Any) -> list:
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        mtype = item.get("type")
        if mtype not in {"photo", "video", "gif", "audio"}:
            continue
        url = item.get("url") or ""
        thumb = item.get("thumbnail_url") or ""
        if url and not str(url).startswith("http"):
            continue
        if not url and not (isinstance(thumb, str) and thumb.startswith("http")):
            continue
        rec: dict[str, Any] = {"type": mtype, "url": str(url)}
        if isinstance(thumb, str) and thumb.startswith("http"):
            rec["thumbnail_url"] = thumb
        for key in ("width", "height", "duration"):
            num = item.get(key)
            if isinstance(num, (int, float)) and not isinstance(num, bool):
                rec[key] = num
        out.append(rec)
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
        known_rows: dict[tuple[str, str], dict[str, Any]] = {}
        for r in conn.execute("SELECT * FROM posts").fetchall():
            post = _row_to_post(r)
            acct = _account_of({"account": r["account"] if "account" in r.keys() else "elonmusk"})
            post["account"] = acct
            known_rows[(acct, str(r["id"]))] = post
        for raw in posts:
            inc = _prepare(raw)
            tid = inc["id"]
            acct = inc["account"]
            if not tid:
                continue
            key = (acct, tid)
            if key in known_rows:
                merged = _merge(known_rows[key], inc)
                merged["id"] = tid
                merged["account"] = acct
                if _material(merged) == _material(known_rows[key]):
                    continue
                conn.execute(
                    """UPDATE posts SET
                        author=?, author_name=?, author_avatar=?, author_verified=?,
                        author_verified_type=?, created_at_utc=?, created_at_shanghai=?,
                        type_label=?, text=?, quote_json=?, retweet_json=?, reply_to=?,
                        engagement_json=?, url=?, images_json=?, media_json=?, enriched=?, updated_at=?
                    WHERE account=? AND id=?""",
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
                        _dump(merged.get("media") or []),
                        merged["enriched"],
                        now,
                        acct,
                        tid,
                    ),
                )
                known_rows[key] = dict(merged)
                updated += 1
            else:
                conn.execute(
                    """INSERT INTO posts (
                        account, id, author, author_name, author_avatar, author_verified,
                        author_verified_type, created_at_utc, created_at_shanghai,
                        type_label, text, quote_json, retweet_json, reply_to,
                        engagement_json, url, images_json, media_json, enriched, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        acct,
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
                        _dump(inc.get("media") or []),
                        inc["enriched"],
                        now,
                    ),
                )
                known_rows[key] = dict(inc)
                inserted += 1
        if inserted or updated:
            conn.commit()
    return {"inserted": inserted, "updated": updated}


def get_posts_page(page: int = 1, page_size: int = 20, account: str | None = None) -> dict[str, Any]:
    page = max(1, int(page or 1))
    page_size = max(1, min(100, int(page_size or 20)))
    total = count_posts(account)
    total_pages = max(1, math.ceil(total / page_size)) if total else 1
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * page_size
    with _connect() as conn:
        if account:
            rows = conn.execute(
                "SELECT * FROM posts WHERE account = ? ORDER BY created_at_utc DESC LIMIT ? OFFSET ?",
                (account, page_size, offset),
            ).fetchall()
            meta = conn.execute(
                "SELECT MAX(updated_at) AS u FROM posts WHERE account = ?",
                (account,),
            ).fetchone()
        else:
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


def get_all_posts(account: str | None = None) -> list[dict[str, Any]]:
    with _connect() as conn:
        if account:
            rows = conn.execute(
                "SELECT * FROM posts WHERE account = ? ORDER BY created_at_utc DESC",
                (account,),
            ).fetchall()
        else:
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
