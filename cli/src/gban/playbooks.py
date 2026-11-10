"""The hourly poll that refreshes the agent files from the deployment (GRPH-P65).

Every `gban` command may poll `/health`; `gban update` always does. The receipt
`.graphban/playbooks.json` (gitignored) holds `polled_at` (Unix seconds), the server URL, the
`git_sha` last applied, and the sha256 of every file this tool wrote.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from gban.client import Client, Refused, Unreachable

RECEIPT = Path(".graphban") / "playbooks.json"
INTERVAL = 3600


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


def poll(url: str, repo: Path, *, force: bool = False, client: Client | None = None,
         now: float | None = None) -> dict:
    """One poll decision. Returns `{"polled": bool, "note": str, ...}`; never raises for an
    unreachable or unreadable `/health` (that leaves `polled_at` alone)."""
    now = time.time() if now is None else now
    url = url.rstrip("/")
    receipt = _read(repo)
    polled_at = receipt.get("polled_at")
    if (not force and isinstance(polled_at, (int, float)) and receipt.get("server") == url
            and 0 <= now - polled_at < INTERVAL):
        return {"polled": False, "polled_at": polled_at, "git_sha": receipt.get("git_sha", ""),
                "note": f"last poll {when(polled_at)}; not checked again within the hour"}
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
        pack = client.call("GET", "/api/playbooks")
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
    plan, kept = [], []
    owned = dict(base.get("files", {}))
    for entry in files:
        rel, body = entry.get("path"), entry.get("body")
        if not isinstance(rel, str) or not isinstance(body, str) or rel.startswith(("/", "..")) \
                or ".." in Path(rel).parts:
            return {"polled": False, "failed": True, "note": "unreadable pack; wrote nothing"}
        data = body.encode("utf-8")
        plan.append((rel, data))
    written = []
    for rel, data in plan:
        dest, digest = repo / rel, _sha(data)
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            written.append(rel)
        else:
            have = _sha(dest.read_bytes())
            if have == digest:
                pass
            elif owned.get(rel) == have:
                dest.write_bytes(data)
                written.append(rel)
            else:
                kept.append(rel)
                continue
        owned[rel] = digest
    gone = [r for r in owned if r not in {p for p, _ in plan}]
    new.update(git_sha=sha, files=owned)
    _save(repo, new)
    note = f"applied {sha[:12]}: {len(written)} file(s) written"
    if kept:
        note += "; local edits left in place: " + ", ".join(kept)
    if gone:
        note += "; no longer in the pack: " + ", ".join(gone)
    return {"polled": True, "polled_at": now, "git_sha": sha, "note": note}
