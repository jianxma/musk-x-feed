#!/usr/bin/env python3
"""Build a static GitHub Pages site for Elon Musk (@elonmusk) X posts.

Fetches the last 3 days from xtracker and enriches each post via fxtwitter.
Writes docs/index.html + docs/feed.json and downloads stills/thumbs to docs/media/.
Stdlib urllib only. Idempotent (skips media files that already exist).

fxtwitter usually resolves a repost to the original status (different id, original
author, `reposted_by` set) and fills `text`, so there is often no `retweet` object.
Quote and repost media live on those nested objects and must be read from there.
The card's primary link is always Musk's own status id from xtracker.
"""

from __future__ import annotations

import html
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
MEDIA_DIR = DOCS / "media"
FEED_JSON = DOCS / "feed.json"
INDEX_HTML = DOCS / "index.html"

SHANGHAI = ZoneInfo("Asia/Shanghai")
UA = "Mozilla/5.0 (compatible; MuskPagesBot/1.0; +https://github.com/)"
PRIMARY_URL = "https://xtracker.polymarket.com/api/users/elonmusk/posts"
FXT_URL = "https://api.fxtwitter.com/elonmusk/status/{id}"
MAX_IMAGE_BYTES = 2_500_000
REQUEST_TIMEOUT = 25
DAYS_BACK = 3
# Polite gap between enrich calls. The cron is every 10 minutes, so the whole
# run needs to finish well inside that window on the happy path.
ENRICH_SLEEP = 0.2
ENRICH_ATTEMPTS = 3
# Stop enriching and still write the feed if the API is slow or rate-limiting.
ENRICH_BUDGET_S = 8 * 60
ENGAGEMENT_KEYS = ("likes", "retweets", "replies", "bookmarks", "quotes", "views")


def http_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.load(resp)


def http_bytes(url: str) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = resp.read(MAX_IMAGE_BYTES + 1)
            if len(data) > MAX_IMAGE_BYTES:
                print(f"  skip oversized image: {url[:80]}...")
                return None
            return data
    except Exception as e:
        print(f"  image download fail: {e}")
        return None


def to_shanghai_iso(utc_iso: str) -> str:
    s = utc_iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


