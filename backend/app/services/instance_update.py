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

import contextvars
import json
import logging
import os
import pathlib
import re
import socket
import stat
import subprocess  # nosec B404 — argv lists only, never shell=True
import sys
import tarfile
import tempfile
import threading
import time

import httpx

from app.config import settings
from app.version import __version__

logger = logging.getLogger("graphban.instance_update")

STATES = ("current", "available", "unknown")
PLACEHOLDER_VERSIONS = frozenset({"", "unknown", "0.1.0"})
FEED_URL = "https://api.github.com/repos/asc-me/graphban/releases/latest"
TAG_URL = "https://api.github.com/repos/asc-me/graphban/releases/tags/{tag}"
_FETCH_TIMEOUT = 3.0
# httpx timeouts are per phase and do not cover name resolution, so the whole check
# also runs under one wall-clock deadline (CHECK_DEADLINE).
CHECK_DEADLINE = 5.0
CACHE_TTL = 60.0
_TAG_PREFIX = re.compile(r"^v", re.I)
_GH_HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "graphban-api"}
NOTES_STATES = ("present", "empty", "unknown")
# Inside the API container. Compose mounts GRAPHBAN_APPLY_DIR here. Not a Settings
# field — absence of the socket is apply=false, not a knob.
SOCKET_PATH = "/run/graphban-apply/apply.sock"
NATIVE_ROOT = "/opt/graphban"
# Install logs. Compose: deploy.log beside the socket (written by the host helper).
# Native: under the install root (written by start_native).
COMPOSE_LOG = "/run/graphban-apply/deploy.log"
NATIVE_LOG = "upgrade.log"
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


# Why the current fetch failed. Each check() job runs on its own thread and sets its own
# holder here, so a reason belongs to one job of one call: concurrent requests, the
# running-tag fetch, and abandoned timed-out threads cannot overwrite each other.
_reason_slot: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "instance_update_reason", default=None)


def _note_reason(reason: str) -> None:
    holder = _reason_slot.get()
    if holder is not None:
        holder["reason"] = reason


def _get_release(url: str, label: str) -> dict | None:
    """One GitHub Release GET. Records why it failed for the calling job. Never raises."""
    try:
        resp = httpx.get(url, headers=_GH_HEADERS, timeout=_FETCH_TIMEOUT)
        if resp.status_code != 200:
            logger.warning("%s status=%s", label, resp.status_code)
            if resp.status_code in (403, 429):
                _note_reason(
                    f"GitHub returned {resp.status_code} — rate limited or throttled")
            else:
                _note_reason(f"GitHub returned {resp.status_code}")
            return None
        return _parse_release(resp.json())
    except (httpx.TimeoutException, httpx.HTTPError, OSError, ValueError, TypeError,
            KeyError) as e:
        logger.warning("%s failed: %s", label, type(e).__name__)
        _note_reason(f"could not reach GitHub ({type(e).__name__})")
        return None


def fetch_latest() -> dict | None:
    """Latest GitHub Release, or None on any failure. Never raises."""
    return _get_release(FEED_URL, "update feed")


def fetch_tag(tag: str) -> dict | None:
    """GitHub Release for one tag, or None on any failure (including 404). Never raises."""
    tag = _TAG_PREFIX.sub("", (tag or "").strip())
    if not tag or tag in PLACEHOLDER_VERSIONS:
        return None
    return _get_release(TAG_URL.format(tag=tag), f"update tag feed tag={tag}")


