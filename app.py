#!/usr/bin/env python3
"""
Local Musk X feed server (SQLite + paginated API).

  python3 app.py
  GET /                              mobile-first timeline (docs/index.html)
  GET /api/feed?page=1&page_size=20  paginated JSON from SQLite

GitHub Pages cannot run this process. `python3 export_pages.py` writes the
static JSON the same SPA reads when /api/feed is unavailable.
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import db
from accounts import DEFAULT_HANDLE, live_handles, normalize_handle
from feed_core import now_shanghai, sync_incremental

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8787"))
SYNC_INTERVAL_SEC = int(os.environ.get("SYNC_INTERVAL", "90"))

_sync_lock = threading.Lock()
_last_sync: dict[str, Any] = {"at": 0.0, "result": None, "error": None, "running": False}


def _run_sync(verbose: bool = False) -> dict:
    with _sync_lock:
        if _last_sync["running"]:
            return _last_sync.get("result") or {"skipped": True}
        _last_sync["running"] = True
    try:
        result = sync_incremental(enrich_limit=20, sleep_between=0.1, verbose=verbose)
        with _sync_lock:
            _last_sync["result"] = result
            _last_sync["at"] = time.time()
            _last_sync["error"] = None
            _last_sync["running"] = False
        return result
    except Exception as e:
        traceback.print_exc()
        with _sync_lock:
            _last_sync["error"] = str(e)
            _last_sync["running"] = False
        raise


def _bg_sync_loop() -> None:
    time.sleep(2)
    while True:
        try:
            print("Background sync starting...")
            r = _run_sync(verbose=True)
            print(
                f"Background sync: inserted={r.get('inserted')} updated={r.get('updated')} "
                f"enriched={r.get('enriched')} total={r.get('total')}"
            )
        except Exception as e:
            print(f"Background sync failed: {e}")
        time.sleep(SYNC_INTERVAL_SEC)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DOCS), **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys_msg = args[0] if args else fmt
        print(f"[{self.log_date_time_string()}] {sys_msg}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/api/feed", "/api/feed/"):
            self._serve_api(parsed)
            return
        if path in ("/api/sync", "/api/sync/"):
            self._serve_sync()
            return
        if path in ("/", "/index.html"):
            self._serve_index()
            return
        super().do_GET()

    def _serve_api(self, parsed) -> None:
        try:
            qs = parse_qs(parsed.query or "")
            try:
                page = int((qs.get("page") or ["1"])[0])
            except ValueError:
                page = 1
            try:
                page_size = int((qs.get("page_size") or ["20"])[0])
            except ValueError:
                page_size = 20
            account = normalize_handle((qs.get("account") or [DEFAULT_HANDLE])[0]) or DEFAULT_HANDLE
            # Unknown handles are static-only. 404 lets the SPA read
            # docs/data/<handle>/ instead of showing this database.
            if account not in live_handles():
                err = json.dumps(
                    {
                        "posts": [],
                        "page": 1,
                        "page_size": page_size,
                        "total": 0,
                        "total_pages": 1,
                        "updated_at_shanghai": now_shanghai(),
                        "account": account,
                        "error": "noapi",
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_response(404)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
                return
            data = db.get_posts_page(page=page, page_size=page_size, account=account)
            data["account"] = account
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            traceback.print_exc()
            err = json.dumps(
                {
                    "posts": [],
                    "page": 1,
                    "page_size": 20,
                    "total": 0,
                    "total_pages": 1,
                    "updated_at_shanghai": now_shanghai(),
                    "error": str(e),
                },
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)

    def _serve_sync(self) -> None:
        try:
            if _last_sync.get("running"):
                data = {"ok": True, "status": "already_running", "last": _last_sync.get("result")}
            else:
                threading.Thread(target=lambda: _run_sync(verbose=True), daemon=True).start()
                data = {"ok": True, "status": "started"}
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            err = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)

    def _serve_index(self) -> None:
        index = DOCS / "index.html"
        body = index.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    db.init_db()
    n = db.count_posts()
    print(f"DB ready: {n} posts at {db.DB_PATH}")
    if SYNC_INTERVAL_SEC > 0:
        threading.Thread(target=_bg_sync_loop, daemon=True).start()
    else:
        print("Background sync disabled (SYNC_INTERVAL=0)")

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"musk-feed on http://{HOST}:{PORT}/")
    print(f"  UI  http://127.0.0.1:{PORT}/")
    print(f"  API http://127.0.0.1:{PORT}/api/feed?page=1&page_size=20")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
