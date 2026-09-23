#!/usr/bin/env python3
"""Shared Elon Musk X feed fetch, classify, and enrich (stdlib only).

Retweets from api.fxtwitter.com come back as the *original* tweet with
``reposted_by`` set (the ``retweet`` object is often absent). Classification
treats that as 转发 and always keeps Musk's own status URL
(``https://x.com/elonmusk/status/{platformId}``).
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
UA = "Mozilla/5.0 (compatible; MuskFeedBot/1.0)"
PRIMARY_URL = "https://xtracker.polymarket.com/api/users/elonmusk/posts"
FXT_URL = "https://api.fxtwitter.com/elonmusk/status/{id}"
REQUEST_TIMEOUT = 25
DAYS_BACK = 3
DEFAULT_ENRICH_LIMIT = 20

# Fallback if a row has not been enriched yet. Updated from live payloads when present.
DEFAULT_MUSK_AVATAR = (
    "https://pbs.twimg.com/profile_images/2053244804520427520/m8mdWZCG_200x200.jpg"
)

_MULTI_NL = re.compile(r"\n{3,}")
# fxtwitter appends attached-media links to the text; the media grid already shows them.
_TRAILING_MEDIA = re.compile(r"(?:\s*https?://(?:pbs|video)\.twimg\.com/\S+)+\s*$")


def normalize_text(s: str | None) -> str:
    """Collapse 3+ consecutive newlines to at most 2. Do not invent blank lines."""
    if not s:
        return ""
    t = str(s).replace("\r\n", "\n").replace("\r", "\n")
    t = _TRAILING_MEDIA.sub("", t)
    t = _MULTI_NL.sub("\n\n", t)
    return t.strip()


def http_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.load(resp)


def to_shanghai_iso(utc_iso: str) -> str:
    s = (utc_iso or "").strip()
    if not s:
        return ""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


def now_shanghai() -> str:
    return datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


def musk_status_url(tweet_id: str) -> str:
    return f"https://x.com/elonmusk/status/{tweet_id}"


def _dt_to_pair(dt: datetime) -> tuple[str, str]:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    utc = dt.astimezone(timezone.utc)
    utc_iso = utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    shanghai = utc.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
    return utc_iso, shanghai


def tweet_times(tweet: dict | None) -> tuple[str, str]:
    """Best-effort (utc_iso, shanghai) from an fxtwitter tweet object."""
    if not isinstance(tweet, dict):
        return "", ""
    ts = tweet.get("created_timestamp")
    if isinstance(ts, (int, float)) and ts > 0:
        seconds = float(ts) / 1000.0 if ts > 10_000_000_000 else float(ts)
        try:
            return _dt_to_pair(datetime.fromtimestamp(seconds, timezone.utc))
        except (OverflowError, OSError, ValueError):
            pass
    created = tweet.get("created_at") or tweet.get("createdAt") or ""
    if isinstance(created, str) and created.strip():
        raw = created.strip()
        try:
            return _dt_to_pair(parsedate_to_datetime(raw))
        except (TypeError, ValueError, IndexError):
            pass
        try:
            s = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
            return _dt_to_pair(datetime.fromisoformat(s))
        except ValueError:
            pass
    return "", ""


def fetch_primary() -> list[dict]:
    start = (datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    url = f"{PRIMARY_URL}?platform=X&startDate={start}"
    data = http_json(url)
    if not isinstance(data, dict) or not data.get("success"):
        raise RuntimeError(f"Primary API unexpected response: {type(data)}")
    posts = data.get("data") or []
    return sorted(posts, key=lambda p: p.get("createdAt") or "", reverse=True)


def _screen(author: dict | None) -> str:
    if not isinstance(author, dict):
        return ""
    return str(author.get("screen_name") or "").strip()


def author_profile(author: dict | None, *, default_verified: bool = False) -> dict[str, Any]:
    a = author if isinstance(author, dict) else {}
    ver = a.get("verification") if isinstance(a.get("verification"), dict) else {}
    verified = bool(ver.get("verified") or a.get("verified") or default_verified)
    vtype = str(ver.get("type") or a.get("verified_type") or "")
    return {
        "author": _screen(a),
        "name": str(a.get("name") or ""),
        "avatar": str(a.get("avatar_url") or ""),
        "verified": verified,
        "verified_type": vtype,
    }


def musk_profile_from_tweet(tweet: dict | None) -> dict[str, Any]:
    """Feed owner is always @elonmusk, even when the payload is a reposted original.

    Avatar stays blank when we have no tweet yet, so a later primary upsert does
    not overwrite a real avatar captured during enrich.
    """
    base = {
        "author": "elonmusk",
        "name": "Elon Musk",
        "avatar": "",
        "verified": True,
        "verified_type": "individual",
    }
    if not isinstance(tweet, dict):
        return base
    rb = tweet.get("reposted_by")
    if isinstance(rb, dict) and _screen(rb).lower() == "elonmusk":
        prof = author_profile(rb, default_verified=True)
        prof["author"] = "elonmusk"
        prof["name"] = prof["name"] or "Elon Musk"
        prof["avatar"] = prof["avatar"] or DEFAULT_MUSK_AVATAR
        prof["verified"] = True
        prof["verified_type"] = prof["verified_type"] or "individual"
        return prof
    author = tweet.get("author") if isinstance(tweet.get("author"), dict) else {}
    if _screen(author).lower() == "elonmusk":
        prof = author_profile(author, default_verified=True)
        prof["author"] = "elonmusk"
        prof["name"] = prof["name"] or "Elon Musk"
        prof["avatar"] = prof["avatar"] or DEFAULT_MUSK_AVATAR
        prof["verified"] = True
        prof["verified_type"] = prof["verified_type"] or "individual"
        return prof
    return base


def is_repost(tweet: dict | None, requested_id: str) -> bool:
    """True when this fxtwitter payload is Musk reposting someone else's status."""
    if not isinstance(tweet, dict):
        return False
    if isinstance(tweet.get("retweet"), dict):
        return True
    rb = tweet.get("reposted_by")
    if isinstance(rb, dict) and _screen(rb).lower() == "elonmusk":
        return True
    author = _screen(tweet.get("author") if isinstance(tweet.get("author"), dict) else None).lower()
    tid = str(tweet.get("id") or "")
    if author and author != "elonmusk":
        return True
    if tid and requested_id and tid != str(requested_id) and author != "elonmusk":
        return True
    return False


