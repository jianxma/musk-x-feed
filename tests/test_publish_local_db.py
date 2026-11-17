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

    def test_monitor_schema_keeps_richer_repo_rows_and_new_monitor_posts(self):
        """Regression for the sanitized 13-column monitor DB and 20-column repo DB."""
        monitor = self._monitor_db("monitor.db")
        repo = self._repo_db("repo.db")
        monitor_bytes = monitor.read_bytes()
        repo_bytes = repo.read_bytes()
        for base, source, label in (
            (repo, monitor, "repo-base"),
            (monitor, repo, "monitor-base"),
        ):
            dest = self.root / f"out-{label}.db"
            publisher.merge_databases(base, dest, [source], [], {"elonmusk", "rocketlab"})
            conn = sqlite3.connect(dest)
            rows = {
                (account, post_id): (text, retweet, media, avatar, author)
                for account, post_id, text, retweet, media, avatar, author in conn.execute(
                    "SELECT account, id, text, retweet_json, media_json, author_avatar, author FROM posts"
                )
            }
            columns = [row[1] for row in conn.execute("PRAGMA table_info(posts)")]
            conn.close()
            self.assertIn("account", columns, label)
            self.assertIn("retweet_json", columns, label)
            self.assertEqual(set(rows), {
                ("elonmusk", "2122601053111677107"),
                ("elonmusk", "2122472372552749426"),
                ("rocketlab", "2122445908747100624"),
                ("rocketlab", "older-rocketlab-only-in-repo"),
            }, label)
            new_elon = rows[("elonmusk", "2122601053111677107")]
            self.assertEqual(new_elon[0], "RT @XFreeze", label)
            quote = sqlite3.connect(dest).execute(
                "SELECT quote_json FROM posts WHERE id=?",
                ("2122601053111677107",),
            ).fetchone()[0]
            self.assertIn("Hitchhiker", quote)
            overlap = rows[("elonmusk", "2122472372552749426")]
            self.assertEqual(overlap[0], "Views from Mechazilla of the first ever Starship catch", label)
            self.assertIn("SpaceX", overlap[1], label)
            self.assertIn("video", overlap[2], label)
            rocket = rows[("rocketlab", "2122445908747100624")]
            self.assertEqual(rocket[4], "rocketlab", label)
            self.assertIn("photo", rocket[2], label)
            self.assertIn("profile_images", rocket[3], label)
            only_repo = rows[("rocketlab", "older-rocketlab-only-in-repo")]
            self.assertEqual(only_repo[0], "older rocket lab post", label)
            self.assertNotIn(("RocketLab", "2122445908747100624"), rows, label)
        self.assertEqual(monitor.read_bytes(), monitor_bytes)
        self.assertEqual(repo.read_bytes(), repo_bytes)

    def test_monitor_database_does_not_drop_eva_rows_from_pages_db(self):
        monitor = self._monitor_db("monitor-no-eva.db")
        pages = self._repo_db("pages-with-eva.db")
        conn = sqlite3.connect(pages)
        conn.execute(
            """INSERT INTO posts (
                account, id, author, author_name, created_at_utc, type_label, text, url, enriched
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                "evavlaar",
                "2122344327586426935",
                "evavlaar",
                "Eva Vlaardingerbroek",
                "2026-11-16T15:01:33.000Z",
                "原文",
                "Berlin Christmas market 2026.",
                "https://x.com/EvaVlaar/status/2122344327586426935",
                1,
            ),
        )
        conn.commit()
        conn.close()
        before = {path.name: path.read_bytes() for path in (monitor, pages)}
        dest = self.root / "union-eva.db"
        publisher.merge_databases(
            monitor,
            dest,
            [pages],
            [],
            {"elonmusk", "rocketlab", "evavlaar"},
        )
        conn = sqlite3.connect(dest)
        eva = conn.execute(
            "SELECT text FROM posts WHERE account=? AND id=?",
            ("evavlaar", "2122344327586426935"),
        ).fetchone()
        elon = conn.execute("SELECT COUNT(*) FROM posts WHERE account='elonmusk'").fetchone()[0]
        rocket = conn.execute("SELECT COUNT(*) FROM posts WHERE account='rocketlab'").fetchone()[0]
        conn.close()
        self.assertIsNotNone(eva)
        self.assertEqual(eva[0], "Berlin Christmas market 2026.")
        self.assertGreaterEqual(elon, 1)
        self.assertGreaterEqual(rocket, 1)
        self.assertEqual(monitor.read_bytes(), before[monitor.name])
        self.assertEqual(pages.read_bytes(), before[pages.name])

    def _monitor_db(self, name: str) -> Path:
        path = self.root / name
        conn = sqlite3.connect(path)
        conn.execute(
            """CREATE TABLE posts (
                id TEXT PRIMARY KEY,
                author TEXT,
                author_name TEXT,
                created_at_utc TEXT,
                created_at_shanghai TEXT,
                type_label TEXT,
                text TEXT,
                quote_json TEXT,
                engagement_json TEXT,
                url TEXT,
                images_json TEXT,
                enriched INTEGER DEFAULT 0,
                updated_at TEXT
            )"""
        )
        conn.execute(
            """INSERT INTO posts (
                id, author, author_name, created_at_utc, created_at_shanghai, type_label,
                text, quote_json, engagement_json, url, images_json, enriched, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "2122601053111677107",
                "elonmusk",
                "Elon Musk",
                "2026-11-17T08:01:41.000Z",
                "2026-11-17 16:01:41",
                "转发",
                "RT @XFreeze",
                json.dumps(
                    {
                        "author": "XFreeze",
                        "name": "X Freeze",
                        "text": "The Hitchhiker's Guide to the Galaxy",
                        "url": "https://x.com/XFreeze/status/2122596768051548482",
                    }
                ),
                json.dumps({"likes": 328, "views": 23044}),
                "https://x.com/elonmusk/status/2122601053111677107",
                json.dumps(["https://pbs.twimg.com/media/HXT6X8JaAAAKVRr.jpg?name=small"]),
                1,
                "2026-11-17 16:08:03",
            ),
        )
        conn.execute(
            """INSERT INTO posts (
                id, author, author_name, created_at_utc, created_at_shanghai, type_label,
                text, quote_json, engagement_json, url, images_json, enriched, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "2122472372552749426",
                "elonmusk",
                "Elon Musk",
                "2026-11-16T23:30:21.000Z",
                "2026-11-17 07:30:21",
                "转发",
                "RT thin",
                None,
                json.dumps({"likes": 1}),
                "https://x.com/elonmusk/status/2122472372552749426",
                json.dumps(["https://pbs.twimg.com/amplify_video_thumb/2122421373616185344/img/x.jpg"]),
                1,
                "2026-11-17 16:08:03",
            ),
        )
        conn.execute(
            """INSERT INTO posts (
                id, author, author_name, created_at_utc, created_at_shanghai, type_label,
                text, quote_json, engagement_json, url, images_json, enriched, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "2122445908747100624",
                "RocketLab",
                "Rocket Lab",
                "2026-11-16T21:45:12.000Z",
                "2026-11-17 05:45:12",
                "原文",
                "Launching rockets, breaking records 🚀",
                None,
                json.dumps({"likes": 1500, "views": 59138}),
                "https://x.com/RocketLab/status/2122445908747100624",
                json.dumps(["https://pbs.twimg.com/media/HXRwU7FbgAIg0H3.jpg?name=small"]),
                1,
                "2026-11-17 15:31:30",
            ),
        )
        conn.commit()
        conn.close()
        return path

    def _repo_db(self, name: str) -> Path:
        path = self.root / name
        conn = sqlite3.connect(path)
        conn.executescript(publisher.PAGES_SCHEMA)
        conn.execute(
            """INSERT INTO posts (
                account, id, author, author_name, author_avatar, author_verified,
                author_verified_type, created_at_utc, created_at_shanghai, type_label,
                text, quote_json, retweet_json, reply_to, engagement_json, url,
                images_json, media_json, enriched, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "elonmusk",
                "2122472372552749426",
                "elonmusk",
                "Elon Musk",
                "https://pbs.twimg.com/profile_images/2103567689515954176/aR-Kjpp4_200x200.jpg",
                1,
                "individual",
                "2026-11-16T23:30:21.000Z",
                "2026-11-17 07:30:21",
                "转发",
                "Views from Mechazilla of the first ever Starship catch",
                None,
                json.dumps({"author": "SpaceX", "text": "Views from Mechazilla of the first ever Starship catch"}),
                "",
                json.dumps({"likes": 16653, "views": 558288}),
                "https://x.com/elonmusk/status/2122472372552749426",
                json.dumps(["https://pbs.twimg.com/amplify_video_thumb/2122421373616185344/img/x.jpg"]),
                json.dumps([{"type": "video", "url": "https://video.twimg.com/amplify_video/1.mp4"}]),
                1,
                "2026-11-17 07:32:52",
            ),
        )
        conn.execute(
            """INSERT INTO posts (
                account, id, author, author_name, author_avatar, author_verified,
                author_verified_type, created_at_utc, created_at_shanghai, type_label,
                text, quote_json, retweet_json, reply_to, engagement_json, url,
                images_json, media_json, enriched, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "rocketlab",
                "2122445908747100624",
                "rocketlab",
                "Rocket Lab",
                "https://pbs.twimg.com/profile_images/1494443717452709900/Y7Lg2mm__200x200.jpg",
                1,
                "organization",
                "2026-11-16T21:45:12.000Z",
                "2026-11-17 05:45:12",
                "原文",
                "Launching rockets, breaking records 🚀",
                None,
                None,
                "",
                json.dumps({"likes": 82, "views": 2721}),
                "https://x.com/rocketlab/status/2122445908747100624",
                json.dumps(["https://pbs.twimg.com/media/HXRwU7FbgAIg0H3.jpg?name=small"]),
                json.dumps([{"type": "photo", "url": "https://pbs.twimg.com/media/HXRwU7FbgAIg0H3.jpg?name=small"}]),
                1,
                "2026-11-17 05:47:09",
            ),
        )
        conn.execute(
            """INSERT INTO posts (
                account, id, author, author_name, created_at_utc, type_label, text,
                url, enriched
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                "rocketlab",
                "older-rocketlab-only-in-repo",
                "rocketlab",
                "Rocket Lab",
                "2026-11-01T00:00:00.000Z",
                "原文",
                "older rocket lab post",
                "https://x.com/rocketlab/status/older-rocketlab-only-in-repo",
                1,
            ),
        )
        conn.commit()
        conn.close()
        return path


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
