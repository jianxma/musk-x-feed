#!/usr/bin/env python3
"""Registry of watched X accounts.

``docs/data/accounts.json`` is the list the site and the exporter share.
Each account's static timeline lives at ``docs/data/<handle>/``.

To add an account later:

1. Append ``{"handle", "name", "avatar"}`` to ``docs/data/accounts.json``.
   ``name`` is the display name. Tab order follows this list: the first four
   are pinned, the rest go under 「更多」.
2. Put that account's pages at ``docs/data/<handle>/manifest.json`` and
   ``page-N.json`` (same shape as ``/api/feed``).
3. ``sync.py --export`` writes that tree for handles in
   ``feed_core.synced_handles``. Today that is ``elonmusk`` (xtracker) and
   ``rocketlab`` (FxTwitter v2 timeline). Handles outside that set are left
   untouched under ``docs/data/<handle>/``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from feed_core import DEFAULT_MUSK_AVATAR

HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
DEFAULT_HANDLE = "elonmusk"


def normalize_handle(value: str | None) -> str:
    raw = str(value or "").strip()
    if raw.startswith("@"):
        raw = raw[1:]
    raw = raw.strip().lower()
    if not HANDLE_RE.fullmatch(raw):
        return ""
    return raw


def seed_accounts() -> list[dict[str, str]]:
    return [
        {
            "handle": DEFAULT_HANDLE,
            "name": "Elon Musk",
            "avatar": DEFAULT_MUSK_AVATAR,
        }
    ]


def coerce_account(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    handle = normalize_handle(raw.get("handle") or raw.get("screen_name"))
    if not handle:
        return None
    name = str(raw.get("name") or raw.get("display_name") or "").strip() or handle
    avatar = str(raw.get("avatar") or raw.get("avatar_url") or "").strip()
    if avatar and not (avatar.startswith("https://") or avatar.startswith("http://")):
        avatar = ""
    return {"handle": handle, "name": name, "avatar": avatar}


def load_accounts(path: Path) -> list[dict[str, str]]:
    """Return the watched list. Missing or empty files fall back to @elonmusk."""
    if not path.exists():
        return seed_accounts()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return seed_accounts()
    rows = data.get("accounts") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return seed_accounts()
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        acct = coerce_account(row)
        if not acct or acct["handle"] in seen:
            continue
        seen.add(acct["handle"])
        out.append(acct)
    return out or seed_accounts()


def dump_accounts(accounts: list[dict[str, str]]) -> str:
    return json.dumps({"accounts": accounts}, ensure_ascii=False, indent=2) + "\n"


def ensure_accounts_file(path: Path, accounts: list[dict[str, str]] | None = None) -> bool:
    """Write the registry only when it is missing. Returns True if created."""
    if path.exists():
        return False
    rows = accounts if accounts else seed_accounts()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_accounts(rows), encoding="utf-8")
    return True


def live_handles() -> set[str]:
    """Handles whose rows live in this SQLite database.

    The static site can list more accounts than this. Those stay on
    ``docs/data/<handle>/`` until a fetcher and ``posts_for_handle`` learn
    them. Do not add a handle here unless its rows are stored on ``account`` —
    the API would otherwise serve the wrong timeline.
    """
    from feed_core import synced_handles

    return synced_handles()
