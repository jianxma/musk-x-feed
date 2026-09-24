#!/usr/bin/env python3
"""Shared X feed fetch, classify, and enrich (stdlib only).

@elonmusk still comes from xtracker (list) plus api.fxtwitter.com (per-status
enrich). Other watched accounts, starting with @rocketlab, use the FxTwitter
v2 profile timeline (``/2/profile/{handle}/statuses``). xtracker returns 404
for Rocket Lab.

Retweets from api.fxtwitter.com come back as the *original* tweet with
``reposted_by`` set (the ``retweet`` object is often absent). Classification
treats that as 转发. For @elonmusk the card links to Musk's own status
(``https://x.com/elonmusk/status/{platformId}`` from xtracker). A v2 timeline
repost does not include the reposter's status id, so that card links to the
watched profile instead.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
UA = "Mozilla/5.0 (compatible; MuskFeedBot/1.0)"
PRIMARY_URL = "https://xtracker.polymarket.com/api/users/elonmusk/posts"
FXT_STATUS_URL = "https://api.fxtwitter.com/{handle}/status/{id}"
FXT_TIMELINE_URL = "https://api.fxtwitter.com/2/profile/{handle}/statuses"
REQUEST_TIMEOUT = 25
DAYS_BACK = 3
DEFAULT_ENRICH_LIMIT = 20
TIMELINE_PAGE_SIZE = 20
TIMELINE_MAX_PAGES = 8

# Fallback if a row has not been enriched yet. Updated from live payloads when present.
DEFAULT_MUSK_AVATAR = (
    "https://pbs.twimg.com/profile_images/2053244804520427520/m8mdWZCG_200x200.jpg"
)
# From api.fxtwitter.com/RocketLab (avatar_url), upgraded _normal -> _200x200.
DEFAULT_ROCKETLAB_AVATAR = (
    "https://pbs.twimg.com/profile_images/1494443717452709900/Y7Lg2mm__200x200.jpg"
)

# source=xtracker keeps the Musk list+enrich path. source=fxtwitter reads the
# v2 profile timeline in one shot (those payloads are already enriched).
SYNCED_ACCOUNTS: list[dict[str, Any]] = [
    {
        "handle": "elonmusk",
        "name": "Elon Musk",
        "avatar": DEFAULT_MUSK_AVATAR,
        "verified": True,
        "verified_type": "individual",
        "source": "xtracker",
    },
    {
        "handle": "rocketlab",
        "name": "Rocket Lab",
        "avatar": DEFAULT_ROCKETLAB_AVATAR,
        "verified": True,
        "verified_type": "organization",
        "source": "fxtwitter",
    },
]


def synced_handles() -> set[str]:
    return {str(item["handle"]) for item in SYNCED_ACCOUNTS}

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
    return status_url("elonmusk", tweet_id)


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


def _normalize_owner(owner: dict | None) -> dict[str, Any]:
    """Watched-account defaults. Missing owner is @elonmusk, matching older callers."""
    base = {
        "handle": "elonmusk",
        "name": "Elon Musk",
        "avatar": DEFAULT_MUSK_AVATAR,
        "verified": True,
        "verified_type": "individual",
    }
    if not isinstance(owner, dict):
        return base
    handle = str(owner.get("handle") or "").strip().lstrip("@").lower()
    if handle:
        base["handle"] = handle
    name = str(owner.get("name") or "").strip()
    if name:
        base["name"] = name
    avatar = str(owner.get("avatar") or "").strip()
    if avatar:
        base["avatar"] = avatar
    if "verified" in owner:
        base["verified"] = bool(owner["verified"])
    vtype = str(owner.get("verified_type") or "").strip()
    if vtype:
        base["verified_type"] = vtype
    return base


def status_url(handle: str, tweet_id: str) -> str:
    return f"https://x.com/{handle}/status/{tweet_id}"


def owner_profile_from_tweet(tweet: dict | None, owner: dict | None = None) -> dict[str, Any]:
    """Feed owner stays the watched account, even when the payload is a repost.

    Avatar stays blank when we have no tweet yet, so a later primary upsert does
    not overwrite a real avatar captured during enrich. ``build_post`` fills the
    account default only on the outgoing row.
    """
    acct = _normalize_owner(owner)
    handle = acct["handle"]
    base = {
        "author": handle,
        "name": acct["name"],
        "avatar": "",
        "verified": bool(acct["verified"]),
        "verified_type": acct["verified_type"],
    }
    if not isinstance(tweet, dict):
        return base

    def _take(person: dict) -> dict[str, Any]:
        prof = author_profile(person, default_verified=bool(acct["verified"]))
        prof["author"] = handle
        prof["name"] = prof["name"] or acct["name"]
        prof["avatar"] = prof["avatar"] or acct["avatar"]
        if acct["verified"]:
            prof["verified"] = True
        prof["verified_type"] = prof["verified_type"] or acct["verified_type"]
        return prof

    rb = tweet.get("reposted_by")
    if isinstance(rb, dict) and _screen(rb).lower() == handle:
        return _take(rb)
    author = tweet.get("author") if isinstance(tweet.get("author"), dict) else {}
    if _screen(author).lower() == handle:
        return _take(author)
    return base


def musk_profile_from_tweet(tweet: dict | None) -> dict[str, Any]:
    return owner_profile_from_tweet(tweet, None)


def is_repost(tweet: dict | None, requested_id: str, owner: str = "elonmusk") -> bool:
    """True when this payload is the watched account reposting someone else."""
    handle = (owner or "elonmusk").strip().lstrip("@").lower() or "elonmusk"
    if not isinstance(tweet, dict):
        return False
    if isinstance(tweet.get("retweet"), dict):
        return True
    rb = tweet.get("reposted_by")
    if isinstance(rb, dict) and _screen(rb).lower() == handle:
        return True
    author = _screen(tweet.get("author") if isinstance(tweet.get("author"), dict) else None).lower()
    tid = str(tweet.get("id") or "")
    if author and author != handle:
        return True
    if tid and requested_id and tid != str(requested_id) and author != handle:
        return True
    return False


def classify_type(tweet: dict | None, requested_id: str = "", owner: str = "elonmusk") -> str:
    if not isinstance(tweet, dict):
        return "原文"
    if is_repost(tweet, requested_id, owner):
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


def _as_http(url: Any) -> str:
    if isinstance(url, str) and url.startswith("http"):
        return url
    return ""


def _path_ext(url: str) -> str:
    path = url.split("?", 1)[0].lower()
    if "." not in path:
        return ""
    return path.rsplit(".", 1)[-1]


def _bitrate(variant: dict) -> int:
    try:
        return int(variant.get("bitrate") or 0)
    except (TypeError, ValueError):
        return 0


def _is_playable_video(url: str, content_type: str = "", container: str = "") -> bool:
    ct = (content_type or "").lower()
    box = (container or "").lower()
    ext = _path_ext(url)
    if "mpegurl" in ct or box in {"m3u8", "hls"} or ext == "m3u8":
        return False
    if "mp4" in ct or "webm" in ct or box in {"mp4", "webm"} or ext in {"mp4", "m4v", "webm"}:
        return True
    return False


def _is_audio_url(url: str, content_type: str = "", container: str = "") -> bool:
    ct = (content_type or "").lower()
    box = (container or "").lower()
    ext = _path_ext(url)
    if ct.startswith("audio/") or box in {"mp3", "m4a", "aac"}:
        return True
    return ext in {"mp3", "m4a", "aac", "wav", "ogg"}


def _canonical_type(raw: str) -> str:
    t = (raw or "").strip().lower()
    if t in {"gif", "animated_gif"}:
        return "gif"
    if t == "video":
        return "video"
    if t in {"audio", "voice"}:
        return "audio"
    if t in {"photo", "image"}:
        return "photo"
    return ""


def _best_stream(item: dict, kind: str) -> str:
    """Highest-bitrate direct file. Skips HLS playlists, which <video> cannot play everywhere."""
    ranked: list[tuple[int, str]] = []
    for key in ("variants", "formats"):
        pool = item.get(key)
        if not isinstance(pool, list):
            continue
        for variant in pool:
            if not isinstance(variant, dict):
                continue
            url = _as_http(variant.get("url"))
            if not url:
                continue
            ct = str(variant.get("content_type") or variant.get("format") or "")
            container = str(variant.get("container") or "")
            ok = _is_audio_url(url, ct, container) if kind == "audio" else _is_playable_video(url, ct, container)
            if ok:
                ranked.append((_bitrate(variant), url))
    if ranked:
        ranked.sort(key=lambda pair: pair[0])
        return ranked[-1][1]
    url = _as_http(item.get("url"))
    fmt = str(item.get("format") or "")
    if not url:
        return ""
    if kind == "audio" and _is_audio_url(url, fmt):
        return url
    if kind != "audio" and _is_playable_video(url, fmt):
        return url
    return ""


def _num(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _media_record(item: dict, default_type: str = "") -> dict | None:
    mtype = _canonical_type(str(item.get("type") or "")) or _canonical_type(default_type)
    if not mtype:
        return None
    thumb = _as_http(item.get("thumbnail_url"))
    if mtype == "photo":
        url = _shrink_image_url(_as_http(item.get("url")))
        if not url:
            return None
        rec: dict[str, Any] = {"type": "photo", "url": url}
    else:
        url = _best_stream(item, "audio" if mtype == "audio" else "video")
        if not url and not thumb:
            return None
        rec = {"type": mtype, "url": url}
        if thumb:
            rec["thumbnail_url"] = thumb
    for key in ("width", "height", "duration"):
        num = _num(item.get(key))
        if num is not None:
            rec[key] = num
    return rec


def _iter_media_dicts(media: dict) -> list[tuple[dict, str]]:
    all_items = media.get("all")
    if isinstance(all_items, list) and any(isinstance(item, dict) for item in all_items):
        return [(item, "") for item in all_items if isinstance(item, dict)]
    out: list[tuple[dict, str]] = []
    for key, default in (("photos", "photo"), ("videos", "video"), ("gifs", "gif"), ("audio", "audio")):
        bucket = media.get(key)
        if not isinstance(bucket, list):
            continue
        for item in bucket:
            if isinstance(item, dict):
                out.append((item, default))
    return out


def extract_media(tweet: dict | None) -> list[dict]:
    """Typed attachments on this status only. Does not walk nested quote/retweet.

    Each item is ``{type, url, thumbnail_url?, width?, height?, duration?}``
    with type photo, video, gif, or audio. Video/gif ``url`` is a playable file
    (highest-bitrate mp4/webm), not the thumbnail.
    """
    if not isinstance(tweet, dict):
        return []
    media = tweet.get("media") or {}
    if not isinstance(media, dict):
        return []
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item, default in _iter_media_dicts(media):
        rec = _media_record(item, default)
        if not rec:
            continue
        key = (rec["type"], rec.get("url") or rec.get("thumbnail_url") or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(rec)
    return out


def images_from_media(items: list[dict]) -> list[str]:
    """Photo URLs and video/gif thumbnails, for older clients that only read images."""
    out: list[str] = []
    seen: set[str] = set()
    for rec in items:
        if rec.get("type") == "photo":
            url = rec.get("url") or ""
        elif rec.get("type") in ("video", "gif"):
            url = rec.get("thumbnail_url") or ""
        else:
            url = ""
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def extract_media_urls(tweet: dict | None) -> list[str]:
    """Remote stills / video thumbs. Skips nested quote media."""
    return images_from_media(extract_media(tweet))


def _media_ban(block: dict | None) -> set[str]:
    ban: set[str] = set()
    if not isinstance(block, dict):
        return ban
    for url in block.get("images") or []:
        if url:
            ban.add(str(url))
    for rec in block.get("media") or []:
        if not isinstance(rec, dict):
            continue
        for key in ("url", "thumbnail_url"):
            url = rec.get(key) or ""
            if url:
                ban.add(str(url))
                ban.add(_shrink_image_url(str(url)))
    return ban


def _strip_quoted_media(
    media: list[dict], images: list[str], quote_block: dict | None
) -> tuple[list[dict], list[str]]:
    ban = _media_ban(quote_block)
    if not ban:
        return media, images
    media = [
        rec
        for rec in media
        if rec.get("url") not in ban and (rec.get("thumbnail_url") or "") not in ban
    ]
    images = [url for url in images if url not in ban]
    return media, images


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
    retweets = tweet.get("retweets")
    if retweets is None:
        # FxTwitter v2 timelines name this count ``reposts``.
        retweets = tweet.get("reposts")
    return {
        "replies": tweet.get("replies"),
        "retweets": retweets,
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
    media = extract_media(tweet)
    return {
        "id": str(tweet.get("id") or ""),
        "author": prof["author"],
        "name": prof["name"],
        "avatar": prof["avatar"],
        "verified": prof["verified"],
        "verified_type": prof["verified_type"],
        "text": normalize_text(tweet.get("text") or ""),
        "url": str(tweet.get("url") or ""),
        "images": images_from_media(media),
        "media": media,
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


def build_post(
    raw: dict,
    tweet: dict | None,
    *,
    remote_images: bool = True,
    owner: dict | None = None,
) -> dict:
    acct = _normalize_owner(owner)
    handle = acct["handle"]
    tid = str(raw.get("platformId") or (tweet or {}).get("id") or "")
    created = str(raw.get("createdAt") or "")
    content = normalize_text(raw.get("content") or "")
    full_text = content
    quote_block = None
    retweet_block = None
    reply_to = ""
    engagement: dict[str, Any] = {}
    images: list[str] = []
    media: list[dict] = []
    post_type = "原文"
    explicit_url = str(raw.get("url") or "").strip()
    link = explicit_url or (status_url(handle, tid) if tid else "")
    profile = owner_profile_from_tweet(tweet, acct)

    if tweet:
        post_type = classify_type(tweet, tid, handle)
        if not created:
            created, _sh = tweet_times(tweet)
        if is_repost(tweet, tid, handle):
            original = _original_tweet(tweet)
            retweet_block = _nested_status(original)
            # Legacy shape: original carries the quote; fxtwitter puts quote on the same object.
            quote_src = original if isinstance(original, dict) and isinstance(original.get("quote"), dict) else tweet
            quote_block = _quote_block(quote_src)
            if retweet_block:
                full_text = retweet_block["text"] or full_text
                images = list(retweet_block["images"])
                media = list(retweet_block.get("media") or [])
            else:
                full_text = normalize_text(original.get("text") or content)
                media = extract_media(original) if remote_images else []
                images = images_from_media(media)
            reply_to = _reply_to(original)
            # Counts shown on a repost are the original status counts.
            engagement = _engagement(original if original.get("likes") is not None or original.get("views") is not None else tweet)
            if retweet_block and quote_block:
                media, images = _strip_quoted_media(media, images, quote_block)
                retweet_block["images"] = list(images)
                retweet_block["media"] = list(media)
        else:
            full_text = normalize_text(tweet.get("text") or content or "")
            quote_block = _quote_block(tweet)
            media = extract_media(tweet) if remote_images else []
            images = images_from_media(media)
            if quote_block:
                media, images = _strip_quoted_media(media, images, quote_block)
            reply_to = _reply_to(tweet)
            engagement = _engagement(tweet)
            profile = owner_profile_from_tweet(tweet, acct)

    created_sh = to_shanghai_iso(created) if created else ""
    return {
        "id": tid,
        "account": handle,
        "author": profile["author"] or handle,
        "author_name": profile["name"] or acct["name"],
        "author_avatar": profile["avatar"] or acct["avatar"],
        "author_verified": bool(profile["verified"]),
        "author_verified_type": profile["verified_type"] or acct["verified_type"],
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
        "media": media,
        "enriched": tweet is not None,
    }


def enrich_one(tweet_id: str, handle: str = "elonmusk") -> tuple[dict | None, str | None]:
    url = FXT_STATUS_URL.format(handle=handle, id=tweet_id)
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


def _flatten_timeline_results(results: list) -> list[dict]:
    out: list[dict] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "thread" and isinstance(item.get("statuses"), list):
            for status in item["statuses"]:
                if isinstance(status, dict) and status.get("id"):
                    out.append(status)
            continue
        if item.get("type") in (None, "", "status") and item.get("id"):
            out.append(item)
    return out


def _status_datetime(status: dict) -> datetime | None:
    utc, _sh = tweet_times(status)
    if not utc:
        return None
    raw = utc[:-1] + "+00:00" if utc.endswith("Z") else utc
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def fetch_fxtwitter_statuses(
    handle: str,
    *,
    days_back: int = DAYS_BACK,
    min_posts: int = TIMELINE_PAGE_SIZE,
    max_pages: int = TIMELINE_MAX_PAGES,
    sleep_between: float = 0.1,
) -> list[dict]:
    """Recent statuses from FxTwitter v2 ``/2/profile/{handle}/statuses``.

    Pages until the window is older than ``days_back`` and at least ``min_posts``
    are in hand (one page, so a quiet account still fills a site page).
    """
    handle = (handle or "").strip().lstrip("@")
    if not handle:
        raise RuntimeError("missing fxtwitter handle")
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    collected: list[dict] = []
    cursor = ""
    for page in range(1, max_pages + 1):
        qs = urllib.parse.urlencode({"count": str(TIMELINE_PAGE_SIZE)})
        url = FXT_TIMELINE_URL.format(handle=urllib.parse.quote(handle)) + "?" + qs
        if cursor:
            url += "&cursor=" + urllib.parse.quote(cursor, safe="")
        data = http_json(url)
        if not isinstance(data, dict) or data.get("code") != 200:
            code = data.get("code") if isinstance(data, dict) else type(data)
            raise RuntimeError(f"fxtwitter timeline unexpected response: {code}")
        batch = _flatten_timeline_results(data.get("results") or [])
        collected.extend(batch)
        oldest = _status_datetime(batch[-1]) if batch else None
        cursor = str((data.get("cursor") or {}).get("bottom") or "")
        if len(collected) >= min_posts and oldest is not None and oldest < cutoff:
            break
        if not cursor or not batch:
            break
        if page < max_pages and sleep_between:
            time.sleep(sleep_between)
    return collected


def posts_from_fxt_statuses(statuses: list[dict], owner: dict) -> list[dict]:
    """Map a v2 timeline page onto the same post shape ``build_post`` writes for Elon."""
    acct = _normalize_owner(owner)
    handle = acct["handle"]
    posts: list[dict] = []
    for status in statuses:
        tid = str(status.get("id") or "")
        if not tid:
            continue
        utc, _sh = tweet_times(status)
        raw: dict[str, Any] = {
            "platformId": tid,
            "createdAt": utc,
            "content": status.get("text") or "",
        }
        if is_repost(status, tid, handle):
            # v2 identifies a repost by the original status id, not the
            # watched account's own status id. Linking x.com/{handle}/status/{id}
            # would 404. The header falls back to the profile; the nested card
            # keeps the original status URL.
            raw["url"] = f"https://x.com/{handle}"
        posts.append(build_post(raw, status, owner=acct))
    return posts


def _sync_xtracker(dbmod: Any, *, enrich_limit: int, sleep_between: float, verbose: bool) -> dict:
    """Existing @elonmusk path: xtracker list, then fxtwitter status enrich."""
    handle = "elonmusk"
    known = dbmod.get_known_ids(account=handle)
    raw_posts = fetch_primary()
    if verbose:
        print(f"Primary @{handle}: {len(raw_posts)} posts; known in DB: {len(known)}")

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
    unenriched = dbmod.get_unenriched_ids(limit=max(enrich_limit * 3, enrich_limit), account=handle)
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
        print(f"Enriching @{handle} {len(to_enrich_ids)} (new={len(new_ids)})")

    for i, tid in enumerate(to_enrich_ids, 1):
        if verbose:
            print(f"[{i}/{len(to_enrich_ids)}] {tid}")
        tweet, err = enrich_one(tid, handle)
        if not tweet:
            failures.append(f"{handle}:{tid}: {err}")
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

    return {
        "handle": handle,
        "inserted": upsert_stats["inserted"],
        "updated": upsert_stats["updated"],
        "enriched": enriched_count,
        "failures": failures,
    }


def _sync_fxtwitter(dbmod: Any, spec: dict, *, sleep_between: float, verbose: bool) -> dict:
    """Timeline sync for accounts xtracker does not list.

    The v2 payload already includes text, media, quote, and repost fields, so
    new rows are stored enriched. Known rows are left in place (same as Elon,
    where a later thin list upsert does not refresh engagement).
    """
    handle = str(spec["handle"])
    statuses = fetch_fxtwitter_statuses(handle, sleep_between=sleep_between)
    built = posts_from_fxt_statuses(statuses, spec)
    known = dbmod.get_known_ids(account=handle)
    fresh = [post for post in built if post.get("id") and post["id"] not in known]
    if verbose:
        print(f"Timeline @{handle}: {len(built)} posts; new={len(fresh)}; known={len(known)}")
    stats = dbmod.upsert_posts(fresh) if fresh else {"inserted": 0, "updated": 0}
    return {
        "handle": handle,
        "inserted": stats["inserted"],
        "updated": stats["updated"],
        "enriched": stats["inserted"] + stats["updated"],
        "failures": [],
    }


def _empty_part(handle: str, failures: list[str]) -> dict:
    return {
        "handle": handle,
        "inserted": 0,
        "updated": 0,
        "enriched": 0,
        "failures": failures,
    }


def sync_incremental(
    *,
    enrich_limit: int = DEFAULT_ENRICH_LIMIT,
    sleep_between: float = 0.12,
    verbose: bool = False,
    handles: list[str] | None = None,
) -> dict:
    """Fetch watched accounts into SQLite. Does not rewrite HTML.

    ``handles`` limits the run (lowercase). The default is every synced account.
    An @elonmusk primary failure still aborts, matching the previous job.
    A later account's fetch error is recorded and does not discard Elon's sync.
    """
    import db as dbmod

    dbmod.init_db()
    wanted = None
    if handles is not None:
        wanted = {str(h).strip().lstrip("@").lower() for h in handles if str(h).strip()}
    inserted = 0
    updated = 0
    enriched = 0
    failures: list[str] = []
    per_account: list[dict] = []
    for spec in SYNCED_ACCOUNTS:
        handle = str(spec["handle"])
        if wanted is not None and handle not in wanted:
            continue
        try:
            if spec.get("source") == "xtracker":
                part = _sync_xtracker(
                    dbmod,
                    enrich_limit=enrich_limit,
                    sleep_between=sleep_between,
                    verbose=verbose,
                )
            elif spec.get("source") == "fxtwitter":
                part = _sync_fxtwitter(dbmod, spec, sleep_between=sleep_between, verbose=verbose)
            else:
                part = _empty_part(handle, [f"{handle}: unknown source"])
        except Exception as e:
            if spec.get("source") == "xtracker":
                raise
            msg = f"{handle}: {e}"
            failures.append(msg)
            per_account.append(_empty_part(handle, [msg]))
            if verbose:
                print(f"Sync failed for @{handle}: {e}")
            continue
        inserted += int(part["inserted"])
        updated += int(part["updated"])
        enriched += int(part["enriched"])
        failures.extend(part.get("failures") or [])
        per_account.append(part)

    total = dbmod.count_posts()
    result = {
        "inserted": inserted,
        "updated": updated,
        "enriched": enriched,
        "total": total,
        "failures": failures,
        "updated_at_shanghai": dbmod.latest_updated_at() or now_shanghai(),
        "accounts": per_account,
    }
    if verbose:
        print(
            f"Sync done: inserted={result['inserted']} updated={result['updated']} "
            f"enriched={result['enriched']} total={result['total']} "
            f"failures={len(failures)}"
        )
    return result