def _run_concurrently(jobs: dict, deadline: float) -> tuple[dict, dict]:
    """Run `jobs` (name -> zero-arg callable) on daemon threads and wait at most
    `deadline` seconds in total. Returns (results, reasons). A job that has not
    finished is absent from results; its thread is abandoned (a stuck getaddrinfo
    cannot be cancelled) and only ever writes to this call's private dicts.
    """
    out: dict = {}
    reasons: dict = {}

    def runner(name, fn):
        holder = {"reason": ""}
        _reason_slot.set(holder)  # a new thread starts with an empty context
        try:
            value = fn()
        except Exception as e:  # noqa: BLE001 — a failed fetch is unknown, not a 500
            logger.warning("update fetch %s raised %s", name, type(e).__name__)
            value = None
        reasons[name] = holder["reason"]
        out[name] = value

    threads = [threading.Thread(target=runner, args=(n, f), daemon=True)
               for n, f in jobs.items()]
    for t in threads:
        t.start()
    end = time.monotonic() + deadline
    for t in threads:
        t.join(max(0.0, end - time.monotonic()))
    return dict(out), dict(reasons)


_cache: dict = {"at": 0.0, "payload": None}


def check_cached(*, refresh: bool = False) -> dict:
    """`check()` with a short cache. `refresh` bypasses it (the Check button).
    Only a definite answer is cached; `unknown` is retried on the next call.
    """
    now = time.monotonic()
    hit = _cache["payload"]
    if not refresh and hit is not None and now - _cache["at"] < CACHE_TTL:
        return hit
    payload = check()
    if payload["state"] != "unknown":
        _cache["at"], _cache["payload"] = now, payload
    else:
        _cache["payload"] = None
    return payload


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
    # The upgrade's output is the install's progress. Same start marker as the compose
    # helper writes, so update-progress reads only this attempt.
    log_p = root_p / NATIVE_LOG
    try:
        log_p.parent.mkdir(parents=True, exist_ok=True)
        log = log_p.open("ab", buffering=0)
    except OSError as e:
        return {"ok": False, "error": f"cannot write {log_p}: {type(e).__name__}"}
    try:
        log.write(f"\n=== apply {tag} {time.strftime('%Y-%m-%dT%H:%M:%S%z')} "
                  f"{int(time.time())}\n".encode("utf-8"))
        popen(
            [sys.executable, str(host), "upgrade",
             "--root", str(root_p), "--release", str(release), "--sha", sha],
            start_new_session=True,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    finally:
        log.close()  # the child holds its own descriptor
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
    jobs = {"latest": fetch_fn}
    if version and version not in PLACEHOLDER_VERSIONS:
        # Concurrent with the latest fetch; dropped below when latest is the running tag.
        jobs["running"] = lambda: running_fn(version)
    done, reasons = _run_concurrently(jobs, CHECK_DEADLINE)
    timed_out = "latest" not in done
    raw = done.get("latest")
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
            running_rel = done.get("running")
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
        "feed_error": "",
    }
    if raw is None:
        base["feed_error"] = (
            f"the update feed did not answer within {CHECK_DEADLINE:g}s"
            if timed_out else (reasons.get("latest") or "the update feed returned nothing"))
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


# --- install progress -------------------------------------------------------------------

PROGRESS_STATES = ("idle", "running", "succeeded", "failed")
_MARKER = re.compile(r"^=== apply (\S+) (\S+)(?: (\d+))?\s*$")
_LINE_CAP = 300
_TAIL_BYTES = 2 * 1024 * 1024
# The `==>` lines deploy.sh and graphban_upgrade print. Matched by prefix, in script order;
# a step the log never printed stays pending. No step is invented.
KNOWN_STEPS = (
    ("worktree", "Creating the deploy worktree", "creating the deploy worktree"),
    ("pin", "Pinning the deploy worktree", "pinning the deploy worktree"),
    ("ship", "Shipping the release to the box", "shipping "),
    ("build", "Building the images", "building on "),
    ("verify", "Verifying release identity", "verifying release identity"),
    ("live", "Release is live", None),  # "<sha> is live" — matched by suffix
)
_SECRET_KV = re.compile(
    r"(?i)\b([A-Za-z0-9_.-]*(?:token|secret|passw(?:or)?d|api[_-]?key|credential|private[_-]?key)"
    r"[A-Za-z0-9_.-]*)(\s*[=:]\s*)(?:\"[^\"]*\"|'[^']*'|\S+)")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_URL_CRED = re.compile(r"(://[^/\s:@]+:)[^@\s]+@")


