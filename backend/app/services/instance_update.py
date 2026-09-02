"""Whether this Graphban instance is current against the published stable cut (P32).

Three states: `current`, `available`, `unknown`. Unknown must not look like current:
a failed feed fetch, or a running version that is still the `0.1.0` placeholder, is
`unknown` even if a latest tag happens to match.

`apply` is true when this is not hosted AND (a compose host helper is on the
unix socket, or `/opt/graphban/current` is a native install). The API container
does not get a Docker socket. Native apply fetches the GitHub Release tarball
and starts `graphban_host.py upgrade` — not the source zip.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile

import httpx

from app.config import settings
from app.version import __version__

logger = logging.getLogger("graphban.instance_update")

STATES = ("current", "available", "unknown")
PLACEHOLDER_VERSIONS = frozenset({"", "unknown", "0.1.0"})
FEED_URL = "https://api.github.com/repos/asc-me/graphban/releases/latest"
TAG_URL = "https://api.github.com/repos/asc-me/graphban/releases/tags/{tag}"
_FETCH_TIMEOUT = 3.0
_TAG_PREFIX = re.compile(r"^v", re.I)
_GH_HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "graphban-api"}
NOTES_STATES = ("present", "empty", "unknown")
# Inside the API container. Compose mounts GRAPHBAN_APPLY_DIR here. Not a Settings
# field — absence of the socket is apply=false, not a knob.
SOCKET_PATH = "/run/graphban-apply/apply.sock"
NATIVE_ROOT = "/opt/graphban"
_HELPER_TIMEOUT = 3.0
_DOWNLOAD_TIMEOUT = 60.0

NOTE_PLACEHOLDER = (
    "this instance does not report a product version — not current, not up to date"
)
NOTE_UNREACHABLE = "could not reach the update feed — not current"
NOTE_CURRENT = ""
NOTE_AVAILABLE = ""


def running() -> dict:
    return {"version": __version__, "git_sha": settings.resolved_git_sha}


def native_present(root: str | None = None) -> bool:
    """True when this process is a native install (`current/backend` exists)."""
    r = pathlib.Path(root if root is not None else NATIVE_ROOT)
    return (r / "current" / "backend").is_dir()


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


def _parse_release(body: dict) -> dict | None:
    """Shared GitHub Release shape. `notes_body` is the markdown; missing/null is empty."""
    tag = _TAG_PREFIX.sub("", str(body.get("tag_name") or "").strip())
    url = str(body.get("html_url") or "").strip()
    if not tag:
        return None
    want = f"graphban-{tag}.tar.gz"
    asset = ""
    for item in body.get("assets") or []:
        if str(item.get("name") or "") == want:
            asset = str(item.get("browser_download_url") or "").strip()
            break
    raw = body.get("body")
    notes_body = "" if raw is None else str(raw)
    return {"tag": tag, "url": url, "asset": asset, "notes_body": notes_body}


def notes_payload(rel: dict | None, tag: str) -> dict:
    """`state` is present / empty / unknown. Unknown is not an empty changelog."""
    tag = _TAG_PREFIX.sub("", (tag or "").strip()) or "unknown"
    if rel is None:
        return {"tag": tag, "state": "unknown", "body": ""}
    text = str(rel["notes_body"]) if "notes_body" in rel else str(rel.get("body") or "")
    if not text.strip():
        return {"tag": tag, "state": "empty", "body": ""}
    return {"tag": tag, "state": "present", "body": text}


def fetch_latest() -> dict | None:
    """Latest GitHub Release, or None on any failure. Never raises."""
    try:
        resp = httpx.get(FEED_URL, headers=_GH_HEADERS, timeout=_FETCH_TIMEOUT)
        if resp.status_code != 200:
            logger.warning("update feed status=%s", resp.status_code)
            return None
        return _parse_release(resp.json())
    except (httpx.TimeoutException, httpx.HTTPError, OSError, ValueError, TypeError,
            KeyError) as e:
        logger.warning("update feed failed: %s", type(e).__name__)
        return None


def fetch_tag(tag: str) -> dict | None:
    """GitHub Release for one tag, or None on any failure (including 404). Never raises."""
    tag = _TAG_PREFIX.sub("", (tag or "").strip())
    if not tag or tag in PLACEHOLDER_VERSIONS:
        return None
    try:
        resp = httpx.get(
            TAG_URL.format(tag=tag), headers=_GH_HEADERS, timeout=_FETCH_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning("update tag feed status=%s tag=%s", resp.status_code, tag)
            return None
        return _parse_release(resp.json())
    except (httpx.TimeoutException, httpx.HTTPError, OSError, ValueError, TypeError,
            KeyError) as e:
        logger.warning("update tag feed failed: %s", type(e).__name__)
        return None


def start_native(tag: str, asset: str, *, root: str | None = None,
                 download=None, popen=subprocess.Popen) -> dict:
    """Fetch the packed tarball and start `graphban_host.py upgrade` detached.

    Waiting for upgrade to finish is wrong: it stops this process. GitHub's
    source zip is not a release — no `asset` is a hard fail, not a zipball.
    """
    if not asset:
        return {"ok": False, "error": "no release tarball on the GitHub Release"}
    root_p = pathlib.Path(root if root is not None else NATIVE_ROOT)
    host = root_p / "current" / "scripts" / "graphban_host.py"
    if not host.is_file():
        return {"ok": False, "error": "graphban_host.py missing on this install"}
    fetch = download if download is not None else _download_asset
    try:
        blob = fetch(asset)
    except (httpx.HTTPError, OSError, ValueError) as e:
        logger.warning("native tarball fetch failed: %s", type(e).__name__)
        return {"ok": False, "error": "could not fetch the release tarball"}
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="graphban-native-"))
    tar_path = tmp / "release.tar.gz"
    tar_path.write_bytes(blob)
    try:
        with tarfile.open(tar_path, "r:gz") as tf:
            tf.extractall(tmp, filter="data")
    except (tarfile.TarError, OSError) as e:
        return {"ok": False, "error": f"tarball: {type(e).__name__}"}
    release = tmp / f"graphban-{tag}"
    if not release.is_dir():
        return {"ok": False, "error": "tarball is not a packed release directory"}
    if (release / "backend" / ".env").is_file() or (release / ".env").is_file():
        return {"ok": False, "error": "refusing a tarball that contains .env"}
    sha = (release / "GIT_SHA").read_text(encoding="utf-8").strip() if (
        release / "GIT_SHA").is_file() else ""
    if not sha or sha == "unknown":
        return {"ok": False, "error": "tarball has no GIT_SHA"}
    popen(
        [sys.executable, str(host), "upgrade",
         "--root", str(root_p), "--release", str(release), "--sha", sha],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {"ok": True, "started": True, "tag": tag, "sha": sha}


def _download_asset(url: str) -> bytes:
    resp = httpx.get(
        url,
        headers={"User-Agent": "graphban-api", "Accept": "application/octet-stream"},
        timeout=_DOWNLOAD_TIMEOUT,
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.content


def apply(tag: str, *, check_fn=None, send=None, native_start=None,
          hosted: bool | None = None) -> dict:
    """Start an apply of the advertised latest tag. JWT gate is the router.

    Compose: host helper runs deploy.sh. Native: fetch tarball, start upgrade.
    Waiting for either to finish is wrong: both stop this process.
    """
    payload = (check_fn or check)(hosted=hosted)
    if payload["hosted"]:
        return {"ok": False, "status": 403, "error": "hosted instances are updated by the operator"}
    if not payload.get("apply"):
        return {"ok": False, "status": 503,
                "error": "no apply path — compose helper is not running, and this is not a native install"}
    if payload["state"] != "available":
        return {"ok": False, "status": 409, "error": "no advertised cut to install"}
    latest = (payload.get("latest") or {}).get("tag") or ""
    if (tag or "").strip() != latest:
        return {"ok": False, "status": 409, "error": "tag is not the advertised cut"}
    via = (payload.get("via") or "").strip()
    if via not in ("compose", "native"):
        # Empty via is unknown, not compose. Defaulting here would start deploy.sh
        # on a box that never named a method.
        return {"ok": False, "status": 503,
                "error": "no apply path — method is unknown, not compose"}
    if via == "native":
        starter = native_start if native_start is not None else start_native
        asset = (payload.get("latest") or {}).get("asset") or ""
        got = starter(latest, asset)
    else:
        send_fn = send if send is not None else talk
        got = send_fn({"op": "apply", "tag": latest})
    if not got.get("ok"):
        return {"ok": False, "status": 502, "error": got.get("error") or "apply refused"}
    return {"ok": True, "status": 202, "started": True, "tag": latest,
            "via": via, **({"sha": got["sha"]} if got.get("sha") else {})}


def check(*, fetch=None, fetch_running=None, run=None, hosted: bool | None = None,
          helper=None, native=None) -> dict:
    """The three-state payload the Updates page renders. `fetch` is injectable so the
    CALL tests can pin the feed without opening a socket.

    Defaults are looked up at call time, not bind time — a default `fetch=fetch_latest`
    would keep the original function after a test monkeypatches the name, and the REST
    CALL would still hit GitHub.

    `notes.running` / `notes.latest` are GitHub Release bodies. Empty body is
    `empty`; a failed fetch is `unknown` — not the same. When `fetch=` is
    injected, the running tag is not fetched from GitHub unless `fetch_running`
    is passed.
    """
    fetch_fn = fetch if fetch is not None else fetch_latest
    if fetch_running is not None:
        running_fn = fetch_running
    elif fetch is None:
        running_fn = fetch_tag
    else:
        running_fn = lambda tag: None  # noqa: E731 — tests stay offline
    got = run if run is not None else running()
    version = (got.get("version") or "").strip()
    raw = fetch_fn()
    latest = None
    latest_tag = ""
    if raw and raw.get("tag"):
        latest_tag = _TAG_PREFIX.sub("", str(raw["tag"]).strip())
        latest = {
            "tag": latest_tag,
            "url": str(raw.get("url") or ""),
            "asset": str(raw.get("asset") or ""),
        }
    hosted_flag = settings.hosted_mode if hosted is None else hosted
    present = helper if helper is not None else helper_present()
    nat = native if native is not None else native_present()
    if hosted_flag:
        via = ""
        can = False
    elif present:
        via, can = "compose", True
    elif nat:
        via, can = "native", True
    else:
        via, can = "", False

    running_rel = None
    if version and version not in PLACEHOLDER_VERSIONS:
        if raw and latest_tag == version:
            running_rel = raw
        else:
            running_rel = running_fn(version)
    notes_running = notes_payload(running_rel, version or "unknown")
    notes_latest = None
    if latest and latest_tag != version:
        notes_latest = notes_payload(raw, latest_tag)

    base = {
        "state": "unknown",
        "running": {"version": version or "unknown", "git_sha": got.get("git_sha") or "unknown"},
        "latest": latest,
        "notes": {"running": notes_running, "latest": notes_latest},
        "apply": can,
        "via": via,
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
