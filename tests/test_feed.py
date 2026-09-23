#!/usr/bin/env python3
"""Unit tests for classification, SQLite pagination, and static export."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="musk-feed-test-")
os.environ["MUSK_FEED_DB"] = str(Path(_TMP) / "t.db")

import db  # noqa: E402
import export_pages  # noqa: E402
from feed_core import build_post, classify_type, musk_status_url, normalize_text  # noqa: E402


def _author(name, screen, verified=True, vtype="individual"):
    return {
        "name": name,
        "screen_name": screen,
        "avatar_url": f"https://pbs.twimg.com/profile_images/{screen}.jpg",
        "verification": {"verified": verified, "type": vtype},
    }


class NormalizeTests(unittest.TestCase):
    def test_collapses_blank_lines(self):
        self.assertEqual(normalize_text("a\n\n\n\nb"), "a\n\nb")
        self.assertEqual(normalize_text("  a\r\nb  "), "a\nb")
        self.assertEqual(normalize_text(""), "")


class ClassifyTests(unittest.TestCase):
    def test_original(self):
        tid = "100"
        tweet = {
            "id": tid,
            "text": "Hello\n\nworld",
            "url": musk_status_url(tid),
            "author": _author("Elon Musk", "elonmusk"),
            "likes": 10,
            "retweets": 2,
            "replies": 1,
            "bookmarks": 3,
            "views": 100,
            "media": {
                "photos": [{"url": "https://pbs.twimg.com/media/abc?format=jpg&name=orig"}]
            },
        }
        post = build_post({"platformId": tid, "createdAt": "2026-09-22T22:07:53.000Z", "content": "Hello"}, tweet)
        self.assertEqual(post["type_label"], "原文")
        self.assertEqual(post["url"], musk_status_url(tid))
        self.assertEqual(post["text"], "Hello\n\nworld")
        self.assertTrue(post["author_verified"])
        self.assertIn("name=small", post["images"][0])
        self.assertIsNone(post["quote"])
        self.assertIsNone(post["retweet"])
        self.assertEqual(classify_type(tweet, tid), "原文")

    def test_quote_keeps_nested_media_off_outer_post(self):
        tid = "200"
        quote_img = "https://pbs.twimg.com/media/quote?format=jpg&name=large"
        tweet = {
            "id": tid,
            "text": "Interesting.",
            "url": musk_status_url(tid),
            "author": _author("Elon Musk", "elonmusk"),
            "likes": 5,
            "replies": 1,
            "retweets": 1,
            "bookmarks": 0,
            "views": 9,
            "quote": {
                "id": "199",
                "text": "Line one\n\n\nLine two",
                "url": "https://x.com/rauchg/status/199",
                "author": _author("Guillermo Rauch", "rauchg"),
                "media": {"photos": [{"url": quote_img}]},
            },
        }
        post = build_post({"platformId": tid, "createdAt": "2026-09-22T22:07:53.000Z", "content": "Interesting."}, tweet)
        self.assertEqual(post["type_label"], "引用")
        self.assertEqual(post["url"], musk_status_url(tid))
        self.assertEqual(post["images"], [])
        self.assertEqual(post["quote"]["author"], "rauchg")
        self.assertEqual(post["quote"]["text"], "Line one\n\nLine two")
        self.assertEqual(len(post["quote"]["images"]), 1)
        self.assertIn("name=small", post["quote"]["images"][0])
        self.assertTrue(post["quote"]["verified"])

    def test_repost_via_reposted_by(self):
        musk_id = "300"
        tweet = {
            "id": "301",
            "text": "Original body",
            "url": "https://x.com/bot/status/301",
            "author": _author("Grok Bot", "bot", True, "organization"),
            "reposted_by": {
                "name": "Elon Musk",
                "screen_name": "elonmusk",
                "avatar_url": "https://pbs.twimg.com/profile_images/musk.jpg",
            },
            "likes": 20,
            "retweets": 4,
            "replies": 3,
            "bookmarks": 1,
            "views": 500,
            "media": {
                "all": [
                    {
                        "type": "video",
                        "thumbnail_url": "https://pbs.twimg.com/amplify_video_thumb/1/img/x.jpg",
                    }
                ]
            },
            "created_timestamp": 1750000000000,
        }
        post = build_post(
            {"platformId": musk_id, "createdAt": "2026-09-22T18:30:46.000Z", "content": "Original body"},
            tweet,
        )
        self.assertEqual(post["type_label"], "转发")
        self.assertEqual(post["url"], musk_status_url(musk_id))
        self.assertEqual(post["author"], "elonmusk")
        self.assertEqual(post["retweet"]["author"], "bot")
        self.assertEqual(post["retweet"]["url"], "https://x.com/bot/status/301")
        self.assertEqual(post["retweet"]["verified_type"], "organization")
        self.assertTrue(post["retweet"]["images"])
        self.assertIn("amplify_video_thumb", post["images"][0])
        self.assertEqual(post["engagement"]["likes"], 20)
        self.assertTrue(post["retweet"]["created_at_shanghai"])

    def test_legacy_nested_retweet_object(self):
        tid = "400"
        tweet = {
            "id": tid,
            "text": "",
            "url": musk_status_url(tid),
            "author": _author("Elon Musk", "elonmusk"),
            "retweet": {
                "id": "401",
                "text": "Nested",
                "url": "https://x.com/nasa/status/401",
                "author": _author("NASA", "nasa", True, "government"),
                "likes": 8,
                "views": 80,
            },
        }
        post = build_post({"platformId": tid, "createdAt": "2026-09-21T00:00:00.000Z", "content": ""}, tweet)
        self.assertEqual(classify_type(tweet, tid), "转发")
        self.assertEqual(post["url"], musk_status_url(tid))
        self.assertEqual(post["retweet"]["author"], "nasa")
        self.assertEqual(post["text"], "Nested")

    def test_repost_of_quote(self):
        tid = "500"
        tweet = {
            "id": "501",
            "text": "Quoted by them",
            "url": "https://x.com/someone/status/501",
            "author": _author("Someone", "someone"),
            "reposted_by": {"name": "Elon Musk", "screen_name": "elonmusk", "avatar_url": ""},
            "likes": 1,
            "views": 2,
            "quote": {
                "id": "502",
                "text": "Inner",
                "url": "https://x.com/inner/status/502",
                "author": _author("Inner", "inner"),
                "media": {"photos": [{"url": "https://pbs.twimg.com/media/inner?format=jpg&name=orig"}]},
            },
        }
        post = build_post({"platformId": tid, "createdAt": "2026-09-22T01:00:00.000Z", "content": "Quoted by them"}, tweet)
        self.assertEqual(post["type_label"], "转发")
        self.assertEqual(post["quote"]["author"], "inner")
        self.assertEqual(post["images"], [])
        self.assertEqual(post["retweet"]["images"], [])
        self.assertEqual(len(post["quote"]["images"]), 1)


class DbTests(unittest.TestCase):
    def setUp(self):
        if db.DB_PATH.exists():
            db.DB_PATH.unlink()
        for suffix in ("-wal", "-shm"):
            p = Path(str(db.DB_PATH) + suffix)
            if p.exists():
                p.unlink()
        db.FEED_JSON = Path(_TMP) / "no-seed-feed.json"
        db.init_db()

    def _post(self, i, **extra):
        base = {
            "id": str(i),
            "author": "elonmusk",
            "author_name": "Elon Musk",
            "created_at_utc": f"2026-09-22T00:00:{i:02d}.000Z",
            "created_at_shanghai": f"2026-09-22 08:00:{i:02d}",
            "type_label": "原文",
            "text": f"post {i}",
            "url": musk_status_url(str(i)),
            "images": [],
            "engagement": {"likes": i},
            "enriched": True,
        }
        base.update(extra)
        return base

    def test_pagination_shape(self):
        db.upsert_posts([self._post(i) for i in range(45)])
        page = db.get_posts_page(1, 20)
        self.assertEqual(
            set(page.keys()),
            {"posts", "page", "page_size", "total", "total_pages", "updated_at_shanghai"},
        )
        self.assertEqual(page["total"], 45)
        self.assertEqual(page["total_pages"], 3)
        self.assertEqual(len(page["posts"]), 20)
        self.assertEqual(page["posts"][0]["id"], "44")
        page3 = db.get_posts_page(3, 20)
        self.assertEqual(len(page3["posts"]), 5)
        clamped = db.get_posts_page(99, 20)
        self.assertEqual(clamped["page"], 3)
        self.assertEqual(len(clamped["posts"]), 5)

    def test_noop_upsert_does_not_bump_updated_at(self):
        db.upsert_posts([self._post(1)])
        first = db.latest_updated_at()
        again = db.upsert_posts([self._post(1)])
        self.assertEqual(again, {"inserted": 0, "updated": 0})
        self.assertEqual(db.latest_updated_at(), first)

    def test_primary_upsert_does_not_clobber_enrichment(self):
        rich = self._post(
            7,
            type_label="转发",
            text="Original",
            retweet={"author": "bot", "name": "Grok Bot", "text": "Original", "url": "https://x.com/bot/status/9", "images": []},
            images=["https://pbs.twimg.com/media/a.jpg"],
            engagement={"likes": 4, "views": 8},
        )
        db.upsert_posts([rich])
        thin = self._post(7, type_label="原文", text="Original", enriched=False, engagement={}, images=[])
        stats = db.upsert_posts([thin])
        self.assertEqual(stats["updated"], 0)
        row = db.get_posts_page(1, 20)["posts"][0]
        self.assertEqual(row["type_label"], "转发")
        self.assertEqual(row["retweet"]["author"], "bot")
        self.assertEqual(row["images"], ["https://pbs.twimg.com/media/a.jpg"])

    def test_refresh_queue_keeps_quote_until_enrich_returns(self):
        db.upsert_posts(
            [
                self._post(
                    8,
                    type_label="引用",
                    text="Interesting.",
                    quote={"author": "rauchg", "name": "Guillermo", "text": "evals", "url": "https://x.com/rauchg/status/1"},
                    enriched=False,
                )
            ]
        )
        thin = self._post(8, type_label="原文", text="Interesting.", enriched=False, quote=None)
        stats = db.upsert_posts([thin])
        self.assertEqual(stats["updated"], 0)
        row = db.get_posts_page(1, 20)["posts"][0]
        self.assertEqual(row["type_label"], "引用")
        self.assertEqual(row["quote"]["author"], "rauchg")
        self.assertFalse(row["enriched"])


class ExportTests(unittest.TestCase):
    def setUp(self):
        if db.DB_PATH.exists():
            db.DB_PATH.unlink()
        db.FEED_JSON = Path(_TMP) / "no-seed-feed.json"
        db.init_db()

    def test_export_is_stable(self):
        db.upsert_posts(
            [
                {
                    "id": "1",
                    "created_at_utc": "2026-09-22T00:00:01.000Z",
                    "text": "hi",
                    "type_label": "原文",
                    "url": musk_status_url("1"),
                    "enriched": True,
                    "engagement": {"likes": 1},
                }
            ]
        )
        dest = Path(_TMP) / "data"
        first = export_pages.export_pages(page_size=20, dest=dest)
        self.assertTrue(first["changed"])
        payload = json.loads((dest / "page-1.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["page"], 1)
        self.assertEqual(payload["page_size"], 20)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(len(payload["posts"]), 1)
        second = export_pages.export_pages(page_size=20, dest=dest)
        self.assertFalse(second["changed"])


class SpaContractTests(unittest.TestCase):
    def test_renderer_does_not_inject_br(self):
        html = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
        self.assertIn("white-space: pre-wrap", html)
        self.assertNotIn(".replace(/\\n/g", html)
        self.assertNotIn("'<br>'", html)
        self.assertNotIn('"<br>"', html)
        self.assertIn("api/feed?page=", html)
        self.assertIn("data/page-", html)
        self.assertIn("转发了", html)


if __name__ == "__main__":
    unittest.main()
