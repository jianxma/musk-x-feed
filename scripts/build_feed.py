#!/usr/bin/env python3
"""Build a static GitHub Pages site for Elon Musk (@elonmusk) X posts.

Fetches last 3 days from xtracker, enriches top ~25 via fxtwitter,
writes docs/index.html + docs/feed.json, downloads images to docs/media/.
Uses stdlib urllib only. Idempotent (skips existing media files).
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
ENRICH_LIMIT = 25
MAX_IMAGE_BYTES = 2_500_000
REQUEST_TIMEOUT = 25
DAYS_BACK = 3


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


def classify_type(tweet: dict | None) -> str:
    if tweet:
        if tweet.get("retweet"):
            return "转发"
        if tweet.get("quote"):
            return "引用"
    return "原文"


def extract_media_items(tweet: dict) -> list[dict]:
    """Return list of {id, url, kind} for downloadable still images / thumbs."""
    out: list[dict] = []
    media = tweet.get("media") or {}
    if not isinstance(media, dict):
        return out

    photos = media.get("photos") or []
    for ph in photos:
        if not isinstance(ph, dict):
            continue
        url = ph.get("url")
        mid = str(ph.get("id") or "")
        if url:
            u = url.replace("?name=orig", "?name=small") if "?name=" in url else url
            if "?name=" not in u:
                u = u + ("&name=small" if "?" in u else "?name=small")
            out.append({"id": mid, "url": u, "kind": "photo"})

    for item in media.get("all") or []:
        if not isinstance(item, dict):
            continue
        mtype = (item.get("type") or "").lower()
        mid = str(item.get("id") or "")
        if mtype in ("video", "gif"):
            thumb = item.get("thumbnail_url")
            if thumb and not any(x["id"] == mid for x in out):
                out.append({"id": mid or "thumb", "url": thumb, "kind": "thumb"})
        elif mtype == "photo":
            url = item.get("url")
            if url and not any(x["id"] == mid for x in out):
                u = url.replace("?name=orig", "?name=small") if "?name=" in url else url
                if "?name=" not in u:
                    u = u + ("&name=small" if "?" in u else "?name=small")
                out.append({"id": mid, "url": u, "kind": "photo"})

    return out


def download_media(tweet_id: str, items: list[dict]) -> list[dict]:
    """Download images; return list of {src, remote, local}.

    src is relative media/... when download succeeds (or file already exists);
    otherwise src is the remote URL and remote=True so the HTML can still show it.
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
            # Mark remote URL so the page can still render the image
            results.append({"src": url, "remote": True, "local": False, "url": url})
            print(f"  keep remote URL for {tweet_id}_{mid}")
    return results


def enrich_one(tweet_id: str) -> tuple[dict | None, str | None]:
    url = FXT_URL.format(id=tweet_id)
    try:
        data = http_json(url)
        if not isinstance(data, dict) or data.get("code") != 200:
            return None, f"non-200: {data.get('code') if isinstance(data, dict) else data}"
        return data.get("tweet"), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, str(e)


def build_post(raw: dict, tweet: dict | None) -> dict:
    tid = str(raw.get("platformId") or "")
    created = raw.get("createdAt") or ""
    content = (raw.get("content") or "").strip()
    full_text = content
    quote_block = None
    engagement: dict[str, Any] = {}
    images: list[dict] = []
    post_type = "原文"
    link = f"https://x.com/elonmusk/status/{tid}"

    if tweet:
        full_text = (tweet.get("text") or content or "").strip()
        post_type = classify_type(tweet)
        link = tweet.get("url") or link
        engagement = {
            "likes": tweet.get("likes"),
            "retweets": tweet.get("retweets"),
            "replies": tweet.get("replies"),
            "bookmarks": tweet.get("bookmarks"),
            "quotes": tweet.get("quotes"),
            "views": tweet.get("views"),
        }
        q = tweet.get("quote")
        if isinstance(q, dict):
            qa = q.get("author") or {}
            quote_block = {
                "author": qa.get("screen_name") or "",
                "name": qa.get("name") or "",
                "text": (q.get("text") or "").strip(),
                "url": q.get("url") or "",
            }
        rt = tweet.get("retweet")
        if isinstance(rt, dict) and not full_text:
            ra = rt.get("author") or {}
            full_text = (rt.get("text") or "").strip()
            quote_block = {
                "author": ra.get("screen_name") or "",
                "name": ra.get("name") or "",
                "text": full_text,
                "url": rt.get("url") or "",
            }
            full_text = f"RT @{quote_block['author']}"
            post_type = "转发"

        media_items = extract_media_items(tweet)
        if media_items:
            images = download_media(tid, media_items)

    # Convenience list of src strings for simple consumers
    image_srcs = [img["src"] for img in images]

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
        "images": image_srcs,
        "image_meta": images,  # includes remote flags
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


