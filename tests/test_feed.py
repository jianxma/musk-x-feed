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

import accounts  # noqa: E402
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
        self.assertEqual(normalize_text("Grok 4.7 https://pbs.twimg.com/media/abc.jpg"), "Grok 4.7")


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
        self.assertEqual(post["media"][0]["type"], "photo")
        self.assertIn("name=small", post["media"][0]["url"])

    def test_video_gif_audio_keep_playable_urls(self):
        tid = "150"
        video = "https://video.twimg.com/amplify_video/1/vid/high.mp4?tag=29"
        gif = "https://video.twimg.com/tweet_video/clip.mp4"
        audio = "https://video.twimg.com/tweet_audio/note.m4a"
        photo = "https://pbs.twimg.com/media/still?format=jpg&name=orig"
        tweet = {
            "id": tid,
            "text": "mixed",
            "url": musk_status_url(tid),
            "author": _author("Elon Musk", "elonmusk"),
            "media": {
                "all": [
                    {
                        "type": "video",
                        "thumbnail_url": "https://pbs.twimg.com/amplify_video_thumb/1/img/x.jpg",
                        "width": 720,
                        "height": 1280,
                        "duration": 12.5,
                        "url": "https://video.twimg.com/amplify_video/1/pl/master.m3u8",
                        "variants": [
                            {
                                "url": "https://video.twimg.com/amplify_video/1/pl/master.m3u8",
                                "bitrate": 0,
                                "content_type": "application/x-mpegURL",
                            },
                            {
                                "url": "https://video.twimg.com/amplify_video/1/vid/low.mp4",
                                "bitrate": 632000,
                                "content_type": "video/mp4",
                            },
                            {
                                "url": video,
                                "bitrate": 2176000,
                                "content_type": "video/mp4",
                            },
                        ],
                    },
                    {
                        "type": "animated_gif",
                        "thumbnail_url": "https://pbs.twimg.com/tweet_video_thumb/clip.jpg",
                        "url": gif,
                        "duration": 1.2,
                    },
                    {"type": "photo", "url": photo, "width": 400, "height": 300},
                ],
                "audio": [{"type": "audio", "url": audio, "duration": 4}],
            },
        }
        post = build_post({"platformId": tid, "createdAt": "2026-09-22T00:00:00.000Z", "content": "mixed"}, tweet)
        kinds = [item["type"] for item in post["media"]]
        self.assertEqual(kinds, ["video", "gif", "photo"])
        self.assertEqual(post["media"][0]["url"], video)
        self.assertNotIn("m3u8", post["media"][0]["url"])
        self.assertEqual(post["media"][0]["thumbnail_url"].rsplit("/", 1)[-1], "x.jpg")
        self.assertEqual(post["media"][0]["duration"], 12.5)
        self.assertEqual(post["media"][1]["url"], gif)
        self.assertEqual(post["media"][1]["type"], "gif")
        self.assertIn("name=small", post["media"][2]["url"])
        self.assertEqual(post["images"][0], post["media"][0]["thumbnail_url"])
        self.assertNotIn(video, post["images"])
        audio_only = build_post(
            {"platformId": "151", "createdAt": "2026-09-22T00:00:01.000Z", "content": "voice"},
            {
                "id": "151",
                "text": "voice",
                "author": _author("Elon Musk", "elonmusk"),
                "media": {"audio": [{"url": audio, "duration": 4}]},
            },
        )
        self.assertEqual(audio_only["media"], [{"type": "audio", "url": audio, "duration": 4}])
        self.assertEqual(audio_only["images"], [])

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
        self.assertEqual(post["media"][0]["type"], "video")
        self.assertEqual(post["media"][0]["url"], "")
        self.assertIn("amplify_video_thumb", post["media"][0]["thumbnail_url"])
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
        self.assertEqual(post["retweet"]["media"], [])
        self.assertEqual(len(post["quote"]["images"]), 1)
        self.assertEqual(post["quote"]["media"][0]["type"], "photo")

    def test_rocketlab_timeline_maps_repost_and_photo(self):
        from feed_core import posts_from_fxt_statuses

        owner = {
            "handle": "rocketlab",
            "name": "Rocket Lab",
            "avatar": "https://pbs.twimg.com/profile_images/rl.jpg",
            "verified": True,
            "verified_type": "organization",
        }
        posts = posts_from_fxt_statuses(
            [
                {
                    "id": "301",
                    "type": "status",
                    "text": "For your joy.",
                    "created_timestamp": 1789800085,
                    "author": _author("Peter Beck", "Peter_J_Beck"),
                    "reposted_by": {
                        "name": "Rocket Lab",
                        "screen_name": "RocketLab",
                        "avatar_url": "https://pbs.twimg.com/rl.jpg",
                    },
                    "reposts": 9,
                    "likes": 5,
                    "views": 6,
                    "url": "https://x.com/Peter_J_Beck/status/301",
                },
                {
                    "id": "400",
                    "type": "status",
                    "text": "Launch",
                    "created_at": "Wed Sep 23 02:09:06 +0000 2026",
                    "author": _author("Rocket Lab", "RocketLab", True, "organization"),
                    "likes": 1,
                    "reposts": 2,
                    "url": "https://x.com/RocketLab/status/400",
                    "media": {
                        "photos": [
                            {
                                "type": "photo",
                                "url": "https://pbs.twimg.com/media/abc.jpg?name=orig",
                            }
                        ]
                    },
                },
            ],
            owner,
        )
        self.assertEqual(posts[0]["type_label"], "转发")
        self.assertEqual(posts[0]["account"], "rocketlab")
        self.assertEqual(posts[0]["author"], "rocketlab")
        self.assertEqual(posts[0]["url"], "https://x.com/rocketlab")
        self.assertEqual(posts[0]["retweet"]["author"], "Peter_J_Beck")
        self.assertEqual(posts[0]["engagement"]["retweets"], 9)
        self.assertEqual(posts[1]["type_label"], "原文")
        self.assertEqual(posts[1]["url"], "https://x.com/rocketlab/status/400")
        self.assertEqual(posts[1]["author_verified_type"], "organization")
        self.assertIn("name=small", posts[1]["images"][0])


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
            media=[{
                "type": "video",
                "url": "https://video.twimg.com/amplify_video/1/vid/a.mp4",
                "thumbnail_url": "https://pbs.twimg.com/amplify_video_thumb/1/img/x.jpg",
            }],
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
        self.assertEqual(row["media"][0]["url"], "https://video.twimg.com/amplify_video/1/vid/a.mp4")

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

    def test_same_status_id_stays_on_its_account(self):
        db.upsert_posts([self._post(1, text="elon")])
        db.upsert_posts(
            [
                self._post(
                    1,
                    account="rocketlab",
                    author="rocketlab",
                    author_name="Rocket Lab",
                    text="rl",
                    url="https://x.com/rocketlab/status/1",
                )
            ]
        )
        elon = db.get_all_posts(account="elonmusk")
        rl = db.get_all_posts(account="rocketlab")
        self.assertEqual([p["text"] for p in elon], ["elon"])
        self.assertEqual([p["text"] for p in rl], ["rl"])
        self.assertEqual(db.get_posts_page(1, 20, account="elonmusk")["total"], 1)
        self.assertEqual(db.count_posts(), 2)

    def test_failed_account_does_not_skip_the_other(self):
        import feed_core

        calls = []

        def boom():
            calls.append("elon")
            raise RuntimeError("xtracker down")

        def timeline(handle, **kwargs):
            calls.append(handle)
            return [
                {
                    "id": "900",
                    "type": "status",
                    "text": "Launch",
                    "created_timestamp": 1790129346,
                    "author": _author("Rocket Lab", "RocketLab", True, "organization"),
                    "likes": 1,
                    "reposts": 2,
                }
            ]

        orig = (
            feed_core.fetch_primary,
            feed_core.fetch_fxtwitter_statuses,
            feed_core.enrich_one,
        )
        feed_core.fetch_primary = boom
        feed_core.fetch_fxtwitter_statuses = timeline
        feed_core.enrich_one = lambda *args, **kwargs: (None, "offline")
        try:
            result = feed_core.sync_incremental(enrich_limit=1, sleep_between=0, verbose=False)
        finally:
            (
                feed_core.fetch_primary,
                feed_core.fetch_fxtwitter_statuses,
                feed_core.enrich_one,
            ) = orig
        self.assertEqual([p["handle"] for p in result["accounts"]], ["elonmusk", "rocketlab"])
        self.assertEqual(calls, ["elon", "rocketlab"])
        elon = next(p for p in result["accounts"] if p["handle"] == "elonmusk")
        rocket = next(p for p in result["accounts"] if p["handle"] == "rocketlab")
        self.assertTrue(elon["fetch_error"])
        self.assertFalse(rocket.get("fetch_error"))
        self.assertEqual(db.get_all_posts(account="rocketlab")[0]["text"], "Launch")
        self.assertEqual(db.get_all_posts(account="elonmusk"), [])

        def primary():
            return [
                {
                    "platformId": "42",
                    "createdAt": "2026-09-22T00:00:00.000Z",
                    "content": "from elon",
                }
            ]

        def timeline_down(handle, **kwargs):
            raise RuntimeError("fxtwitter down")

        feed_core.fetch_primary = primary
        feed_core.fetch_fxtwitter_statuses = timeline_down
        feed_core.enrich_one = lambda *args, **kwargs: (None, "offline")
        try:
            again = feed_core.sync_incremental(enrich_limit=1, sleep_between=0, verbose=False)
        finally:
            (
                feed_core.fetch_primary,
                feed_core.fetch_fxtwitter_statuses,
                feed_core.enrich_one,
            ) = orig
        self.assertEqual(db.get_posts_page(1, 20, account="elonmusk")["posts"][0]["text"], "from elon")
        self.assertTrue(any(p.get("fetch_error") and p["handle"] == "rocketlab" for p in again["accounts"]))
        self.assertEqual(db.get_all_posts(account="rocketlab")[0]["text"], "Launch")


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
        self.assertTrue((dest / "page-1.json").exists())
        self.assertFalse((dest / "elonmusk").exists())

    def test_export_all_namespaces_accounts_and_keeps_unsourced_files(self):
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
        root = Path(_TMP) / "namespaced"
        root.mkdir()
        (root / "page-1.json").write_text("{}\n", encoding="utf-8")
        (root / "manifest.json").write_text("{}\n", encoding="utf-8")
        manual = root / "other"
        manual.mkdir()
        (manual / "manifest.json").write_text('{"total": 1}\n', encoding="utf-8")
        (manual / "page-1.json").write_text('{"posts": [{"id": "keep"}]}\n', encoding="utf-8")
        (root / "accounts.json").write_text(
            json.dumps(
                {
                    "accounts": [
                        {"handle": "@ElonMusk", "display_name": "Elon Musk", "avatar": ""},
                        {"handle": "other", "name": "Other", "avatar": "not-a-url"},
                        {"handle": "../etc/passwd", "name": "nope"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        first = export_pages.export_all(page_size=20, data_root=root)
        self.assertTrue(first["changed"])
        payload = json.loads((root / "elonmusk" / "page-1.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["account"], "elonmusk")
        self.assertEqual(payload["total"], 1)
        manifest = json.loads((root / "elonmusk" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["account"], "elonmusk")
        self.assertFalse((root / "page-1.json").exists())
        self.assertFalse((root / "manifest.json").exists())
        self.assertEqual(
            json.loads((manual / "page-1.json").read_text(encoding="utf-8"))["posts"][0]["id"],
            "keep",
        )
        self.assertFalse((root / "etc").exists())
        saved = accounts.load_accounts(root / "accounts.json")
        self.assertEqual([a["handle"] for a in saved], ["elonmusk", "other"])
        self.assertEqual(saved[1]["avatar"], "")
        second = export_pages.export_all(page_size=20, data_root=root)
        self.assertFalse(second["changed"])
        self.assertTrue(any(item.get("handle") == "other" and item.get("skipped") for item in second["accounts"]))


class AccountRegistryTests(unittest.TestCase):
    def test_normalize_and_seed(self):
        self.assertEqual(accounts.normalize_handle("@ElonMusk"), "elonmusk")
        self.assertEqual(accounts.normalize_handle("../etc"), "")
        self.assertEqual(accounts.normalize_handle("elon musk"), "")
        self.assertEqual(accounts.live_handles(), {"elonmusk", "rocketlab"})
        missing = Path(_TMP) / "no-such-accounts.json"
        seeded = accounts.load_accounts(missing)
        self.assertEqual([a["handle"] for a in seeded], ["elonmusk"])
        self.assertFalse(missing.exists())


class SpaContractTests(unittest.TestCase):
    def test_renderer_does_not_inject_br(self):
        html = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
        self.assertIn("white-space: pre-wrap", html)
        self.assertNotIn(".replace(/\\n/g", html)
        self.assertNotIn("'<br>'", html)
        self.assertNotIn('"<br>"', html)
        self.assertIn("api/feed?account=", html)
        self.assertIn("data/accounts.json", html)
        self.assertIn("data/manifest.json", html)
        self.assertIn("'page-'", html)
        self.assertIn("转发了", html)

    def test_header_is_freshness_line_and_compact_tabs(self):
        html = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn(">马斯克</h1>", html)
        self.assertNotIn("<h1", html)
        self.assertNotIn("height: 53px", html)
        self.assertIn('class="title-row"', html)
        self.assertIn('class="account-row"', html)
        self.assertIn('id="accountTabs"', html)
        self.assertIn('class="account-label"', html)
        self.assertGreaterEqual(html.count('class="account-label"'), 3)
        self.assertIn("更多", html)
        self.assertIn("PINNED_TABS = 4", html)
        self.assertIn("accounts.slice(0, PINNED_TABS)", html)
        self.assertIn("musk-x-feed-account", html)
        self.assertIn('id="statusText"', html)
        self.assertIn("backdrop-filter: blur(12px)", html)
        self.assertIn('data-handle="elonmusk"', html)
        self.assertIn('data-handle="rocketlab"', html)
        self.assertIn("@elonmusk", html)
        self.assertIn("@rocketlab", html)
        self.assertIn("overflow-x: auto", html)
        self.assertIn(".account-tab .account-handle", html)
        self.assertIn("font-size: 10px;", html)

    def test_long_post_text_collapses_with_toggle(self):
        html = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
        self.assertIn("-webkit-line-clamp: 6", html)
        self.assertIn('class="text is-clamped"', html)
        self.assertIn('class="q-text is-clamped"', html)
        self.assertIn("展开", html)
        self.assertIn("收起", html)
        self.assertIn("syncTextClamps", html)
        self.assertNotIn("media.is-clamped", html)

    def test_renderer_plays_typed_media_inline(self):
        html = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
        self.assertIn('controls playsinline preload="metadata"', html)
        self.assertIn("<audio controls preload=\"metadata\"", html)
        self.assertIn("p.media", html)
        self.assertIn("isPlayableVideoUrl", html)


if __name__ == "__main__":
    unittest.main()