def classify_type(tweet: dict | None, requested_id: str = "") -> str:
    if not isinstance(tweet, dict):
        return "原文"
    if is_repost(tweet, requested_id):
        return "转发"
    if isinstance(tweet.get("quote"), dict):
        return "引用"
    return "原文"


def _shrink_image_url(url: str) -> str:
    if "pbs.twimg.com/media/" in url or "pbs.twimg.com/tweet_video_thumb/" in url:
        if "name=" in url:
            return (
                url.replace("name=orig", "name=small")
                .replace("name=large", "name=small")
                .replace("name=medium", "name=small")
            )
        return url + ("&name=small" if "?" in url else "?name=small")
    return url


def extract_media_urls(tweet: dict | None) -> list[str]:
    """Remote stills / video thumbs (pbs.twimg.com). Skips quote media."""
    out: list[str] = []
    seen: set[str] = set()
    if not isinstance(tweet, dict):
        return out
    media = tweet.get("media") or {}
    if not isinstance(media, dict):
        return out

    def add(url: str | None) -> None:
        if not url or not isinstance(url, str):
            return
        if not url.startswith("http"):
            return
        u = _shrink_image_url(url)
        if u in seen:
            return
        seen.add(u)
        out.append(u)

    for ph in media.get("photos") or []:
        if isinstance(ph, dict):
            add(ph.get("url"))

    for item in media.get("all") or []:
        if not isinstance(item, dict):
            continue
        mtype = (item.get("type") or "").lower()
        if mtype in ("video", "gif", "animated_gif"):
            add(item.get("thumbnail_url"))
        elif mtype == "photo":
            add(item.get("url"))
    return out


