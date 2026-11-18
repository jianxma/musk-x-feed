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


THUMB_A = "https://pbs.twimg.com/amplify_video_thumb/2122813666642825216/img/0xnS9d7S-MNoi-Gl.jpg"
MP4_A = "https://video.twimg.com/amplify_video/2122813666642825216/vid/avc1/3840x2160/R_ovrsC9lTeNHskU.mp4?tag=29"
HLS_A = "https://video.twimg.com/amplify_video/2122813666642825216/pl/master.m3u8?tag=29"
THUMB_B = "https://pbs.twimg.com/amplify_video_thumb/2122887107714887682/img/dJJ__gMrB_YyO6MJ.jpg"
MP4_B = "https://video.twimg.com/amplify_video/2122887107714887682/vid/avc1/1280x720/supercut.mp4?tag=29"
HLS_B = "https://video.twimg.com/amplify_video/2122887107714887682/pl/supercut.m3u8?tag=29"


def _video_media(thumb: str, playlist: str, low: str, high: str) -> dict:
    """FxTwitter-shaped video, not an already-normalized Pages record."""
    return {
        "type": "video",
        "thumbnail_url": thumb,
        "url": playlist,
        "variants": [
            {"url": playlist, "bitrate": 0, "content_type": "application/x-mpegURL"},
            {"url": low, "bitrate": 832000, "content_type": "video/mp4"},
            {"url": high, "bitrate": 25128000, "content_type": "video/mp4"},
        ],
    }


REPOST_TWEET = {
    "id": "2122814338893332591",
    "text": "The Starbase tower chopsticks catching their first Starship.",
    "url": "https://x.com/SpaceX/status/2122814338893332591",
    "author": {"name": "SpaceX", "screen_name": "SpaceX", "avatar_url": "https://pbs.twimg.com/profile_images/sx.jpg"},
    "reposted_by": {"name": "Elon Musk", "screen_name": "elonmusk", "avatar_url": ""},
    "likes": 10,
    "views": 20,
    "media": {
        "videos": [
            _video_media(
                THUMB_A,
                HLS_A,
                "https://video.twimg.com/amplify_video/2122813666642825216/vid/avc1/1280x720/low.mp4?tag=29",
                MP4_A,
            )
        ]
    },
}

QUOTE_TWEET = {
    "id": "2122928184119161018",
    "text": "Worth watching.",
    "url": "https://x.com/elonmusk/status/2122928184119161018",
    "author": {"name": "Elon Musk", "screen_name": "elonmusk"},
    "likes": 3,
    "views": 4,
    "quote": {
        "id": "2122887107714887682",
        "text": "SpaceX Investors MUST Watch This",
        "url": "https://x.com/stevenmarkryan/status/2122887107714887682",
        "author": {"name": "Steven", "screen_name": "stevenmarkryan"},
        "media": {"videos": [_video_media(THUMB_B, HLS_B, "https://video.twimg.com/amplify_video/2122887107714887682/vid/avc1/480x270/low.mp4?tag=29", MP4_B)]},
    },
}


def _built(post_id: str, tweet: dict, url: str = "") -> dict:
    from feed_core import build_post

    return build_post(
        {"platformId": post_id, "content": "", "createdAt": "", "url": url},
        tweet,
        owner={"handle": "elonmusk"},
    )