def render_html(posts: list[dict], updated_shanghai: str, failures: list[str]) -> str:
    cards = []
    for p in posts:
        tid = html.escape(p["id"])
        tlabel = html.escape(p.get("type_label") or "原文")
        time_s = html.escape(p.get("created_at_shanghai") or "")
        body = html.escape(p.get("text") or "").replace("\n", "<br>\n")
        url = html.escape(p.get("url") or "#")
        eng = p.get("engagement") or {}
        eng_line = (
            f"♥ {fmt_num(eng.get('likes'))} · "
            f"↻ {fmt_num(eng.get('retweets'))} · "
            f"💬 {fmt_num(eng.get('replies'))} · "
            f"👁 {fmt_num(eng.get('views'))}"
        )

        quote_html = ""
        q = p.get("quote")
        if q and (q.get("text") or q.get("author")):
            qauthor = html.escape(q.get("author") or "")
            qname = html.escape(q.get("name") or "")
            qtext = html.escape(q.get("text") or "").replace("\n", "<br>\n")
            qurl = html.escape(q.get("url") or "")
            quote_html = f"""
      <blockquote class="quote">
        <div class="quote-author">{qname} <span class="handle">@{qauthor}</span></div>
        <div class="quote-text">{qtext}</div>
        {f'<a class="quote-link" href="{qurl}" target="_blank" rel="noopener">查看原帖</a>' if qurl else ''}
      </blockquote>"""

        imgs_html = ""
        # Prefer image_meta (has remote flag); fall back to images list
        meta = p.get("image_meta") or []
        if meta:
            parts = []
            for m in meta:
                src = html.escape(m.get("src") or "")
                remote = m.get("remote")
                cls = ' class="remote-img"' if remote else ""
                title = ' title="远程图片（本地下载失败）"' if remote else ""
                parts.append(
                    f'<a href="{src}" target="_blank" rel="noopener">'
                    f'<img src="{src}" alt="media" loading="lazy"{cls}{title}></a>'
                )
            imgs_html = f'<div class="media">{"".join(parts)}</div>'
        else:
            images = p.get("images") or []
            if images:
                imgs = "".join(
                    f'<a href="{html.escape(src)}" target="_blank" rel="noopener">'
                    f'<img src="{html.escape(src)}" alt="media" loading="lazy"></a>'
                    for src in images
                )
                imgs_html = f'<div class="media">{imgs}</div>'

        type_class = {"原文": "t-orig", "引用": "t-quote", "转发": "t-rt"}.get(
            p.get("type_label"), "t-orig"
        )

        cards.append(f"""
    <article class="card" data-id="{tid}">
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
      <div class="body">{body}</div>
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
    .quote {{
      margin: 8px 0 10px;
      padding: 10px 12px;
      border: 1px solid var(--border);
      border-radius: 12px;
      background: var(--quote-bg);
      border-left: 3px solid var(--accent);
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
      <div class="sub">最近更新：{html.escape(updated_shanghai)} CST · GitHub Pages 定时刷新 · @elonmusk</div>
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


def content_fingerprint(posts: list[dict]) -> str:
    """Stable fingerprint of feed content (ignore updated_at) for change detection."""
    slim = []
    for p in posts:
        slim.append(
            {
                "id": p.get("id"),
                "text": p.get("text"),
                "type": p.get("type"),
                "images": p.get("images"),
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

    to_enrich = raw_posts[:ENRICH_LIMIT]
    enriched_map: dict[str, dict] = {}
    print(f"Enriching top {len(to_enrich)} via fxtwitter...")
    for i, raw in enumerate(to_enrich, 1):
        tid = str(raw.get("platformId") or "")
        if not tid:
            continue
        print(f"[{i}/{len(to_enrich)}] {tid}")
        tweet, err = enrich_one(tid)
        if tweet:
            enriched_map[tid] = tweet
        else:
            msg = f"{tid}: {err}"
            failures.append(msg)
            print(f"  skip: {err}")
        time.sleep(0.15)

    posts: list[dict] = []
    for raw in raw_posts:
        tid = str(raw.get("platformId") or "")
        if not tid:
            continue
        tweet = enriched_map.get(tid)
        posts.append(build_post(raw, tweet))

    now_sh = datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
    payload = {
        "updated_at_shanghai": now_sh,
        "source": "xtracker.polymarket.com + api.fxtwitter.com",
        "count": len(posts),
        "enriched_count": sum(1 for p in posts if p.get("enriched")),
        "failures": failures,
        "posts": posts,
    }

    # Idempotent content write: always refresh HTML/JSON, but print change hint
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
    print(f"Enriched: {payload['enriched_count']}, failures: {len(failures)}")
    print(f"CONTENT_CHANGED={'yes' if changed else 'no'}")
    if posts:
        n = posts[0]
        print(f"Newest: id={n['id']} time={n['created_at_shanghai']} CST")
        print(f"Snippet: {(n.get('text') or '')[:120]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
