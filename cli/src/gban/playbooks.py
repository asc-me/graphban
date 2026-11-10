"""The hourly poll that refreshes the agent files from the deployment (GRPH-P65).

Every `gban` command may poll `/health`; `gban update` always does. The receipt
`.graphban/playbooks.json` (gitignored) holds `polled_at` (Unix seconds), the server URL, the
`git_sha` last applied, and the sha256 of every file this tool wrote.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess  # nosec B404
import time
from collections.abc import Callable
from pathlib import Path

from gban import config
from gban.client import Client, NoSession, Refused, Unreachable, authenticated

RECEIPT = Path(".graphban") / "playbooks.json"
INTERVAL = 3600
#: A `skill` entry is published once, under `.claude/skills/<slug>/SKILL.md`; every harness
#: reads its own directory (`gbagent` reads all three), so the client writes it to each.
SKILL_DIRS = (".claude/skills", ".grok/skills", ".agents/skills")


def targets(entry: dict) -> list[str]:
    """Every checkout path one pack entry is written to."""
    rel = entry.get("path")
    if entry.get("kind") == "skill" and isinstance(rel, str):
        for base in SKILL_DIRS:
            if rel.startswith(base + "/"):
                tail = rel[len(base) + 1:]
                return [f"{b}/{tail}" for b in SKILL_DIRS]
    return [rel]


def _write(dest: Path, data: bytes) -> None:
    """Whole file or nothing: a `gbagent` child spawned mid-apply never reads half a body."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".gban-tmp")
    tmp.write_bytes(data)
    os.replace(tmp, dest)