def _reply_to(tweet: dict | None) -> str:
    if not isinstance(tweet, dict):
        return ""
    raw = tweet.get("replying_to")
    if isinstance(raw, str):
        return raw.strip().lstrip("@")
    if isinstance(raw, dict):
        return _screen(raw)
    return ""


def _engagement(tweet: dict | None) -> dict[str, Any]:
    if not isinstance(tweet, dict):
        return {}
    return {
        "replies": tweet.get("replies"),
        "retweets": tweet.get("retweets"),
        "likes": tweet.get("likes"),
        "bookmarks": tweet.get("bookmarks"),
        "quotes": tweet.get("quotes"),
        "views": tweet.get("views"),
    }


def _nested_status(tweet: dict | None) -> dict[str, Any] | None:
    """Author/text/media block for a quoted or reposted status."""
    if not isinstance(tweet, dict):
        return None
    prof = author_profile(tweet.get("author") if isinstance(tweet.get("author"), dict) else None)
    if not (prof["author"] or prof["name"] or tweet.get("text") or tweet.get("url")):
        return None
    utc, sh = tweet_times(tweet)
    return {
        "id": str(tweet.get("id") or ""),
        "author": prof["author"],
        "name": prof["name"],
        "avatar": prof["avatar"],
        "verified": prof["verified"],
        "verified_type": prof["verified_type"],
        "text": normalize_text(tweet.get("text") or ""),
        "url": str(tweet.get("url") or ""),
        "images": extract_media_urls(tweet),
        "created_at_utc": utc,
        "created_at_shanghai": sh,
    }


def _quote_block(tweet: dict | None) -> dict[str, Any] | None:
    if not isinstance(tweet, dict):
        return None
    q = tweet.get("quote")
    if isinstance(q, dict):
        return _nested_status(q)
    return None


def _original_tweet(tweet: dict) -> dict:
    """Status whose text/media/author should render inside a repost card."""
    nested = tweet.get("retweet")
    if isinstance(nested, dict):
        return nested
    return tweet


def build_post(raw: dict, tweet: dict | None, *, remote_images: bool = True) -> dict:
    tid = str(raw.get("platformId") or (tweet or {}).get("id") or "")
    created = str(raw.get("createdAt") or "")
    content = normalize_text(raw.get("content") or "")
    full_text = content
    quote_block = None
    retweet_block = None
    reply_to = ""
    engagement: dict[str, Any] = {}
    images: list[str] = []
    post_type = "原文"
    link = musk_status_url(tid) if tid else ""
    owner = musk_profile_from_tweet(tweet)

    if tweet:
        post_type = classify_type(tweet, tid)
        if not created:
            created, _sh = tweet_times(tweet)
        if is_repost(tweet, tid):
            original = _original_tweet(tweet)
            retweet_block = _nested_status(original)
            # Legacy shape: original carries the quote; fxtwitter puts quote on the same object.
            quote_src = original if isinstance(original, dict) and isinstance(original.get("quote"), dict) else tweet
            quote_block = _quote_block(quote_src)
            if retweet_block:
                full_text = retweet_block["text"] or full_text
                images = list(retweet_block["images"])
            else:
                full_text = normalize_text(original.get("text") or content)
                images = extract_media_urls(original) if remote_images else []
            reply_to = _reply_to(original)
            # Counts shown on a repost are the original status counts.
            engagement = _engagement(original if original.get("likes") is not None or original.get("views") is not None else tweet)
            if retweet_block and quote_block:
                qimgs = set(quote_block.get("images") or [])
                if qimgs:
                    images = [u for u in images if u not in qimgs]
                    retweet_block["images"] = list(images)
        else:
            full_text = normalize_text(tweet.get("text") or content or "")
            quote_block = _quote_block(tweet)
            images = extract_media_urls(tweet) if remote_images else []
            if quote_block:
                qimgs = set(quote_block.get("images") or [])
                if qimgs:
                    images = [u for u in images if u not in qimgs]
            reply_to = _reply_to(tweet)
            engagement = _engagement(tweet)
            owner = musk_profile_from_tweet(tweet)

    created_sh = to_shanghai_iso(created) if created else ""
    return {
        "id": tid,
        "author": owner["author"] or "elonmusk",
        "author_name": owner["name"] or "Elon Musk",
        "author_avatar": owner["avatar"] or DEFAULT_MUSK_AVATAR,
        "author_verified": bool(owner["verified"]),
        "author_verified_type": owner["verified_type"] or "individual",
        "created_at_utc": created,
        "created_at_shanghai": created_sh,
        "type": post_type,
        "type_label": post_type,
        "text": full_text,
        "quote": quote_block,
        "retweet": retweet_block,
        "reply_to": reply_to,
        "engagement": engagement,
        "url": link,
        "images": images,
        "enriched": tweet is not None,
    }