def fetch_primary() -> list[dict]:
    start = (datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    url = f"{PRIMARY_URL}?platform=X&startDate={start}"
    print(f"Fetching primary: {url}")
    data = http_json(url)
    if not isinstance(data, dict) or not data.get("success"):
        raise RuntimeError(f"Primary API unexpected response: {type(data)}")
    posts = data.get("data") or []
    posts = sorted(posts, key=lambda p: p.get("createdAt") or "", reverse=True)
    print(f"Primary returned {len(posts)} posts")
    return posts


def musk_status_url(status_id: str) -> str:
    return f"https://x.com/elonmusk/status/{status_id}"


def author_of(tweet: dict) -> dict:
    author = tweet.get("author")
    return author if isinstance(author, dict) else {}


def is_retweet(tweet: dict, musk_id: str) -> bool:
    """True when this fxtwitter payload is Musk reposting someone else.

    Current fxtwitter resolves the repost: `id`/`url`/`text` belong to the
    original author and `reposted_by` names @elonmusk. Older payloads nest the
    original under `retweet` and may still fill `text`, so that key alone is enough.
    """
    if isinstance(tweet.get("retweet"), dict):
        return True
    returned = str(tweet.get("id") or "")
    author = (author_of(tweet).get("screen_name") or "").lower()
    if returned == str(musk_id) and author in ("", "elonmusk"):
        return False
    reposted_by = tweet.get("reposted_by")
    if isinstance(reposted_by, dict) and (reposted_by.get("screen_name") or "").lower() == "elonmusk":
        return True
    if returned and returned != str(musk_id) and author not in ("", "elonmusk"):
        return True
    return False


def classify_type(tweet: dict | None, musk_id: str = "") -> str:
    if not tweet:
        return "原文"
    if is_retweet(tweet, musk_id):
        return "转发"
    if isinstance(tweet.get("quote"), dict):
        return "引用"
    return "原文"


def _small_photo_url(url: str) -> str:
    if "name=" in url:
        for size in ("orig", "large", "medium"):
            url = url.replace(f"name={size}", "name=small")
        return url
    return url + ("&name=small" if "?" in url else "?name=small")


def extract_media_items(tweet: dict) -> list[dict]:
    """Return {id, url, kind} stills/thumbs from one tweet object.

    Does not walk nested quote/retweet; callers pass those objects themselves.
    """
    if not isinstance(tweet, dict):
        return []
    media = tweet.get("media") or {}
    if not isinstance(media, dict):
        return []

    out: list[dict] = []
    seen: set[str] = set()

    def add(mid: str, url: str, kind: str) -> None:
        if not url:
            return
        key = mid or url
        if key in seen:
            return
        seen.add(key)
        out.append({"id": mid or "m", "url": url, "kind": kind})

    for ph in media.get("photos") or []:
        if isinstance(ph, dict) and ph.get("url"):
            add(str(ph.get("id") or ""), _small_photo_url(str(ph["url"])), "photo")

    pools: list[Any] = []
    for key in ("all", "videos"):
        pool = media.get(key) or []
        if isinstance(pool, list):
            pools.extend(pool)

    for item in pools:
        if not isinstance(item, dict):
            continue
        mtype = (item.get("type") or "").lower()
        mid = str(item.get("id") or "")
        if mtype in ("video", "gif", "animated_gif"):
            thumb = item.get("thumbnail_url") or ""
            add(mid or "thumb", str(thumb), "thumb")
        elif mtype == "photo" or item.get("url"):
            url = item.get("url") or ""
            if url and mtype in ("", "photo"):
                add(mid, _small_photo_url(str(url)), "photo")
    return out


def download_media(tweet_id: str, items: list[dict]) -> list[dict]:
    """Download images; return list of {src, remote, local, url}.

    src is media/... when the file is saved or already present; otherwise src
    is the remote URL so the page can still show it.
    """
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    for i, item in enumerate(items):
        url = item["url"]
        mid = item.get("id") or str(i)
        ext = ".jpg"
        low = url.lower()
        if ".png" in low:
            ext = ".png"
        elif ".webp" in low:
            ext = ".webp"
        elif ".gif" in low:
            ext = ".gif"
        fname = f"{tweet_id}_{mid}{ext}"
        fname = "".join(c for c in fname if c.isalnum() or c in "._-")
        dest = MEDIA_DIR / fname
        rel = f"media/{fname}"
        if dest.exists() and dest.stat().st_size > 0:
            results.append({"src": rel, "remote": False, "local": True, "url": url})
            continue
        data = http_bytes(url)
        if data:
            dest.write_bytes(data)
            results.append({"src": rel, "remote": False, "local": True, "url": url})
            print(f"  saved {rel} ({len(data)} bytes)")
        else:
            results.append({"src": url, "remote": True, "local": False, "url": url})
            print(f"  keep remote URL for {tweet_id}_{mid}")
    return results


def engagement_of(tweet: dict | None) -> dict[str, Any]:
    if not isinstance(tweet, dict):
        return {}
    return {k: tweet.get(k) for k in ENGAGEMENT_KEYS}


def status_url(screen_name: str, status_id: str, fallback: str = "") -> str:
    if fallback:
        return fallback
    if screen_name and status_id:
        return f"https://x.com/{screen_name}/status/{status_id}"
    return ""


def merge_image_meta(block: dict, extra: list[dict]) -> None:
    seen = set(block.get("images") or [])
    meta = block.setdefault("image_meta", [])
    images = block.setdefault("images", [])
    for item in extra:
        src = item.get("src") or ""
        if not src or src in seen:
            continue
        seen.add(src)
        meta.append(item)
        images.append(src)


def nested_block(obj: dict, file_id: str, depth: int = 0) -> dict:
    """Author, full text, media, and link for a quoted or reposted status."""
    author = author_of(obj)
    handle = (author.get("screen_name") or "").strip()
    name = (author.get("name") or "").strip()
    text = (obj.get("text") or "").strip()
    sid = str(obj.get("id") or "")
    url = status_url(handle, sid, (obj.get("url") or "").strip())
    items = extract_media_items(obj)
    image_meta = download_media(file_id, items) if items else []
    inner = None
    quoted = obj.get("quote")
    if depth < 1 and isinstance(quoted, dict):
        inner = nested_block(quoted, file_id, depth + 1)
    return {
        "author": handle,
        "name": name,
        "text": text,
        "url": url,
        "images": [m["src"] for m in image_meta],
        "image_meta": image_meta,
        "quote": inner,
    }


def retry_after_seconds(err: urllib.error.HTTPError) -> float:
    raw = err.headers.get("Retry-After") if err.headers else None
    try:
        return float(raw) if raw is not None else 2.0
    except (TypeError, ValueError):
        return 2.0


def enrich_one(tweet_id: str) -> tuple[dict | None, str | None]:
    url = FXT_URL.format(id=tweet_id)
    last_err = "unknown"
    for attempt in range(ENRICH_ATTEMPTS):
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
            last_err = f"HTTP {e.code}"
            retriable = e.code == 429 or e.code >= 500
            if retriable and attempt < ENRICH_ATTEMPTS - 1:
                wait = retry_after_seconds(e) if e.code == 429 else 1.5 * (attempt + 1)
                wait = min(max(wait, 1.0), 20.0)
                print(f"  {last_err}, retry in {wait:.1f}s")
                time.sleep(wait)
                continue
            return None, last_err
        except urllib.error.URLError as e:
            last_err = str(e.reason if getattr(e, "reason", None) else e)
            if attempt < ENRICH_ATTEMPTS - 1:
                time.sleep(1.0)
                continue
            return None, last_err
        except Exception as e:
            last_err = str(e)
            if attempt < ENRICH_ATTEMPTS - 1:
                time.sleep(1.0)
                continue
            return None, last_err
    return None, last_err


def build_post(raw: dict, tweet: dict | None) -> dict:
    tid = str(raw.get("platformId") or "")
    created = raw.get("createdAt") or ""
    content = (raw.get("content") or "").strip()
    # Primary link is always Musk's status, never the quoted/reposted author.
    link = musk_status_url(tid)
    full_text = content
    quote_block = None
    engagement: dict[str, Any] = {}
    image_meta: list[dict] = []
    post_type = "原文"

    if tweet:
        post_type = classify_type(tweet, tid)
        if post_type == "转发":
            source = tweet["retweet"] if isinstance(tweet.get("retweet"), dict) else tweet
            quote_block = nested_block(source, tid)
            if isinstance(tweet.get("retweet"), dict):
                # Wrapper can carry media the nested object does not.
                extra_items = extract_media_items(tweet)
                if extra_items:
                    merge_image_meta(quote_block, download_media(tid, extra_items))
            handle = quote_block.get("author") or ""
            full_text = f"转发 @{handle}" if handle else "转发"
            engagement = engagement_of(source)
        else:
            full_text = (tweet.get("text") or content or "").strip()
            engagement = engagement_of(tweet)
            items = extract_media_items(tweet)
            if items:
                image_meta = download_media(tid, items)
            quoted = tweet.get("quote")
            if isinstance(quoted, dict):
                quote_block = nested_block(quoted, tid)

    return {
        "id": tid,
        "author": "elonmusk",
        "author_name": "Elon Musk",
        "created_at_utc": created,
        "created_at_shanghai": to_shanghai_iso(created) if created else "",
        "type": post_type,
        "type_label": post_type,
        "text": full_text,
        "quote": quote_block,
        "engagement": engagement,
        "url": link,
        "images": [img["src"] for img in image_meta],
        "image_meta": image_meta,
        "enriched": tweet is not None,
    }


def fmt_num(n: Any) -> str:
    if n is None:
        return "—"
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def render_media(meta: list) -> str:
    if not meta:
        return ""
    parts: list[str] = []
    for item in meta:
        if isinstance(item, str):
            src, remote = item, False
        elif isinstance(item, dict):
            src = item.get("src") or ""
            remote = bool(item.get("remote"))
        else:
            continue
        if not src:
            continue
        src_e = html.escape(src, quote=True)
        cls = ' class="remote-img"' if remote else ""
        title = ' title="远程图片（本地下载失败）"' if remote else ""
        parts.append(
            f'<a href="{src_e}" target="_blank" rel="noopener">'
            f'<img src="{src_e}" alt="media" loading="lazy"{cls}{title}></a>'
        )
    if not parts:
        return ""
    return f'<div class="media">{"".join(parts)}</div>'


def render_quote_block(q: dict | None) -> str:
    if not isinstance(q, dict):
        return ""
    images = q.get("image_meta") or q.get("images") or []
    if not (q.get("text") or q.get("author") or images):
        return ""
    qauthor = html.escape(q.get("author") or "")
    qname = html.escape(q.get("name") or "")
    qtext = html.escape(q.get("text") or "").replace("\n", "<br>\n")
    qurl = html.escape(q.get("url") or "", quote=True)
    inner = ""
    nested = q.get("quote")
    if isinstance(nested, dict):
        inner = render_quote_block(nested)
    link = (
        f'<a class="quote-link" href="{qurl}" target="_blank" rel="noopener">查看原帖</a>'
        if qurl
        else ""
    )
    who = qname
    if qauthor:
        who = f"{qname} <span class=\"handle\">@{qauthor}</span>".strip()
    return f"""
      <blockquote class="quote">
        <div class="quote-author">{who}</div>
        <div class="quote-text">{qtext}</div>
        {render_media(images)}
        {inner}
        {link}
      </blockquote>"""


def render_html(posts: list[dict], updated_shanghai: str, failures: list[str]) -> str:
    n_rt = sum(1 for p in posts if p.get("type") == "转发")
    n_q = sum(1 for p in posts if p.get("type") == "引用")
    n_o = sum(1 for p in posts if p.get("type") == "原文")
    enriched = sum(1 for p in posts if p.get("enriched"))

    cards = []
    for p in posts:
        tid = html.escape(p["id"])
        tlabel = html.escape(p.get("type_label") or "原文")
        time_s = html.escape(p.get("created_at_shanghai") or "")
        body = html.escape(p.get("text") or "").replace("\n", "<br>\n")
        url = html.escape(p.get("url") or "#", quote=True)
        eng = p.get("engagement") or {}
        eng_line = (
            f"♥ {fmt_num(eng.get('likes'))} · "
            f"↻ {fmt_num(eng.get('retweets'))} · "
            f"💬 {fmt_num(eng.get('replies'))} · "
            f"👁 {fmt_num(eng.get('views'))}"
        )
        quote_html = render_quote_block(p.get("quote"))
        imgs_html = render_media(p.get("image_meta") or p.get("images") or [])
        type_class = {"原文": "t-orig", "引用": "t-quote", "转发": "t-rt"}.get(
            p.get("type_label"), "t-orig"
        )
        body_cls = "body rt-line" if p.get("type") == "转发" else "body"

        cards.append(f"""
    <article class="card" data-id="{tid}" data-type="{tlabel}">
      <header class="card-head">
        <div class="avatar" aria-hidden="true">𝕏</div>
        <div class="meta">
          <div class="name-row">
            <span class="name">Elon Musk</span>
            <span class="handle">@elonmusk</span>
            <span class="badge {type_class}">{tlabel}</span>
          </div>
          <time datetime="{html.escape(p.get('created_at_utc') or '')}">{time_s} CST</time>
        </div>
      </header>
      <div class="{body_cls}">{body}</div>
      {quote_html}
      {imgs_html}
      <div class="eng">{html.escape(eng_line)}</div>
      <a class="xlink" href="{url}" target="_blank" rel="noopener">在 X 打开 →</a>
    </article>""")

    cards_joined = "\n".join(cards) if cards else '<p class="empty">暂无帖子</p>'
    fail_note = ""
    if failures:
        fail_note = (
            f'<p class="note">部分 enrichment 失败: {html.escape(str(len(failures)))} 条</p>'
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta http-equiv="refresh" content="300">
  <title>马斯克 X 盯盘</title>
  <style>
    :root {{
      --bg: #000;
      --card: #16181c;
      --border: #2f3336;
      --text: #e7e9ea;
      --muted: #71767b;
      --accent: #1d9bf0;
      --quote-bg: #0a0a0a;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    html, body {{
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC",
        "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
      line-height: 1.45;
      -webkit-text-size-adjust: 100%;
    }}
    .wrap {{
      max-width: 600px;
      margin: 0 auto;
      padding: 12px 12px 48px;
    }}
    header.top {{
      position: sticky;
      top: 0;
      z-index: 10;
      background: rgba(0,0,0,0.85);
      backdrop-filter: blur(12px);
      -webkit-backdrop-filter: blur(12px);
      padding: 14px 4px 12px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 12px;
    }}
    header.top h1 {{
      font-size: 1.25rem;
      font-weight: 800;
      letter-spacing: 0.02em;
    }}
    header.top .sub {{
      color: var(--muted);
      font-size: 0.8rem;
      margin-top: 4px;
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 14px 14px 12px;
      margin-bottom: 12px;
    }}
    .card-head {{
      display: flex;
      gap: 10px;
      align-items: flex-start;
      margin-bottom: 10px;
    }}
    .avatar {{
      width: 40px; height: 40px;
      border-radius: 50%;
      background: #111;
      border: 1px solid var(--border);
      display: flex; align-items: center; justify-content: center;
      font-size: 1.1rem;
      flex-shrink: 0;
      color: var(--text);
    }}
    .meta {{ flex: 1; min-width: 0; }}
    .name-row {{
      display: flex; flex-wrap: wrap; gap: 6px; align-items: center;
    }}
    .name {{ font-weight: 700; font-size: 0.95rem; }}
    .handle {{ color: var(--muted); font-size: 0.85rem; }}
    .badge {{
      font-size: 0.7rem;
      padding: 2px 8px;
      border-radius: 999px;
      border: 1px solid var(--border);
      color: var(--muted);
    }}
    .t-quote {{ color: #a78bfa; border-color: #4c1d95; }}
    .t-rt {{ color: #4ade80; border-color: #14532d; }}
    .t-orig {{ color: var(--accent); border-color: #0c4a6e; }}
    time {{ display: block; color: var(--muted); font-size: 0.75rem; margin-top: 2px; }}
    .body {{
      font-size: 0.98rem;
      white-space: pre-wrap;
      word-break: break-word;
      margin-bottom: 8px;
    }}
    .rt-line {{ color: #86efac; font-weight: 600; }}
    .quote {{
      margin: 8px 0 10px;
      padding: 10px 12px;
      border: 1px solid var(--border);
      border-radius: 12px;
      background: var(--quote-bg);
      border-left: 3px solid var(--accent);
    }}
    .quote .quote {{
      border-left-color: #a78bfa;
    }}
    .quote-author {{ font-size: 0.85rem; margin-bottom: 4px; }}
    .quote-text {{
      font-size: 0.9rem;
      color: #cfd9de;
      white-space: pre-wrap;
      word-break: break-word;
    }}
    .quote-link {{
      display: inline-block;
      margin-top: 6px;
      font-size: 0.75rem;
      color: var(--accent);
      text-decoration: none;
    }}
    .media {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 6px;
      margin: 8px 0 10px;
    }}
    .media img {{
      width: 100%;
      height: auto;
      max-height: 320px;
      object-fit: cover;
      border-radius: 12px;
      border: 1px solid var(--border);
      display: block;
      background: #000;
    }}
    .quote .media img {{ max-height: 260px; }}
    .media img.remote-img {{
      border-color: #92400e;
    }}
    .eng {{
      color: var(--muted);
      font-size: 0.8rem;
      margin: 6px 0 8px;
    }}
    .xlink {{
      color: var(--accent);
      font-size: 0.85rem;
      text-decoration: none;
      font-weight: 600;
    }}
    .xlink:active, .xlink:hover {{ text-decoration: underline; }}
    .note, .empty {{
      color: var(--muted);
      font-size: 0.85rem;
      text-align: center;
      padding: 16px;
    }}
    footer.foot {{
      text-align: center;
      color: var(--muted);
      font-size: 0.75rem;
      padding: 20px 8px;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <header class="top">
      <h1>马斯克 X 盯盘</h1>
      <div class="sub">最近更新：{html.escape(updated_shanghai)} CST · 补全 {enriched}/{len(posts)} · 转发 {n_rt} · 引用 {n_q} · 原文 {n_o}</div>
    </header>
    {fail_note}
    <main id="feed">
{cards_joined}
    </main>
    <footer class="foot">静态站 · 数据来自公开 API · 仅供盯盘</footer>
  </div>
</body>
</html>
"""


def _quote_fingerprint(q: Any) -> Any:
    if not isinstance(q, dict):
        return None
    return {
        "author": q.get("author"),
        "text": q.get("text"),
        "url": q.get("url"),
        "images": q.get("images") or [],
        "quote": _quote_fingerprint(q.get("quote")),
    }


def content_fingerprint(posts: list[dict]) -> str:
    """Stable fingerprint of feed content (ignore updated_at) for change detection."""
    slim = []
    for p in posts:
        slim.append(
            {
                "id": p.get("id"),
                "text": p.get("text"),
                "type": p.get("type"),
                "url": p.get("url"),
                "images": p.get("images"),
                "quote": _quote_fingerprint(p.get("quote")),
                "created_at_utc": p.get("created_at_utc"),
                "engagement": p.get("engagement"),
            }
        )
    return json.dumps(slim, ensure_ascii=False, sort_keys=True)


def main() -> int:
    DOCS.mkdir(parents=True, exist_ok=True)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []

    try:
        raw_posts = fetch_primary()
    except Exception as e:
        print(f"FATAL primary API: {e}", file=sys.stderr)
        return 1

    enriched_map: dict[str, dict] = {}
    print(f"Enriching all {len(raw_posts)} posts via fxtwitter...")
    started = time.monotonic()
    pause = ENRICH_SLEEP
    for i, raw in enumerate(raw_posts, 1):
        if time.monotonic() - started > ENRICH_BUDGET_S:
            msg = f"enrich budget {ENRICH_BUDGET_S}s exceeded after {i - 1} posts"
            print(msg)
            failures.append(msg)
            break
        tid = str(raw.get("platformId") or "")
        if not tid:
            continue
        print(f"[{i}/{len(raw_posts)}] {tid}")
        tweet, err = enrich_one(tid)
        if tweet:
            enriched_map[tid] = tweet
        else:
            msg = f"{tid}: {err}"
            failures.append(msg)
            print(f"  skip: {err}")
            if err and "429" in err:
                pause = min(pause * 2, 3.0)
        time.sleep(pause)

    posts: list[dict] = []
    for raw in raw_posts:
        tid = str(raw.get("platformId") or "")
        if not tid:
            continue
        posts.append(build_post(raw, enriched_map.get(tid)))

    now_sh = datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
    enriched_count = sum(1 for p in posts if p.get("enriched"))
    type_counts = {
        "原文": sum(1 for p in posts if p.get("type") == "原文"),
        "引用": sum(1 for p in posts if p.get("type") == "引用"),
        "转发": sum(1 for p in posts if p.get("type") == "转发"),
    }
    with_images = 0
    for p in posts:
        if p.get("images"):
            with_images += 1
            continue
        q = p.get("quote") or {}
        if isinstance(q, dict) and (q.get("images") or (isinstance(q.get("quote"), dict) and q["quote"].get("images"))):
            with_images += 1
    payload = {
        "updated_at_shanghai": now_sh,
        "source": "xtracker.polymarket.com + api.fxtwitter.com",
        "count": len(posts),
        "enriched_count": enriched_count,
        "type_counts": type_counts,
        "posts_with_images": with_images,
        "failures": failures,
        "posts": posts,
    }

    new_fp = content_fingerprint(posts)
    old_fp = ""
    if FEED_JSON.exists():
        try:
            old = json.loads(FEED_JSON.read_text(encoding="utf-8"))
            old_fp = content_fingerprint(old.get("posts") or [])
        except Exception:
            old_fp = ""

    FEED_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    INDEX_HTML.write_text(render_html(posts, now_sh, failures), encoding="utf-8")

    changed = new_fp != old_fp
    print(f"Wrote {len(posts)} posts -> {FEED_JSON}")
    print(f"Wrote HTML -> {INDEX_HTML}")
    print(f"Enriched: {enriched_count}/{len(posts)}, failures: {len(failures)}")
    print(
        f"Types: 原文={type_counts['原文']} 引用={type_counts['引用']} 转发={type_counts['转发']}"
    )
    print(f"Posts with images: {with_images}")
    print(f"CONTENT_CHANGED={'yes' if changed else 'no'}")
    if posts:
        n = posts[0]
        print(f"Newest: id={n['id']} time={n['created_at_shanghai']} CST type={n['type']}")
        print(f"Snippet: {(n.get('text') or '')[:120]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
