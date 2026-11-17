#!/usr/bin/env python3
"""Union local feed databases into one SQLite file without overwriting rows.

The Pages publisher starts from the canonical local database, then inserts
``(account, id)`` pairs that are missing from origin, from the monitor's
database, and from paginated JSON still sitting in a dirty checkout. Existing
rows are left as they are, including enriched text and media.

This file is stdlib only so the untracked runner can install it next to
``trigger_pages_update.sh`` before this branch is merged.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
from pathlib import Path

HANDLE_RE = re.compile(r"^[a-z0-9_]{1,15}$")
COLUMN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DEFAULT_ACCOUNTS = ("elonmusk", "rocketlab")


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


def table_columns(conn: sqlite3.Connection, schema: str | None = None) -> list[str]:
    prefix = f"{schema}." if schema else ""
    rows = conn.execute(f"PRAGMA {prefix}table_info(posts)").fetchall()
    cols = [str(row[1]) for row in rows]
    if "account" not in cols or "id" not in cols:
        raise RuntimeError("posts table is missing account or id")
    return cols


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


def insert_missing(dest: Path, sources: list[Path]) -> int:
    """Insert rows whose primary key is absent. Never UPDATE existing rows."""
    inserted = 0
    conn = sqlite3.connect(str(dest))
    try:
        dest_cols = table_columns(conn)
        for index, src in enumerate(sources):
            if not src.is_file():
                raise RuntimeError(f"missing database: {src}")
            alias = f"src{index}"
            conn.execute("ATTACH DATABASE ? AS " + _quote_ident(alias), (str(src),))
            try:
                src_cols = table_columns(conn, alias)
                common = [col for col in dest_cols if col in src_cols]
                col_sql = ", ".join(_quote_ident(col) for col in common)
                before = int(conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0])
                conn.execute(
                    f"""INSERT INTO posts ({col_sql})
                        SELECT {col_sql} FROM {_quote_ident(alias)}.posts AS s
                        WHERE NOT EXISTS (
                            SELECT 1 FROM posts AS p
                            WHERE p.account = s.account AND p.id = s.id
                        )"""
                )
                conn.commit()
                after = int(conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0])
                inserted += after - before
            finally:
                conn.execute("DETACH DATABASE " + _quote_ident(alias))
        return inserted
    finally:
        conn.close()


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
    conn = connect(path, readonly=True)
    try:
        table_columns(conn)
        return {
            (str(account), str(post_id))
            for account, post_id in conn.execute("SELECT account, id FROM posts")
        }
    finally:
        conn.close()


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
    conn = connect(path, readonly=True)
    try:
        columns = table_columns(conn)
        order = "created_at_utc DESC" if "created_at_utc" in columns else "id DESC"
        row = conn.execute(
            f"SELECT id FROM posts WHERE account = ? ORDER BY {order} LIMIT 1",
            (account,),
        ).fetchone()
        return str(row[0]) if row else ""
    finally:
        conn.close()


def merge_databases(
    base: Path,
    dest: Path,
    sources: list[Path],
    json_roots: list[Path],
    accounts: set[str],
) -> dict[str, int]:
    copy_db(base, dest)
    from_db = insert_missing(dest, sources)
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

    args = parser.parse_args(argv)
    accounts = _account_set(getattr(args, "account", []) or [])
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