def enrich_one(tweet_id: str) -> tuple[dict | None, str | None]:
    url = FXT_URL.format(id=tweet_id)
    try:
        data = http_json(url)
        if not isinstance(data, dict) or data.get("code") != 200:
            code = data.get("code") if isinstance(data, dict) else data
            return None, f"non-200: {code}"
        tweet = data.get("tweet")
        if not isinstance(tweet, dict):
            return None, "missing tweet"
        return tweet, None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, str(e)


def sync_incremental(
    *,
    enrich_limit: int = DEFAULT_ENRICH_LIMIT,
    sleep_between: float = 0.12,
    verbose: bool = False,
) -> dict:
    """Fetch primary, upsert SQLite, enrich only new/unenriched posts.

    Does not rewrite HTML. Returns inserted/updated/enriched/total.
    """
    import db as dbmod

    dbmod.init_db()
    known = dbmod.get_known_ids()
    raw_posts = fetch_primary()
    if verbose:
        print(f"Primary: {len(raw_posts)} posts; known in DB: {len(known)}")

    primary_posts: list[dict] = []
    new_ids: list[str] = []
    for raw in raw_posts:
        tid = str(raw.get("platformId") or "")
        if not tid:
            continue
        primary_posts.append(build_post(raw, None, remote_images=True))
        if tid not in known:
            new_ids.append(tid)

    upsert_stats = dbmod.upsert_posts(primary_posts)
    unenriched = dbmod.get_unenriched_ids(limit=max(enrich_limit * 3, enrich_limit))
    to_enrich_ids: list[str] = []
    seen: set[str] = set()
    for tid in new_ids + unenriched:
        if tid in seen:
            continue
        seen.add(tid)
        to_enrich_ids.append(tid)
        if len(to_enrich_ids) >= enrich_limit:
            break

    enriched_count = 0
    failures: list[str] = []
    enriched_posts: list[dict] = []
    raw_by_id = {str(r.get("platformId") or ""): r for r in raw_posts}

    if verbose:
        print(f"Enriching {len(to_enrich_ids)} (new={len(new_ids)})")

    for i, tid in enumerate(to_enrich_ids, 1):
        if verbose:
            print(f"[{i}/{len(to_enrich_ids)}] {tid}")
        tweet, err = enrich_one(tid)
        if not tweet:
            failures.append(f"{tid}: {err}")
            if verbose:
                print(f"  skip: {err}")
        else:
            raw = raw_by_id.get(tid) or {"platformId": tid, "createdAt": "", "content": ""}
            enriched_posts.append(build_post(raw, tweet, remote_images=True))
            enriched_count += 1
        if sleep_between:
            time.sleep(sleep_between)

    if enriched_posts:
        enr_stats = dbmod.upsert_posts(enriched_posts)
        upsert_stats["updated"] += enr_stats["updated"]
        upsert_stats["inserted"] += enr_stats["inserted"]

    total = dbmod.count_posts()
    result = {
        "inserted": upsert_stats["inserted"],
        "updated": upsert_stats["updated"],
        "enriched": enriched_count,
        "total": total,
        "failures": failures,
        "updated_at_shanghai": dbmod.latest_updated_at() or now_shanghai(),
    }
    if verbose:
        print(
            f"Sync done: inserted={result['inserted']} updated={result['updated']} "
            f"enriched={result['enriched']} total={result['total']} "
            f"failures={len(failures)}"
        )
    return result
