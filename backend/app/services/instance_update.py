"""Whether this Graphban instance is current against the published stable cut (P32).

Three states: `current`, `available`, `unknown`. Unknown must not look like current:
a failed feed fetch, or a running version that is still the `0.1.0` placeholder, is
`unknown` even if a latest tag happens to match.

`apply` is true only when a compose host helper is on the unix socket AND this is
not hosted. The API container does not get a Docker socket.
"""
from __future__ import annotations

import json
import logging
import os
import re
import socket
import stat

import httpx

from app.config import settings
from app.version import __version__

logger = logging.getLogger("graphban.instance_update")

STATES = ("current", "available", "unknown")
PLACEHOLDER_VERSIONS = frozenset({"", "unknown", "0.1.0"})
FEED_URL = "https://api.github.com/repos/asc-me/graphban/releases/latest"
_FETCH_TIMEOUT = 3.0
_TAG_PREFIX = re.compile(r"^v", re.I)
# Inside the API container. Compose mounts GRAPHBAN_APPLY_DIR here. Not a Settings
# field — absence of the socket is apply=false, not a knob.
SOCKET_PATH = "/run/graphban-apply/apply.sock"
_HELPER_TIMEOUT = 3.0

NOTE_PLACEHOLDER = (
    "this instance does not report a product version — not current, not up to date"
)
NOTE_UNREACHABLE = "could not reach the update feed — not current"
NOTE_CURRENT = ""
NOTE_AVAILABLE = ""


def running() -> dict:
    return {"version": __version__, "git_sha": settings.resolved_git_sha}


def helper_present(path: str | None = None) -> bool:
    """True only for a unix socket. A missing path, a regular file, or `/dev/null`
    mounted in its place is not a helper — that must not enable Install.
    """
    raw = path if path is not None else SOCKET_PATH
    try:
        st = os.lstat(raw)
    except OSError:
        return False
    return stat.S_ISSOCK(st.st_mode)


def talk(msg: dict, path: str | None = None, *, timeout: float = _HELPER_TIMEOUT) -> dict:
    """One JSON line to the helper, one JSON line back. Never raises."""
    sock = path if path is not None else SOCKET_PATH
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(sock)
            s.sendall((json.dumps(msg) + "\n").encode("utf-8"))
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
        if not buf:
            return {"ok": False, "error": "helper closed"}
        return json.loads(buf.decode("utf-8"))
    except (OSError, socket.timeout, json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning("compose helper failed: %s", type(e).__name__)
        return {"ok": False, "error": type(e).__name__}


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


def apply(tag: str, *, check_fn=None, send=None, hosted: bool | None = None) -> dict:
    """Start a compose apply of the advertised latest tag. JWT gate is the router.

    Returns `{ok, status, ...}`. The router maps `status` onto HTTP. Waiting for
    deploy.sh to finish is wrong: it recreates this process.
    """
    payload = (check_fn or check)(hosted=hosted)
    if payload["hosted"]:
        return {"ok": False, "status": 403, "error": "hosted instances are updated by the operator"}
    if not payload.get("apply"):
        return {"ok": False, "status": 503, "error": "compose host helper is not running"}
    if payload["state"] != "available":
        return {"ok": False, "status": 409, "error": "no advertised cut to install"}
    latest = (payload.get("latest") or {}).get("tag") or ""
    if (tag or "").strip() != latest:
        return {"ok": False, "status": 409, "error": "tag is not the advertised cut"}
    send_fn = send if send is not None else talk
    got = send_fn({"op": "apply", "tag": latest})
    if not got.get("ok"):
        return {"ok": False, "status": 502, "error": got.get("error") or "helper refused"}
    return {"ok": True, "status": 202, "started": True, "tag": latest}


def check(*, fetch=None, run=None, hosted: bool | None = None, helper=None) -> dict:
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
    present = helper if helper is not None else helper_present()
    base = {
        "state": "unknown",
        "running": {"version": version or "unknown", "git_sha": got.get("git_sha") or "unknown"},
        "latest": latest,
        "apply": (not hosted_flag) and bool(present),
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