def mask(line: str) -> str:
    """Hide secret-looking values in a log line before it leaves the API."""
    line = _SECRET_KV.sub(lambda m: f"{m.group(1)}{m.group(2)}***", line)
    line = _BEARER.sub(lambda m: f"{m.group(1)} ***", line)
    return _URL_CRED.sub(r"\1***@", line)


def _clean(line: str) -> str:
    line = mask(line.strip())
    return line if len(line) <= _LINE_CAP else line[:_LINE_CAP - 1] + "…"


def _idle(reason: str) -> dict:
    return {"state": "idle", "reason": reason, "tag": "", "steps": [],
            "current_line": "", "error": ""}


def _step_key(text: str) -> str | None:
    for key, _label, prefix in KNOWN_STEPS:
        if prefix and text.startswith(prefix):
            return key
    if text.endswith(" is live"):
        return "live"
    return None


def parse_progress(text: str) -> dict:
    """Progress of the CURRENT attempt: the lines after the last start marker."""
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if _MARKER.match(ln):
            start = i
    if start is None:
        return _idle("the install log has no start marker")
    m = _MARKER.match(lines[start])
    tag = m.group(1)
    seen: dict[str, dict] = {}
    last_key = None
    error = ""
    current = ""
    for ln in lines[start + 1:]:
        raw = ln.strip()
        if not raw:
            continue
        if raw.startswith("@@ "):
            if last_key and raw[3:].strip().isdigit() and seen[last_key]["started_at"] is None:
                seen[last_key]["started_at"] = int(raw[3:].strip())
            continue
        current = raw
        if raw.startswith("==> "):
            key = _step_key(raw[4:])
            if key and key not in seen:
                seen[key] = {"key": key, "text": raw[4:], "started_at": None}
                last_key = key
        elif raw.startswith("!!") or raw.startswith("upgrade failed"):
            error = raw
    failed = bool(error)
    live = "live" in seen
    steps = []
    order = [k for k, _l, _p in KNOWN_STEPS]
    active = None
    if not failed and not live:
        present = [k for k in order if k in seen]
        active = present[-1] if present else None
    failed_key = None
    if failed:
        present = [k for k in order if k in seen]
        failed_key = present[-1] if present else None
    for key, label, _p in KNOWN_STEPS:
        if key == "worktree" and key not in seen:
            continue  # one-time step; only shown when the script printed it
        info = seen.get(key)
        if info is None:
            status = "pending"
        elif key == failed_key:
            status = "failed"
        elif key == active:
            status = "active"
        else:
            status = "done"
        steps.append({"key": key, "label": label, "status": status,
                      "started_at": info["started_at"] if info else None})
    state = "failed" if failed else "succeeded" if live else "running"
    return {"state": state, "reason": "", "tag": tag, "steps": steps,
            "current_line": _clean(current), "error": _clean(error) if error else ""}


def _read_tail(path: pathlib.Path) -> str:
    size = path.stat().st_size
    with path.open("rb") as f:
        f.seek(max(0, size - _TAIL_BYTES))
        return f.read().decode("utf-8", errors="replace")


def progress(*, paths: list[str] | None = None) -> dict:
    """Current install progress from the newest install log. Never raises."""
    cands = paths if paths is not None else [
        COMPOSE_LOG, str(pathlib.Path(NATIVE_ROOT) / NATIVE_LOG)]
    found = []
    for c in cands:
        p = pathlib.Path(c)
        try:
            if p.is_file():
                found.append((p.stat().st_mtime, p))
        except OSError:
            continue
    if not found:
        return _idle("no install log on this box")
    found.sort(key=lambda t: t[0], reverse=True)
    path = found[0][1]
    try:
        text = _read_tail(path)
    except OSError as e:
        return _idle(f"the install log is unreadable: {type(e).__name__}")
    out = parse_progress(text)
    out["log_path"] = str(path)
    return out
