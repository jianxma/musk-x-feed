#!/usr/bin/env python3
"""Publisher union and the local Pages trigger. No network."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import publish_local_db as publisher  # noqa: E402


def _git(cwd: Path, *args: str, env: dict | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )
    return proc.stdout.strip()


def _git_env() -> dict:
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "test"
    env["GIT_AUTHOR_EMAIL"] = "test@example.com"
    env["GIT_COMMITTER_NAME"] = "test"
    env["GIT_COMMITTER_EMAIL"] = "test@example.com"
    env.pop("MUSK_FEED_DB", None)
    return env


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _db(self, name: str, rows: list[tuple]) -> Path:
        path = self.root / name
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE posts (account TEXT NOT NULL, id TEXT NOT NULL, "
            "text TEXT, enriched INTEGER, PRIMARY KEY (account, id))"
        )
        conn.executemany("INSERT INTO posts (account, id, text, enriched) VALUES (?,?,?,?)", rows)
        conn.commit()
        conn.close()
        return path

    def test_insert_missing_does_not_overwrite_existing_text(self):
        base = self._db("base.db", [("elonmusk", "1", "rich", 1)])
        other = self._db(
            "other.db",
            [("elonmusk", "1", "thin", 1), ("rocketlab", "2", "new", 1)],
        )
        dest = self.root / "out.db"
        stats = publisher.merge_databases(base, dest, [other], [], {"elonmusk", "rocketlab"})
        conn = sqlite3.connect(dest)
        rows = dict(conn.execute("SELECT id, text FROM posts"))
        conn.close()
        self.assertEqual(rows["1"], "rich")
        self.assertEqual(rows["2"], "new")
        self.assertEqual(stats["inserted_db"], 1)

    def test_json_only_post_is_inserted_once(self):
        base = self._db("base.db", [("elonmusk", "1", "kept", 1)])
        data = self.root / "docs" / "data" / "elonmusk"
        data.mkdir(parents=True)
        (data / "page-9.json").write_text(
            json.dumps(
                {
                    "posts": [
                        {
                            "id": "3",
                            "text": "from-json",
                            "type_label": "原文",
                            "enriched": True,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        dest = self.root / "out.db"
        publisher.merge_databases(base, dest, [], [data.parent], {"elonmusk"})
        conn = sqlite3.connect(dest)
        text = dict(conn.execute("SELECT id, text FROM posts"))
        conn.close()
        self.assertEqual(text, {"1": "kept", "3": "from-json"})
        again = self.root / "again.db"
        publisher.merge_databases(dest, again, [], [data.parent], {"elonmusk"})
        conn = sqlite3.connect(again)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 2)
        conn.close()

    def test_check_reports_a_dropped_id(self):
        base = self._db("base.db", [("elonmusk", "1", "a", 1)])
        dest = self.root / "out.db"
        publisher.copy_db(base, dest)
        conn = sqlite3.connect(dest)
        conn.execute("DELETE FROM posts")
        conn.commit()
        conn.close()
        code = publisher.main(["check", "--dest", str(dest), "--source", str(base)])
        self.assertEqual(code, 2)


class TriggerScriptTests(unittest.TestCase):
    def test_script_does_not_reset_or_scrape_secrets(self):
        text = (ROOT / "scripts" / "trigger_pages_update.sh").read_text(encoding="utf-8")
        code = "\n".join(
            line for line in text.splitlines() if not line.strip().startswith("#")
        )
        for banned in (
            "reset --hard",
            "checkout -B",
            "push --force",
            "push -f",
            "git clean",
            "box-secrets",
            "x-access-token:%s",
            "AUTHORIZATION:",
        ):
            self.assertNotIn(banned, code, banned)
        self.assertIn('unset-all "http.https://github.com/.extraheader"', code)
        self.assertNotRegex(code, re.compile(r"git config[^\n]*extraheader[^\n]*AUTHORIZATION"))
        self.assertIn("GIT_ASKPASS", code)
        self.assertIn("publish_local_db.py", code)
        self.assertNotRegex(code, re.compile(r"^\s*python3\s+sync\.py\s+--export", re.M))

    def test_trigger_publishes_union_without_touching_dirty_checkout(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            root = Path(tmp.name)
            env = _git_env()
            seed = root / "seed"
            seed.mkdir()
            for name in ("export_pages.py", "feed_core.py", "db.py", "accounts.py"):
                (seed / name).write_text((ROOT / name).read_text(encoding="utf-8"), encoding="utf-8")
            data = seed / "docs" / "data"
            data.mkdir(parents=True)
            (data / "accounts.json").write_text(
                (ROOT / "docs" / "data" / "accounts.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            write_feed_post(seed / "musk_feed.db", "111", "origin-text", "elonmusk", "2026-11-17T00:00:01.000Z")
            _git(seed, "init", "-b", "main", env=env)
            _git(seed, "add", "-A", env=env)
            _git(seed, "commit", "-m", "seed", env=env)
            bare = root / "origin.git"
            _git(root, "clone", "--bare", str(seed), str(bare), env=env)
            workdir = root / "workdir"
            _git(root, "clone", str(bare), str(workdir), env=env)
            before = _git(workdir, "rev-parse", "HEAD", env=env)
            conn = sqlite3.connect(workdir / "musk_feed.db")
            conn.execute("UPDATE posts SET text = ? WHERE id = ?", ("workdir-text", "111"))
            conn.commit()
            conn.close()
            write_feed_post(
                workdir / "musk_feed.db",
                "222",
                "local-only",
                "elonmusk",
                "2026-11-17T00:00:02.000Z",
            )
            dirty = workdir / "docs" / "data" / "elonmusk"
            dirty.mkdir(parents=True)
            dirty_page = dirty / "page-9.json"
            dirty_page.write_text(
                json.dumps(
                    {
                        "posts": [
                            {
                                "id": "333",
                                "text": "json-only",
                                "type_label": "原文",
                                "enriched": True,
                                "created_at_utc": "2026-11-17T00:00:03.000Z",
                                "author": "elonmusk",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            dirty_bytes = dirty_page.read_bytes()
            monitor = root / "musk-feed"
            monitor.mkdir()
            write_feed_post(
                monitor / "musk_feed.db",
                "444",
                "monitor-rocket",
                "rocketlab",
                "2026-11-17T00:00:04.000Z",
            )

            run_env = env.copy()
            run_env.update(
                {
                    "MUSK_X_FEED_REMOTE": str(bare),
                    "MUSK_X_FEED_WORKDIR": str(workdir),
                    "MUSK_X_FEED_PUBLISH_DIR": str(root / "publish"),
                    "MUSK_FEED_DIR": str(monitor),
                    "MUSK_PAGES_BUILD_LOG": str(root / "build.log"),
                }
            )
            script = ROOT / "scripts" / "trigger_pages_update.sh"
            first = subprocess.run(["bash", str(script)], env=run_env, text=True, capture_output=True)
            self.assertEqual(first.returncode, 0, first.stdout + "\n" + first.stderr)
            self.assertEqual(_git(workdir, "rev-parse", "HEAD", env=env), before)
            self.assertEqual(dirty_page.read_bytes(), dirty_bytes)
            conn = sqlite3.connect(workdir / "musk_feed.db")
            local_rows = dict(conn.execute("SELECT id, text FROM posts"))
            conn.close()
            self.assertEqual(local_rows["111"], "workdir-text")
            self.assertEqual(local_rows["222"], "local-only")
            self.assertNotIn("333", local_rows)
            self.assertNotIn("444", local_rows)

            checked = root / "checked"
            _git(root, "clone", str(bare), str(checked), env=env)
            pages = _page_posts(checked / "docs" / "data")
            self.assertEqual(pages[("elonmusk", "111")]["text"], "workdir-text")
            self.assertEqual(pages[("elonmusk", "222")]["text"], "local-only")
            self.assertEqual(pages[("elonmusk", "333")]["text"], "json-only")
            self.assertEqual(pages[("rocketlab", "444")]["text"], "monitor-rocket")
            commits_after_first = int(_git(bare, "rev-list", "--count", "main", env=env))

            second = subprocess.run(["bash", str(script)], env=run_env, text=True, capture_output=True)
            self.assertEqual(second.returncode, 0, second.stdout + "\n" + second.stderr)
            self.assertIn("no content change", second.stdout)
            self.assertEqual(int(_git(bare, "rev-list", "--count", "main", env=env)), commits_after_first)
            self.assertEqual(_git(workdir, "rev-parse", "HEAD", env=env), before)
        finally:
            tmp.cleanup()


def write_feed_post(db_path: Path, post_id: str, text: str, account: str, created: str) -> None:
    code = textwrap.dedent(
        """
        import os, sys
        os.environ["MUSK_FEED_DB"] = sys.argv[1]
        sys.path.insert(0, sys.argv[2])
        import db
        db.init_db()
        db.upsert_posts([{
            "id": sys.argv[3],
            "account": sys.argv[4],
            "author": sys.argv[4],
            "author_name": "Name",
            "text": sys.argv[5],
            "created_at_utc": sys.argv[6],
            "type_label": "原文",
            "url": "https://x.com/" + sys.argv[4] + "/status/" + sys.argv[3],
            "enriched": True,
            "engagement": {"likes": 1},
        }])
        """
    )
    subprocess.run(
        [sys.executable, "-c", code, str(db_path), str(ROOT), post_id, account, text, created],
        check=True,
        text=True,
    )


def _page_posts(data_root: Path) -> dict[tuple[str, str], dict]:
    found: dict[tuple[str, str], dict] = {}
    for path in data_root.glob("*/*.json"):
        if not path.name.startswith("page-"):
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        account = path.parent.name
        for post in payload.get("posts") or []:
            found[(account, str(post["id"]))] = post
    return found


if __name__ == "__main__":
    unittest.main()