def _read(repo: Path) -> dict:
    try:
        loaded = json.loads((repo / RECEIPT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _save(repo: Path, receipt: dict) -> None:
    path = repo / RECEIPT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(ts))


def checkout(start: Path) -> Path | None:
    """The git toplevel containing `start`, or None outside a checkout.

    The receipt and every pack path are relative to the checkout root, so running `gban`
    from `cli/` must not plant `.graphban/` and the skills under `cli/`.
    """
    try:
        proc = subprocess.run(  # nosec B603 B607
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    top = (proc.stdout or "").strip()
    return Path(top) if proc.returncode == 0 and top else None


def credentialed(url: str, project: str = "") -> Client:
    """A client that may read `/api/playbooks`: the `gban login` session, else the API key
    (`$GRAPHBAN_API_KEY`, then the key `gban setup` stored for this project).

    Raises `NoSession` when there is none. `/health` stays unauthenticated; only the pack,
    which carries the bodies, needs this.
    """
    try:
        return authenticated(url, act="update")
    except NoSession:
        key = (os.environ.get(config.API_KEY_ENV) or "").strip() \
            or config.stored_supervisor_key(project)
        if not key:
            raise
        return Client(url, api_key=key)


def poll(url: str, repo: Path, *, force: bool = False, client: Client | None = None,
         auth: Callable[[], Client] | None = None, project: str = "",
         now: float | None = None) -> dict:
    """One poll decision. Returns `{"polled": bool, "note": str, ...}`; never raises for an
    unreachable or unreadable `/health` (that leaves `polled_at` alone).

    `client` reads `/health`, which needs no credential. `auth` builds the client for the
    pack, which does: by default the `gban login` session or the stored API key.
    """
    now = time.time() if now is None else now
    url = url.rstrip("/")
    receipt = _read(repo)
    polled_at = receipt.get("polled_at")
    if (not force and isinstance(polled_at, (int, float)) and receipt.get("server") == url
            and 0 <= now - polled_at < INTERVAL):
        stored = receipt.get("git_sha") or ""
        at = f" at {stored[:12]}" if stored else (" (no pack applied)")
        return {"polled": False, "skipped": True, "polled_at": polled_at, "git_sha": stored,
                "note": f"last poll {when(polled_at)}{at}; not checked against the server "
                        "again within the hour"}
    if auth is None:
        auth = lambda: credentialed(url, project)  # noqa: E731
    client = client or Client(url)
    try:
        health = client.call("GET", "/health")
    except (Unreachable, Refused) as exc:
        return {"polled": False, "failed": True, "note": f"could not poll /health: {exc}"}
    if not isinstance(health, dict):
        return {"polled": False, "failed": True, "note": "unreadable /health"}
    sha = str(health.get("git_sha") or "").strip()
    # A different server never inherits the old server's applied sha or file hashes.
    base = receipt if receipt.get("server") == url else {}
    new = {"server": url, "polled_at": now, "git_sha": base.get("git_sha", ""),
           "files": base.get("files", {})}
    if not sha:
        _save(repo, new)
        return {"polled": True, "polled_at": now, "note": "deployment is unstamped (empty git_sha)"}
    if sha == base.get("git_sha"):
        _save(repo, new)
        return {"polled": True, "polled_at": now, "git_sha": sha, "note": f"current at {sha[:12]}"}
    try:
        pack = auth().call("GET", "/api/playbooks")
    except NoSession:
        return {"polled": False, "failed": True,
                "note": "no credential to fetch the pack; run `gban login` or `gban setup`"}
    except Refused as exc:
        if exc.status == 404:
            new["no_pack"] = True
            _save(repo, new)
            return {"polled": True, "polled_at": now,
                    "note": "this server publishes no pack; setup keeps the wheel copy"}
        return {"polled": False, "failed": True, "note": f"could not fetch pack: {exc}"}
    except Unreachable as exc:
        return {"polled": False, "failed": True, "note": f"could not fetch pack: {exc}"}
    files = pack.get("files") if isinstance(pack, dict) else None
    if not isinstance(files, list) or pack.get("git_sha") != sha:
        return {"polled": False, "failed": True,
                "note": "deployment identity is split or the pack is unreadable; wrote nothing"}
    # Validate the whole pack before touching a file: a partial or unreadable payload
    # writes nothing.
    plan: dict[str, bytes] = {}
    for entry in files:
        if not isinstance(entry, dict):
            return {"polled": False, "failed": True, "note": "unreadable pack; wrote nothing"}
        body = entry.get("body")
        if not isinstance(body, str):
            return {"polled": False, "failed": True, "note": "unreadable pack; wrote nothing"}
        for rel in targets(entry):
            if not isinstance(rel, str) or not rel or rel.startswith(("/", "\\")) \
                    or ".." in Path(rel).parts or Path(rel).is_absolute():
                return {"polled": False, "failed": True,
                        "note": "unreadable pack; wrote nothing"}
            plan[rel] = body.encode("utf-8")
    applied = apply(repo, plan, dict(base.get("files", {})))
    owned, written, kept = applied["files"], applied["written"], applied["kept"]
    gone = applied["gone"]
    new.update(git_sha=sha, files=owned)
    _save(repo, new)
    note = f"applied {sha[:12]}: {len(written)} file(s) written"
    if kept:
        note += "; local edits left in place: " + ", ".join(kept)
    if gone:
        note += "; no longer in the pack: " + ", ".join(gone)
    return {"polled": True, "polled_at": now, "git_sha": sha, "note": note,
            "written": written, "kept": kept, "gone": gone}


def apply(repo: Path, plan: dict[str, bytes], owned: dict[str, str]) -> dict:
    """Write `plan` (path -> bytes) into `repo` by the ownership rules of GRPH-P65.

    - receipt says we wrote it and the new body differs: replace it;
    - bytes already match: leave the file, record the hash (this adopts a committed copy);
    - missing: write it, record the hash;
    - matches neither the receipt nor the pack: a local edit, left and named;
    - in the receipt but no longer in the pack: left on disk and named.
    """
    owned = dict(owned)
    written, kept = [], []
    for rel, data in plan.items():
        dest, digest = repo / rel, _sha(data)
        if dest.is_file():
            have = _sha(dest.read_bytes())
            if have != digest:
                if owned.get(rel) != have:
                    kept.append(rel)
                    continue
                _write(dest, data)
                written.append(rel)
        elif dest.exists():  # a directory where a file belongs: not ours to remove
            kept.append(rel)
            continue
        else:
            _write(dest, data)
            written.append(rel)
        owned[rel] = digest
    gone = sorted(r for r in owned if r not in plan)
    return {"files": owned, "written": written, "kept": kept, "gone": gone}
