#!/usr/bin/env python3
"""Union local feed databases into one SQLite file without overwriting rows.

The Pages publisher starts from the canonical local database, then inserts
``(account, id)`` pairs that are missing from origin, from the monitor's
database, and from paginated JSON still sitting in a dirty checkout.

The live monitor database at ``/workspace/musk-feed/musk_feed.db`` is the older
13-column table: ``id`` is the primary key and the handle lives in ``author``
(``elonmusk`` or ``RocketLab``). It has no ``account``, ``retweet_json``,
``media_json``, or avatar columns. Those rows are mapped onto the 20-column
Pages schema. ``RocketLab`` becomes account ``rocketlab``. Columns the monitor
does not have stay null. A Pages row that already has those fields is kept
whole; a thinner monitor copy of the same post does not replace it. Neither
source file is opened for writes.

This file is stdlib only so the untracked runner can install it next to
``trigger_pages_update.sh``. ``repair-videos`` is the exception: it imports
``feed_core`` from the publish worktree only when a row still needs a file,
and it never opens the monitor database for writes.

A legacy monitor row has video posters in ``images_json`` and no ``media_json``.
Inserting that row used to freeze the poster, because a later merge will not
replace an existing id. ``repair-videos`` re-fetches only those rows and fills
a real mp4/HLS url. A row that already has one is left alone. A failed fetch
leaves the stored row in place.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable

HANDLE_RE = re.compile(r"^[a-z0-9_]{1,15}$")
COLUMN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VIDEO_THUMB = re.compile(
    r"amplify_video_thumb|ext_tw_video_thumb|tweet_video_thumb",
    re.IGNORECASE,
)
_PLAYABLE_EXT = (".mp4", ".m4v", ".webm", ".m3u8")
DEFAULT_REPAIR_LIMIT = 40
DEFAULT_ACCOUNTS = ("elonmusk", "rocketlab")
# Same columns as db.py. Monitor rows leave the ones they do not store null.
PAGES_COLUMNS = (
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
RICH_FIELDS = (
    "retweet_json",
    "media_json",
    "author_avatar",
    "author_verified_type",
    "reply_to",
    "quote_json",
    "images_json",
    "engagement_json",
    "text",
    "author_verified",
)
PAGES_SCHEMA = """
CREATE TABLE posts (
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
);
CREATE INDEX idx_posts_account_created ON posts(account, created_at_utc DESC);
"""


def _quote_ident(name: str) -> str:
    if not COLUMN_RE.fullmatch(name):
        raise RuntimeError(f"unexpected SQL identifier: {name}")
    return '"' + name + '"'


def connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        # Never fall back to a read-write open: that can checkpoint the
        # canonical database the monitor is still updating.
        uri = path.resolve().as_uri() + "?mode=ro"
        return sqlite3.connect(uri, uri=True)
    return sqlite3.connect(str(path))


def column_names(conn: sqlite3.Connection, schema: str | None = None) -> list[str]:
    prefix = f"{schema}." if schema else ""
    rows = conn.execute(f"PRAGMA {prefix}table_info(posts)").fetchall()
    if not rows:
        raise RuntimeError("posts table is missing")
    return [str(row[1]) for row in rows]


def table_columns(conn: sqlite3.Connection, schema: str | None = None) -> list[str]:
    """Columns of a Pages-schema posts table (must include account and id)."""
    cols = column_names(conn, schema)
    if "account" not in cols or "id" not in cols:
        raise RuntimeError("posts table is missing account or id")
    return cols


def normalize_handle(value: object) -> str:
    raw = str(value or "").strip()
    if raw.startswith("@"):
        raw = raw[1:]
    return raw.strip().lower()


def _filled(value: object) -> bool:
    if value is None or isinstance(value, bool):
        return bool(value)
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip()
    return text not in ("", "null", "[]", "{}", "None")


def richness(row: dict) -> int:
    """Prefer a row that still has media, retweet, or avatar fields.

    The monitor table cannot store those columns, so its copy of a post is
    always the thinner one.
    """
    return sum(2 for field in RICH_FIELDS if _filled(row.get(field)))


def _drops_filled_fields(current: dict, projected: dict) -> bool:
    """True when replacing ``current`` would blank a field it already has."""
    for key, value in current.items():
        if key in ("account", "id"):
            continue
        if _filled(value) and not _filled(projected.get(key)):
            return True
    return False


def project_row(raw: dict, columns: list[str]) -> dict:
    """Map one source row onto the Pages columns. Missing fields stay null."""
    if "account" in columns and normalize_handle(raw.get("account")):
        account = normalize_handle(raw.get("account"))
    elif "author" in columns and normalize_handle(raw.get("author")):
        account = normalize_handle(raw.get("author"))
    else:
        account = ""
    post_id = str(raw.get("id") or "")
    projected = {col: None for col in PAGES_COLUMNS}
    projected["account"] = account
    projected["id"] = post_id
    for col in PAGES_COLUMNS:
        if col in ("account", "id") or col not in columns:
            continue
        if raw.get(col) is not None:
            projected[col] = raw.get(col)
    author = projected.get("author")
    if not _filled(author):
        projected["author"] = account
    elif normalize_handle(author) == account:
        projected["author"] = account
    if projected.get("author_verified") is None:
        projected["author_verified"] = 0
    if projected.get("enriched") is None:
        projected["enriched"] = 0
    return projected


def _snapshot_files(src: Path, snapshot: Path) -> None:
    """Copy the database and its WAL without opening ``src`` for writes."""
    if snapshot.exists():
        snapshot.unlink()
    shutil.copy2(src, snapshot)
    for suffix in ("-wal", "-shm"):
        side = Path(str(src) + suffix)
        target = Path(str(snapshot) + suffix)
        if side.exists():
            shutil.copy2(side, target)
        elif target.exists():
            target.unlink()


def copy_db(src: Path, dest: Path) -> None:
    """Consistent snapshot, including a sibling WAL, without writing ``src``."""
    if not src.is_file():
        raise RuntimeError(f"missing database: {src}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    for suffix in ("-wal", "-shm"):
        side = Path(str(dest) + suffix)
        if side.exists():
            side.unlink()
    try:
        source = connect(src, readonly=True)
    except sqlite3.Error:
        source = None
    if source is not None:
        try:
            target = sqlite3.connect(str(dest))
            try:
                source.backup(target)
                return
            finally:
                target.close()
        finally:
            source.close()
    snapshot = dest.with_name(dest.name + ".snapshot")
    _snapshot_files(src, snapshot)
    source = sqlite3.connect(str(snapshot))
    try:
        target = sqlite3.connect(str(dest))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
        snapshot.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            side = Path(str(snapshot) + suffix)
            if side.exists():
                side.unlink()


def peek_columns(path: Path) -> list[str]:
    conn = connect(path, readonly=True)
    try:
        return column_names(conn)
    finally:
        conn.close()


def iter_source_rows(path: Path):
    """Read posts without writing ``path``.

    Yields ``(account, id, pages_row)``. A monitor database has no ``account``
    column; ``author`` ``RocketLab`` is stored as account ``rocketlab``.
    """
    if not path.is_file():
        raise RuntimeError(f"missing database: {path}")
    conn = connect(path, readonly=True)
    try:
        columns = column_names(conn)
        if "id" not in columns:
            raise RuntimeError(f"{path} posts table has no id column")
        if "account" not in columns and "author" not in columns:
            raise RuntimeError(f"{path} posts table has neither account nor author")
        quoted = ", ".join(_quote_ident(col) for col in columns)
        for row in conn.execute(f"SELECT {quoted} FROM posts"):
            raw = {columns[index]: row[index] for index in range(len(columns))}
            projected = project_row(raw, columns)
            account = str(projected.get("account") or "")
            post_id = str(projected.get("id") or "")
            if not account or not post_id:
                raise RuntimeError(f"{path} has a post with no id or author/account")
            yield account, post_id, projected
    finally:
        conn.close()


def _write_projected(conn: sqlite3.Connection, dest_cols: list[str], projected: dict, *, update: bool) -> None:
    present = [col for col in dest_cols if col in projected]
    if update:
        changing = [col for col in present if col not in ("account", "id")]
        assignments = ", ".join(f"{_quote_ident(col)}=?" for col in changing)
        values = [projected[col] for col in changing]
        values.extend([projected["account"], projected["id"]])
        conn.execute(
            f"UPDATE posts SET {assignments} WHERE {_quote_ident('account')}=? AND {_quote_ident('id')}=?",
            values,
        )
        return
    conn.execute(
        f"INSERT INTO posts ({', '.join(_quote_ident(col) for col in present)}) "
        f"VALUES ({', '.join('?' for _ in present)})",
        [projected[col] for col in present],
    )


def create_pages_db(dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    for suffix in ("-wal", "-shm"):
        side = Path(str(dest) + suffix)
        if side.exists():
            side.unlink()
    conn = sqlite3.connect(str(dest))
    try:
        conn.executescript(PAGES_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def merge_source(dest: Path, src: Path) -> int:
    """Insert ids ``dest`` lacks. Replace a row only when ``src`` is richer.

    A monitor row never replaces a Pages row that already has retweet, media,
    or avatar fields. Returns the number of newly inserted ids.
    """
    inserted = 0
    conn = sqlite3.connect(str(dest))
    try:
        dest_cols = column_names(conn)
        if "account" not in dest_cols or "id" not in dest_cols:
            raise RuntimeError(f"{dest} is not a Pages posts table")
        quoted = ", ".join(_quote_ident(col) for col in dest_cols)
        existing: dict[tuple[str, str], dict] = {}
        for row in conn.execute(f"SELECT {quoted} FROM posts"):
            current = {dest_cols[index]: row[index] for index in range(len(dest_cols))}
            existing[(str(current["account"]), str(current["id"]))] = current
        for account, post_id, projected in iter_source_rows(src):
            key = (account, post_id)
            current = existing.get(key)
            if current is None:
                _write_projected(conn, dest_cols, projected, update=False)
                existing[key] = {col: projected.get(col) for col in dest_cols}
                inserted += 1
                continue
            if _drops_filled_fields(current, projected):
                continue
            if richness(projected) > richness(current):
                _write_projected(conn, dest_cols, projected, update=True)
                existing[key] = {col: projected.get(col) for col in dest_cols}
        conn.commit()
        return inserted
    finally:
        conn.close()


def materialize_base(base: Path, dest: Path) -> None:
    """Copy ``base`` into ``dest``. A monitor file is rewritten to Pages columns."""
    columns = peek_columns(base)
    if "account" in columns:
        copy_db(base, dest)
        return
    create_pages_db(dest)
    merge_source(dest, base)


def _posts_in_dir(directory: Path, account: str) -> list[dict]:
    posts: list[dict] = []
    for path in sorted(directory.glob("page-*.json")):
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"cannot read {path}: {exc}") from exc
        for post in payload.get("posts") or []:
            if not isinstance(post, dict):
                continue
            post_id = str(post.get("id") or "")
            if not post_id:
                continue
            row = dict(post)
            row["account"] = account
            row["id"] = post_id
            posts.append(row)
    return posts


def posts_from_json(roots: list[Path], accounts: set[str]) -> list[dict]:
    """Posts in paginated JSON that this exporter knows how to publish.

    Namespaced files win over a legacy flat ``page-N.json`` because missing-id
    inserts keep the first copy.
    """
    found: list[dict] = []
    for root in roots:
        if not root.is_dir():
            continue
        for child in sorted(path for path in root.iterdir() if path.is_dir()):
            if child.name not in accounts or not HANDLE_RE.fullmatch(child.name):
                continue
            found.extend(_posts_in_dir(child, child.name))
        if "elonmusk" in accounts:
            found.extend(_posts_in_dir(root, "elonmusk"))
    return found


def _json_value(value: object) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def insert_missing_posts(dest: Path, posts: list[dict]) -> int:
    if not posts:
        return 0
    conn = sqlite3.connect(str(dest))
    try:
        columns = table_columns(conn)
        existing = {
            (str(account), str(post_id))
            for account, post_id in conn.execute("SELECT account, id FROM posts")
        }
        inserted = 0
        for post in posts:
            account = str(post.get("account") or "")
            post_id = str(post.get("id") or "")
            if not account or not post_id or (account, post_id) in existing:
                continue
            mapped = {
                "account": account,
                "id": post_id,
                "author": post.get("author") or account,
                "author_name": post.get("author_name") or "",
                "author_avatar": post.get("author_avatar") or "",
                "author_verified": 1 if post.get("author_verified") else 0,
                "author_verified_type": post.get("author_verified_type") or "",
                "created_at_utc": post.get("created_at_utc") or "",
                "created_at_shanghai": post.get("created_at_shanghai") or "",
                "type_label": post.get("type_label") or post.get("type") or "原文",
                "text": post.get("text") or "",
                "quote_json": _json_value(post.get("quote")),
                "retweet_json": _json_value(post.get("retweet"))
                if isinstance(post.get("retweet"), dict)
                else None,
                "reply_to": post.get("reply_to") or "",
                "engagement_json": _json_value(post.get("engagement") or {}),
                "url": post.get("url") or "",
                "images_json": _json_value(post.get("images") or []),
                "media_json": _json_value(post.get("media") or []),
                "enriched": 1 if post.get("enriched") else 0,
                "updated_at": post.get("updated_at") or "",
            }
            present = [col for col in columns if col in mapped]
            conn.execute(
                f"INSERT INTO posts ({', '.join(_quote_ident(col) for col in present)}) "
                f"VALUES ({', '.join('?' for _ in present)})",
                [mapped[col] for col in present],
            )
            existing.add((account, post_id))
            inserted += 1
        conn.commit()
        return inserted
    finally:
        conn.close()


def post_keys(path: Path) -> set[tuple[str, str]]:
    return {(account, post_id) for account, post_id, _row in iter_source_rows(path)}


def missing_keys(dest: Path, sources: list[Path], posts: list[dict]) -> list[tuple[str, str]]:
    have = post_keys(dest)
    need: set[tuple[str, str]] = set()
    for src in sources:
        need |= post_keys(src)
    for post in posts:
        account = str(post.get("account") or "")
        post_id = str(post.get("id") or "")
        if account and post_id:
            need.add((account, post_id))
    return sorted(need - have)


def exported_keys(data_root: Path, accounts: set[str]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for post in posts_from_json([data_root], accounts):
        keys.add((str(post["account"]), str(post["id"])))
    return keys


def db_keys_for_accounts(path: Path, accounts: set[str]) -> set[tuple[str, str]]:
    return {key for key in post_keys(path) if key[0] in accounts}


def latest_id(path: Path, account: str) -> str:
    wanted = normalize_handle(account)
    best_id = ""
    best_created = ""
    for row_account, post_id, projected in iter_source_rows(path):
        if row_account != wanted:
            continue
        created = str(projected.get("created_at_utc") or "")
        if created >= best_created:
            best_created = created
            best_id = post_id
    return best_id


def merge_databases(
    base: Path,
    dest: Path,
    sources: list[Path],
    json_roots: list[Path],
    accounts: set[str],
) -> dict[str, int]:
    materialize_base(base, dest)
    from_db = 0
    for src in sources:
        from_db += merge_source(dest, src)
    posts = posts_from_json(json_roots, accounts)
    from_json = insert_missing_posts(dest, posts)
    absent = missing_keys(dest, [base, *sources], posts)
    if absent:
        sample = ", ".join(f"{account}:{post_id}" for account, post_id in absent[:8])
        raise RuntimeError(f"merge dropped {len(absent)} posts ({sample})")
    total = len(post_keys(dest))
    print(
        f"MERGED posts={total} inserted_db={from_db} inserted_json={from_json} "
        f"base={base}"
    )
    for account in sorted(accounts):
        print(f"LATEST {account}={latest_id(dest, account) or '-'}")
    return {"total": total, "inserted_db": from_db, "inserted_json": from_json}


def _json_or(value: object, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return default
    return default if parsed is None else parsed


def _dump_json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _playable_url(url: str) -> bool:
    path = str(url or "").split("?", 1)[0].lower()
    return path.endswith(_PLAYABLE_EXT)


def _media_playable(media: object) -> bool:
    if not isinstance(media, list):
        return False
    for item in media:
        if not isinstance(item, dict):
            continue
        if _playable_url(str(item.get("url") or "")) or _playable_url(str(item.get("hls_url") or "")):
            return True
    return False


def _is_video_thumb(url: object) -> bool:
    return isinstance(url, str) and bool(_VIDEO_THUMB.search(url))


def _block_needs_repair(images: object, media: object) -> bool:
    """True when a poster is stored and no mp4/HLS url is."""
    if _media_playable(media):
        return False
    if isinstance(images, list) and any(_is_video_thumb(url) for url in images):
        return True
    if not isinstance(media, list):
        return False
    for item in media:
        if not isinstance(item, dict):
            continue
        if item.get("type") in ("video", "gif"):
            return True
        if _is_video_thumb(item.get("url")) or _is_video_thumb(item.get("thumbnail_url")):
            return True
    return False


def _row_needs_repair(post: dict) -> bool:
    if _block_needs_repair(post.get("images") or [], post.get("media") or []):
        return True
    for key in ("quote", "retweet"):
        block = post.get(key)
        if isinstance(block, dict) and _block_needs_repair(block.get("images") or [], block.get("media") or []):
            return True
    return False


def _row_has_playable(post: dict) -> bool:
    if _media_playable(post.get("media")):
        return True
    for key in ("quote", "retweet"):
        block = post.get(key)
        if isinstance(block, dict) and _media_playable(block.get("media")):
            return True
    return False


def _is_stub_block(block: object) -> bool:
    """Author-only quote left by the legacy table. It has no status id or text."""
    if not isinstance(block, dict):
        return False
    if _media_playable(block.get("media")):
        return False
    if str(block.get("text") or "").strip():
        return False
    if block.get("images"):
        return False
    if str(block.get("id") or "").strip():
        return False
    return True


def repair_plan(existing: dict, enriched: dict) -> dict | None:
    """Fields to store when ``enriched`` actually contains a playable file.

    Returns None when there is nothing safe to add. An existing mp4/HLS url is
    never replaced by a poster.
    """
    if not _row_needs_repair(existing) or not _row_has_playable(enriched):
        return None
    from db import _preserve_nested_media, _preserve_playable_media

    media = _preserve_playable_media(existing.get("media"), enriched.get("media"))
    incoming_quote = enriched.get("quote")
    if incoming_quote is None and _is_stub_block(existing.get("quote")):
        quote = None
    elif isinstance(incoming_quote, dict):
        quote = _preserve_nested_media(existing.get("quote"), incoming_quote)
    else:
        quote = existing.get("quote")
    incoming_retweet = enriched.get("retweet")
    if isinstance(incoming_retweet, dict):
        retweet = _preserve_nested_media(existing.get("retweet"), incoming_retweet)
    else:
        retweet = existing.get("retweet")
    images = enriched.get("images") or existing.get("images") or []
    return {
        "type_label": enriched.get("type_label") or existing.get("type_label") or "",
        "images": images,
        "media": media or [],
        "quote": quote,
        "retweet": retweet,
    }


def _post_from_sql(row: sqlite3.Row) -> dict:
    return {
        "account": str(row["account"] or ""),
        "id": str(row["id"] or ""),
        "type_label": row["type_label"] or "",
        "url": row["url"] or "",
        "created_at_utc": row["created_at_utc"] or "",
        "images": _json_or(row["images_json"], []),
        "media": _json_or(row["media_json"], []),
        "quote": _json_or(row["quote_json"], None),
        "retweet": _json_or(row["retweet_json"], None),
    }


def default_video_fetch(account: str, post_id: str, existing_url: str) -> dict:
    """Read one status from FxTwitter. Raises when the status cannot be read."""
    from feed_core import build_post, enrich_one

    tweet, err = enrich_one(post_id, account)
    if not isinstance(tweet, dict):
        raise RuntimeError(err or "missing tweet")
    return build_post(
        {
            "platformId": post_id,
            "content": "",
            "createdAt": "",
            "url": existing_url or "",
        },
        tweet,
        owner={"handle": account},
    )


def repair_thin_videos(
    db_path: Path,
    *,
    fetch: Callable[[str, str, str], dict],
    limit: int = DEFAULT_REPAIR_LIMIT,
    ids: list[str] | None = None,
    accounts: set[str] | None = None,
) -> dict[str, int]:
    """Fill playable media on Pages rows that only have a video poster.

    ``fetch(account, id, url)`` returns a ``build_post`` dict or raises.
    The database at ``db_path`` must already be the Pages schema. This never
    opens the legacy monitor file.
    """
    wanted = {str(item) for item in ids or [] if str(item).strip()}
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(posts)")}
        required = {"account", "id", "images_json", "media_json", "quote_json", "retweet_json", "type_label"}
        if not required <= cols:
            raise RuntimeError(
                "repair-videos needs the Pages posts table; refusing to alter a legacy monitor database"
            )
        selected: list[dict] = []
        for row in conn.execute(
            """SELECT account, id, type_label, url, created_at_utc,
                      images_json, media_json, quote_json, retweet_json
               FROM posts"""
        ):
            post = _post_from_sql(row)
            if accounts and post["account"] not in accounts:
                continue
            if wanted and post["id"] not in wanted:
                continue
            if not _row_needs_repair(post):
                continue
            selected.append(post)
        selected.sort(key=lambda item: (item.get("created_at_utc") or "", item["id"]), reverse=True)
        if not wanted:
            selected = selected[: max(0, limit)]
        repaired = 0
        failed = 0
        no_video = 0
        for post in selected:
            label = f"{post['account']}:{post['id']}"
            try:
                enriched = fetch(post["account"], post["id"], post.get("url") or "")
            except Exception as exc:
                failed += 1
                print(f"VIDEO_ENRICH_INCOMPLETE {label} {exc}", flush=True)
                continue
            if not isinstance(enriched, dict):
                failed += 1
                print(f"VIDEO_ENRICH_INCOMPLETE {label} empty", flush=True)
                continue
            plan = repair_plan(post, enriched)
            if plan is None:
                no_video += 1
                print(f"VIDEO_REPAIR_UNAVAILABLE {label}", flush=True)
                continue
            conn.execute(
                """UPDATE posts SET type_label=?, images_json=?, media_json=?,
                                    quote_json=?, retweet_json=?
                   WHERE account=? AND id=?""",
                (
                    plan["type_label"],
                    _dump_json(plan["images"]),
                    _dump_json(plan["media"]),
                    _dump_json(plan["quote"]),
                    _dump_json(plan["retweet"]),
                    post["account"],
                    post["id"],
                ),
            )
            repaired += 1
            print(f"VIDEO_REPAIR {label}", flush=True)
        conn.commit()
        stats = {
            "candidates": len(selected),
            "repaired": repaired,
            "failed": failed,
            "no_video": no_video,
        }
        print(
            f"VIDEO_REPAIR repaired={repaired} failed={failed} "
            f"no_video={no_video} candidates={len(selected)}",
            flush=True,
        )
        return stats
    finally:
        conn.close()


def _account_set(values: list[str]) -> set[str]:
    accounts = {item.strip().lstrip("@").lower() for item in values if item.strip()}
    return accounts or set(DEFAULT_ACCOUNTS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Union local feed SQLite files")
    sub = parser.add_subparsers(dest="cmd", required=True)

    merge = sub.add_parser("merge")
    merge.add_argument("--base", required=True)
    merge.add_argument("--dest", required=True)
    merge.add_argument("--source", action="append", default=[])
    merge.add_argument("--json-root", action="append", default=[])
    merge.add_argument("--account", action="append", default=[])

    check = sub.add_parser("check")
    check.add_argument("--dest", required=True)
    check.add_argument("--source", action="append", default=[])
    check.add_argument("--json-root", action="append", default=[])
    check.add_argument("--account", action="append", default=[])

    exported = sub.add_parser("check-export")
    exported.add_argument("--db", required=True)
    exported.add_argument("--data-root", required=True)
    exported.add_argument("--account", action="append", default=[])

    repair = sub.add_parser("repair-videos")
    repair.add_argument("--db", required=True)
    repair.add_argument("--limit", type=int, default=DEFAULT_REPAIR_LIMIT)
    repair.add_argument("--id", action="append", default=[])
    repair.add_argument("--account", action="append", default=[])

    args = parser.parse_args(argv)
    accounts = _account_set(getattr(args, "account", []) or [])
    if args.cmd == "repair-videos":
        # An empty --account list means every account in the database.
        # _account_set would otherwise fill the historical default pair and
        # skip a third watched account such as evavlaar.
        repair_accounts = {
            item.strip().lstrip("@").lower()
            for item in (args.account or [])
            if item.strip()
        }
        try:
            repair_thin_videos(
                Path(args.db),
                fetch=default_video_fetch,
                limit=max(0, args.limit),
                ids=list(args.id or []),
                accounts=repair_accounts or None,
            )
        except RuntimeError as exc:
            print(f"ABORT: {exc}", file=sys.stderr)
            return 1
        return 0
    if args.cmd == "merge":
        merge_databases(
            Path(args.base),
            Path(args.dest),
            [Path(item) for item in args.source],
            [Path(item) for item in args.json_root],
            accounts,
        )
        return 0
    if args.cmd == "check":
        posts = posts_from_json([Path(item) for item in args.json_root], accounts)
        absent = missing_keys(
            Path(args.dest),
            [Path(item) for item in args.source],
            posts,
        )
        if absent:
            sample = ", ".join(f"{account}:{post_id}" for account, post_id in absent[:12])
            print(f"MISSING {len(absent)} posts: {sample}", flush=True)
            return 2
        print(f"CHECK ok posts={len(post_keys(Path(args.dest)))}")
        return 0
    need = db_keys_for_accounts(Path(args.db), accounts)
    have = exported_keys(Path(args.data_root), accounts)
    absent = sorted(need - have)
    if absent:
        sample = ", ".join(f"{account}:{post_id}" for account, post_id in absent[:12])
        print(f"EXPORT_MISSING {len(absent)} posts: {sample}", flush=True)
        return 2
    print(f"EXPORT_CHECK ok posts={len(need)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