class LegacyVideoRepairTests(unittest.TestCase):
    """A 13-column monitor row must not freeze a video poster into Pages."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _legacy(self) -> Path:
        path = self.root / "monitor.db"
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
        rows = [
            (
                "2122897540882542782",
                "elonmusk",
                "Elon Musk",
                "2026-11-18T06:40:00.000Z",
                "2026-11-18 14:40:00",
                "转发",
                "The Starbase tower chopsticks catching their first Starship.",
                json.dumps({"author": "SpaceX", "name": "SpaceX", "text": "", "images": [], "media": None}),
                json.dumps({"likes": 10}),
                "https://x.com/elonmusk/status/2122897540882542782",
                json.dumps([THUMB_A]),
                1,
                "2026-11-18 14:40:00",
            ),
            (
                "2122928184119161018",
                "elonmusk",
                "Elon Musk",
                "2026-11-18T07:10:00.000Z",
                "2026-11-18 15:10:00",
                "引用",
                "Worth watching.",
                json.dumps(
                    {
                        "author": "stevenmarkryan",
                        "name": "Steven",
                        "id": "2122887107714887682",
                        "text": "SpaceX Investors MUST Watch This",
                        "images": [THUMB_B],
                        "media": None,
                    }
                ),
                json.dumps({"likes": 3}),
                "https://x.com/elonmusk/status/2122928184119161018",
                json.dumps([]),
                1,
                "2026-11-18 15:10:00",
            ),
            (
                "photo-only",
                "elonmusk",
                "Elon Musk",
                "2026-11-18T05:00:00.000Z",
                "2026-11-18 13:00:00",
                "原文",
                "A still.",
                None,
                json.dumps({"likes": 1}),
                "https://x.com/elonmusk/status/photo-only",
                json.dumps(["https://pbs.twimg.com/media/still.jpg?name=small"]),
                1,
                "2026-11-18 13:00:00",
            ),
            (
                "already-playable",
                "elonmusk",
                "Elon Musk",
                "2026-11-18T04:00:00.000Z",
                "2026-11-18 12:00:00",
                "转发",
                "Kept.",
                None,
                json.dumps({"likes": 2}),
                "https://x.com/elonmusk/status/already-playable",
                json.dumps([THUMB_A]),
                1,
                "2026-11-18 12:00:00",
            ),
        ]
        conn.executemany(
            """INSERT INTO posts (
                id, author, author_name, created_at_utc, created_at_shanghai, type_label,
                text, quote_json, engagement_json, url, images_json, enriched, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        conn.commit()
        conn.close()
        return path

    def _pages_with_playable(self) -> Path:
        path = self.root / "pages.db"
        conn = sqlite3.connect(path)
        conn.executescript(publisher.PAGES_SCHEMA)
        conn.execute(
            """INSERT INTO posts (
                account, id, author, type_label, text, url, images_json, media_json, enriched
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                "elonmusk",
                "already-playable",
                "elonmusk",
                "转发",
                "Kept.",
                "https://x.com/elonmusk/status/already-playable",
                json.dumps([THUMB_A]),
                json.dumps([{"type": "video", "url": "https://video.twimg.com/amplify_video/kept.mp4", "thumbnail_url": THUMB_A}]),
                1,
            ),
        )
        conn.commit()
        conn.close()
        return path

    def test_legacy_insert_stays_thin_until_offline_repair(self):
        legacy = self._legacy()
        before = legacy.read_bytes()
        pages = self._pages_with_playable()
        dest = self.root / "out.db"
        publisher.merge_databases(pages, dest, [legacy], [], {"elonmusk"})
        self.assertEqual(legacy.read_bytes(), before)
        conn = sqlite3.connect(dest)
        thin = conn.execute(
            "SELECT media_json, retweet_json, quote_json FROM posts WHERE id=?",
            ("2122897540882542782",),
        ).fetchone()
        conn.close()
        self.assertFalse(thin[0])
        self.assertFalse(thin[1])

        calls: list[str] = []

        def fetch(account: str, post_id: str, url: str) -> dict:
            calls.append(post_id)
            if post_id == "2122897540882542782":
                return _built(post_id, REPOST_TWEET, url)
            if post_id == "2122928184119161018":
                return _built(post_id, QUOTE_TWEET, url)
            raise AssertionError(post_id)

        stats = publisher.repair_thin_videos(dest, fetch=fetch, limit=40, accounts={"elonmusk"})
        self.assertEqual(stats["repaired"], 2)
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(set(calls), {"2122897540882542782", "2122928184119161018"})
        self.assertEqual(legacy.read_bytes(), before)

        conn = sqlite3.connect(dest)
        conn.row_factory = sqlite3.Row
        repost = dict(conn.execute("SELECT * FROM posts WHERE id=?", ("2122897540882542782",)).fetchone())
        quoted = dict(conn.execute("SELECT * FROM posts WHERE id=?", ("2122928184119161018",)).fetchone())
        kept = json.loads(conn.execute("SELECT media_json FROM posts WHERE id=?", ("already-playable",)).fetchone()[0])
        photo = json.loads(conn.execute("SELECT images_json FROM posts WHERE id=?", ("photo-only",)).fetchone()[0])
        conn.close()

        media = json.loads(repost["media_json"])
        self.assertEqual(media[0]["url"], MP4_A)
        self.assertEqual(media[0]["hls_url"], HLS_A)
        self.assertNotIn("m3u8", media[0]["url"])
        retweet = json.loads(repost["retweet_json"])
        self.assertEqual(retweet["author"], "SpaceX")
        self.assertEqual(retweet["media"][0]["url"], MP4_A)
        self.assertIsNone(repost["quote_json"])
        self.assertEqual(repost["text"], "The Starbase tower chopsticks catching their first Starship.")

        quote = json.loads(quoted["quote_json"])
        self.assertEqual(quote["media"][0]["url"], MP4_B)
        self.assertEqual(quote["author"], "stevenmarkryan")
        self.assertEqual(json.loads(quoted["media_json"]), [])
        self.assertEqual(kept[0]["url"], "https://video.twimg.com/amplify_video/kept.mp4")
        self.assertEqual(photo, ["https://pbs.twimg.com/media/still.jpg?name=small"])

        def downgrade(*_args):
            raise AssertionError("playable row must not be fetched again")

        again = publisher.repair_thin_videos(dest, fetch=downgrade, limit=40, accounts={"elonmusk"})
        self.assertEqual(again["candidates"], 0)
        self.assertEqual(again["repaired"], 0)

    def test_unreachable_source_keeps_the_poster_and_continues(self):
        legacy = self._legacy()
        dest = self.root / "out.db"
        publisher.merge_databases(self._pages_with_playable(), dest, [legacy], [], {"elonmusk"})

        def fetch(account: str, post_id: str, url: str) -> dict:
            if post_id == "2122897540882542782":
                raise OSError("upstream timed out")
            if post_id == "2122928184119161018":
                return {
                    "type_label": "引用",
                    "images": [THUMB_B],
                    "media": [],
                    "quote": {
                        "author": "stevenmarkryan",
                        "text": "SpaceX Investors MUST Watch This",
                        "images": [THUMB_B],
                        "media": [{"type": "video", "url": "", "thumbnail_url": THUMB_B}],
                    },
                    "retweet": None,
                }
            raise AssertionError(post_id)

        stats = publisher.repair_thin_videos(
            dest,
            fetch=fetch,
            limit=1,
            accounts={"elonmusk"},
        )
        # Newest thin row is the quote. Limit 1 must not fetch the older repost,
        # and a payload with only a poster must not be stored as a video.
        self.assertEqual(stats["candidates"], 1)
        self.assertEqual(stats["repaired"], 0)
        self.assertEqual(stats["no_video"], 1)
        self.assertEqual(stats["failed"], 0)
        conn = sqlite3.connect(dest)
        media = conn.execute(
            "SELECT media_json FROM posts WHERE id=?",
            ("2122928184119161018",),
        ).fetchone()[0]
        conn.close()
        self.assertFalse(media)

        failed = publisher.repair_thin_videos(
            dest,
            fetch=fetch,
            ids=["2122897540882542782"],
            accounts={"elonmusk"},
        )
        self.assertEqual(failed["failed"], 1)
        self.assertEqual(failed["repaired"], 0)
        conn = sqlite3.connect(dest)
        still = conn.execute(
            "SELECT images_json, media_json FROM posts WHERE id=?",
            ("2122897540882542782",),
        ).fetchone()
        conn.close()
        self.assertIn("amplify_video_thumb", still[0])
        self.assertFalse(still[1])

    def test_monitor_schema_is_not_rewritten(self):
        legacy = self._legacy()
        before = legacy.read_bytes()
        with self.assertRaises(RuntimeError):
            publisher.repair_thin_videos(legacy, fetch=lambda *_a: {}, limit=5)
        self.assertEqual(legacy.read_bytes(), before)


class RepairImportPathTests(unittest.TestCase):
    """The ops copy of publish_local_db.py sits beside the legacy modules.

    Python puts that directory on sys.path[0], ahead of PYTHONPATH. The
    production failure was ``build_post() got an unexpected keyword argument
    'owner'`` from that old feed_core.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.legacy = self.root / "musk-feed"
        self.wt = self.root / "wt"
        self.legacy.mkdir()
        self.wt.mkdir()
        (self.legacy / "feed_core.py").write_text(
            "def enrich_one(tweet_id, handle='elonmusk'):\n"
            "    return {'id': tweet_id, 'legacy': True}, None\n"
            "def build_post(raw, tweet, author='', author_name=''):\n"
            "    raise TypeError(\"build_post() got an unexpected keyword argument 'owner'\")\n",
            encoding="utf-8",
        )
        (self.legacy / "db.py").write_text(
            "SENTINEL = 'legacy-db'\n",
            encoding="utf-8",
        )
        (self.wt / "feed_core.py").write_text(
            "def enrich_one(tweet_id, handle='elonmusk'):\n"
            "    return {'id': tweet_id, 'screen': handle}, None\n"
            "def build_post(raw, tweet, owner=None):\n"
            "    handle = (owner or {}).get('handle') or 'elonmusk'\n"
            "    thumb = 'https://pbs.twimg.com/amplify_video_thumb/2122813666642825216/img/x.jpg'\n"
            "    media = [{'type': 'video', 'url': 'https://video.twimg.com/amplify_video/from-worktree.mp4', 'thumbnail_url': thumb}]\n"
            "    return {\n"
            "        'id': raw.get('platformId'),\n"
            "        'type_label': '转发',\n"
            "        'text': 'from-worktree',\n"
            "        'images': [thumb],\n"
            "        'media': media,\n"
            "        'quote': None,\n"
            "        'retweet': {'author': 'SpaceX', 'text': 'from-worktree', 'images': [thumb], 'media': media},\n"
            "        'loaded_from': __file__,\n"
            "        'handle': handle,\n"
            "    }\n",
            encoding="utf-8",
        )
        (self.wt / "db.py").write_text(
            "import feed_core\n"
            "LOADED_FEED = feed_core.__file__\n"
            "def _preserve_playable_media(old, new):\n"
            "    return list(new or [])\n"
            "def _preserve_nested_media(old, new):\n"
            "    return new\n",
            encoding="utf-8",
        )
        import shutil

        shutil.copy(ROOT / "scripts" / "publish_local_db.py", self.legacy / "publish_local_db.py")
        self.db = self.root / "pages.db"
        conn = sqlite3.connect(self.db)
        conn.executescript(publisher.PAGES_SCHEMA)
        conn.execute(
            """INSERT INTO posts (
                account, id, author, type_label, text, url, images_json, quote_json, enriched
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                "elonmusk",
                "2122897540882542782",
                "elonmusk",
                "转发",
                "The Starbase tower chopsticks catching their first Starship.",
                "https://x.com/elonmusk/status/2122897540882542782",
                json.dumps(["https://pbs.twimg.com/amplify_video_thumb/2122813666642825216/img/0xnS9d7S-MNoi-Gl.jpg"]),
                json.dumps({"author": "SpaceX", "text": "", "images": []}),
                1,
            ),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *extra: str, worktree: str | None = "") -> subprocess.CompletedProcess[str]:
        cmd = [
            sys.executable,
            str(self.legacy / "publish_local_db.py"),
            "repair-videos",
            "--db",
            str(self.db),
            "--id",
            "2122897540882542782",
        ]
        if worktree is not None:
            cmd.extend(["--worktree", worktree if worktree != "" else str(self.wt)])
        cmd.extend(extra)
        env = os.environ.copy()
        # Production set PYTHONPATH to the good tree. That still loses to sys.path[0].
        env["PYTHONPATH"] = str(self.wt)
        env["MUSK_FEED_DB"] = str(self.legacy / "must-not-open.db")
        return subprocess.run(cmd, env=env, text=True, capture_output=True)

    def test_cli_uses_worktree_modules_not_the_legacy_siblings(self):
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + "\n" + proc.stderr)
        self.assertNotIn("unexpected keyword argument 'owner'", proc.stdout + proc.stderr)
        self.assertIn(f"VIDEO_REPAIR_MODULES feed_core={self.wt / 'feed_core.py'}", proc.stdout)
        self.assertIn(f"db={self.wt / 'db.py'}", proc.stdout)
        self.assertNotIn(str(self.legacy / "feed_core.py"), proc.stdout)
        conn = sqlite3.connect(self.db)
        row = conn.execute(
            "SELECT media_json, retweet_json, quote_json FROM posts WHERE id=?",
            ("2122897540882542782",),
        ).fetchone()
        conn.close()
        media = json.loads(row[0])
        retweet = json.loads(row[1])
        self.assertEqual(media[0]["url"], "https://video.twimg.com/amplify_video/from-worktree.mp4")
        self.assertEqual(retweet["author"], "SpaceX")
        self.assertIsNone(row[2])
        self.assertFalse((self.legacy / "must-not-open.db").exists())

    def test_missing_worktree_fails_without_calling_legacy_build_post(self):
        proc = self._run(worktree=None)
        self.assertNotEqual(proc.returncode, 0)
        combined = proc.stdout + proc.stderr
        self.assertIn("worktree", combined.lower())
        self.assertNotIn("unexpected keyword argument 'owner'", combined)
        conn = sqlite3.connect(self.db)
        media = conn.execute(
            "SELECT media_json FROM posts WHERE id=?",
            ("2122897540882542782",),
        ).fetchone()[0]
        conn.close()
        self.assertFalse(media)

    def test_legacy_directory_as_worktree_is_refused(self):
        proc = self._run(worktree=str(self.legacy))
        self.assertEqual(proc.returncode, 1, proc.stdout + "\n" + proc.stderr)
        self.assertIn("no owner parameter", proc.stderr)
        conn = sqlite3.connect(self.db)
        media = conn.execute(
            "SELECT media_json FROM posts WHERE id=?",
            ("2122897540882542782",),
        ).fetchone()[0]
        conn.close()
        self.assertFalse(media)


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
        self.assertIn("repair-videos", code)
        self.assertIn('--worktree "$WT"', text)
        self.assertNotIn('PYTHONPATH="$WT', code)
        self.assertIn("keeping stored rows and continuing export", code)
        self.assertIn('git -C "$WT" add -- musk_feed.db docs/data', text)
        self.assertNotIn("docs/index.html", code)
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


class LiveWorktreeRepairTests(unittest.TestCase):
    """Real subprocess, legacy siblings, and FxTwitter. Not a mocked build_post."""

    IDS = (
        "2122897540882542782",
        "2122928184119161018",
        "2122895004045861065",
    )

    def test_cli_fetches_real_mp4_without_loading_legacy_feed_core(self):
        import shutil

        tmp = tempfile.TemporaryDirectory()
        try:
            root = Path(tmp.name)
            legacy = root / "musk-feed"
            legacy.mkdir()
            (legacy / "feed_core.py").write_text(
                "def enrich_one(tweet_id, handle='elonmusk'):\n"
                "    return {'id': tweet_id}, None\n"
                "def build_post(raw, tweet, author='', author_name=''):\n"
                "    raise TypeError(\"build_post() got an unexpected keyword argument 'owner'\")\n",
                encoding="utf-8",
            )
            (legacy / "db.py").write_text("SENTINEL = 'legacy'\n", encoding="utf-8")
            shutil.copy(ROOT / "scripts" / "publish_local_db.py", legacy / "publish_local_db.py")
            db_path = root / "pages.db"
            conn = sqlite3.connect(db_path)
            conn.executescript(publisher.PAGES_SCHEMA)
            for post_id in self.IDS:
                conn.execute(
                    """INSERT INTO posts (
                        account, id, author, type_label, text, url, images_json, quote_json, enriched,
                        created_at_utc
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "elonmusk",
                        post_id,
                        "elonmusk",
                        "转发",
                        "poster only",
                        f"https://x.com/elonmusk/status/{post_id}",
                        json.dumps(["https://pbs.twimg.com/amplify_video_thumb/1/img/x.jpg"]),
                        json.dumps({"author": "someone", "text": "", "images": []}),
                        1,
                        "2026-11-18T06:00:00.000Z",
                    ),
                )
            conn.commit()
            conn.close()
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT)
            env["MUSK_FEED_DB"] = str(legacy / "monitor.db")
            cmd = [
                sys.executable,
                str(legacy / "publish_local_db.py"),
                "repair-videos",
                "--db",
                str(db_path),
                "--worktree",
                str(ROOT),
            ]
            for post_id in self.IDS:
                cmd.extend(["--id", post_id])
            proc = subprocess.run(
                cmd,
                env=env,
                text=True,
                capture_output=True,
            )
            print(proc.stdout)
            print(proc.stderr, file=sys.stderr)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertNotIn("unexpected keyword argument 'owner'", proc.stdout + proc.stderr)
            self.assertIn(f"feed_core={(ROOT / 'feed_core.py').resolve()}", proc.stdout)
            self.assertIn(f"db={(ROOT / 'db.py').resolve()}", proc.stdout)
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            for post_id in self.IDS:
                row = conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
                media = json.loads(row["media_json"] or "[]")
                retweet = json.loads(row["retweet_json"]) if row["retweet_json"] else None
                quote = json.loads(row["quote_json"]) if row["quote_json"] else None
                urls = [item.get("url") for item in media if isinstance(item, dict)]
                if isinstance(retweet, dict):
                    urls += [item.get("url") for item in (retweet.get("media") or []) if isinstance(item, dict)]
                if isinstance(quote, dict):
                    urls += [item.get("url") for item in (quote.get("media") or []) if isinstance(item, dict)]
                playable = [url for url in urls if isinstance(url, str) and ".mp4" in url.split("?", 1)[0]]
                self.assertTrue(playable, f"{post_id} urls={urls}")
                where = "post" if any(
                    isinstance(item, dict) and item.get("url") in playable for item in media
                ) else "nested"
                print(
                    f"LIVE {post_id} where={where} "
                    f"retweet={(retweet or {}).get('author') if isinstance(retweet, dict) else None} "
                    f"quote={(quote or {}).get('author') if isinstance(quote, dict) else None} "
                    f"mp4={playable[0]}"
                )
            spacex = conn.execute(
                "SELECT retweet_json, media_json FROM posts WHERE id=?",
                ("2122897540882542782",),
            ).fetchone()
            retweet = json.loads(spacex["retweet_json"])
            self.assertEqual(retweet["author"], "SpaceX")
            self.assertIn(".mp4", retweet["media"][0]["url"])
            conn.close()
            self.assertFalse((legacy / "monitor.db").exists())
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
