"""Whether this Graphban instance is current against the published stable cut (P32).

Check only — Apply is a later slice. Three states: `current`, `available`, `unknown`.
Unknown must not look like current: a failed feed fetch, or a running version that is
still the `0.1.0` placeholder, is `unknown` even if a latest tag happens to match.
"""
from __future__ import annotations

import logging
import re

import httpx

from app.config import settings
from app.version import __version__

logger = logging.getLogger("graphban.instance_update")

STATES = ("current", "available", "unknown")
PLACEHOLDER_VERSIONS = frozenset({"", "unknown", "0.1.0"})
FEED_URL = "https://api.github.com/repos/asc-me/graphban/releases/latest"
_FETCH_TIMEOUT = 3.0
_TAG_PREFIX = re.compile(r"^v", re.I)

NOTE_PLACEHOLDER = (
    "this instance does not report a product version — not current, not up to date"
)
NOTE_UNREACHABLE = "could not reach the update feed — not current"
NOTE_CURRENT = ""
NOTE_AVAILABLE = ""


def running() -> dict:
    return {"version": __version__, "git_sha": settings.resolved_git_sha}


def fetch_latest() -> dict | None:
    """`{tag, url}` for the latest GitHub Release, or None on any failure. Never raises."""
    try:
        resp = httpx.get(
            FEED_URL,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "graphban-api"},
            timeout=_FETCH_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning("update feed status=%s", resp.status_code)
            return None
        body = resp.json()
        tag = _TAG_PREFIX.sub("", str(body.get("tag_name") or "").strip())
        url = str(body.get("html_url") or "").strip()
        if not tag:
            return None
        return {"tag": tag, "url": url}
    except (httpx.TimeoutException, httpx.HTTPError, OSError, ValueError, TypeError,
            KeyError) as e:
        logger.warning("update feed failed: %s", type(e).__name__)
        return None


def check(*, fetch=None, run=None, hosted: bool | None = None) -> dict:
    """The three-state payload the Updates page renders. `fetch` is injectable so the
    CALL tests can pin the feed without opening a socket.

    Defaults are looked up at call time, not bind time — a default `fetch=fetch_latest`
    would keep the original function after a test monkeypatches the name, and the REST
    CALL would still hit GitHub.
    """
    fetch_fn = fetch if fetch is not None else fetch_latest
    got = run if run is not None else running()
    version = (got.get("version") or "").strip()
    raw = fetch_fn()
    latest = None
    if raw and raw.get("tag"):
        latest = {"tag": _TAG_PREFIX.sub("", str(raw["tag"]).strip()), "url": str(raw.get("url") or "")}
    hosted_flag = settings.hosted_mode if hosted is None else hosted
    base = {
        "state": "unknown",
        "running": {"version": version or "unknown", "git_sha": got.get("git_sha") or "unknown"},
        "latest": latest,
        "apply": False,
        "hosted": bool(hosted_flag),
        "note": NOTE_UNREACHABLE,
    }
    if version in PLACEHOLDER_VERSIONS:
        base["note"] = NOTE_PLACEHOLDER
        return base
    if latest is None:
        return base
    if version == latest["tag"]:
        base["state"] = "current"
        base["note"] = NOTE_CURRENT
        return base
    base["state"] = "available"
    base["note"] = NOTE_AVAILABLE
    return base
