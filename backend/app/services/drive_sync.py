"""Two-way sync between PRDs and markdown files (AL-01).

Backend-agnostic sync engine + a filesystem backend. Point the sync directory at a Google
Drive Desktop folder and PRDs reach Drive with no OAuth. A native Drive-API backend can
implement the same `SyncBackend` interface later.

Conflict-safe: a per-PRD last-synced hash (SyncState) lets us tell which side changed. If
both changed since the last sync, we flag a conflict rather than clobbering either.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import SyncState, utcnow
from app.services import prds as prd_svc

PRDS_SUBDIR = "PRDs"


# ---- Backend interface + filesystem implementation ----

class SyncBackend:
    def list(self, subdir: str) -> list[dict]:  # [{"name", "content"}]
        raise NotImplementedError

    def write(self, subdir: str, name: str, content: str) -> None:
        raise NotImplementedError

    def delete(self, subdir: str, name: str) -> None:
        raise NotImplementedError


class LocalFolderBackend(SyncBackend):
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)

    def _dir(self, subdir: str) -> Path:
        d = self.root / subdir
        d.mkdir(parents=True, exist_ok=True)
        return d

    def list(self, subdir: str) -> list[dict]:
        d = self._dir(subdir)
        out = []
        for p in sorted(d.glob("*.md")):
            out.append({"name": p.name, "content": p.read_text(encoding="utf-8")})
        return out

    def write(self, subdir: str, name: str, content: str) -> None:
        (self._dir(subdir) / name).write_text(content, encoding="utf-8")

    def delete(self, subdir: str, name: str) -> None:
        p = self._dir(subdir) / name
        if p.exists():
            p.unlink()


# ---- Front-matter helpers ----

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _hash(body: str) -> str:
    return hashlib.sha256(body.strip().encode("utf-8")).hexdigest()


def parse_front_matter_id(content: str) -> str | None:
    m = _FM_RE.match(content)
    if not m:
        return None
    fm = m.group(1)
    idm = re.search(r"graphban_id:\s*(\S+)", fm)
    return idm.group(1) if idm else None


def strip_front_matter(content: str) -> str:
    return _FM_RE.sub("", content, count=1).lstrip("\n")


def extract_title(body: str) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def render(prd) -> str:
    """PRD → file content with an Graphban front-matter block."""
    fm = (
        "---\n"
        f"graphban_id: {prd.id}\n"
        f"title: {prd.title}\n"
        f"status: {prd.status}\n"
        f"version: {prd.version}\n"
        "---\n"
    )
    return fm + prd.body


def _safe_name(prd) -> str:
    safe = re.sub(r"[^\w \-]", "", prd.title).strip()[:60] or prd.id
    return f"{prd.id} — {safe}.md"


# ---- The sync engine ----

def sync(db: Session, project_id: str, root_dir: str | os.PathLike, backend: SyncBackend | None = None) -> dict:
    backend = backend or LocalFolderBackend(root_dir)
    report = {"exported": [], "imported": [], "updated_db": [], "updated_file": [], "conflicts": [], "in_sync": 0,
              "conflict_details": [], "export_status": "ok"}

    prds = prd_svc.list_prds(db, project_id=project_id)
    files = backend.list(PRDS_SUBDIR)
    files_by_id: dict[str, dict] = {}
    unmatched = []
    for f in files:
        fid = parse_front_matter_id(f["content"])
        (files_by_id.__setitem__(fid, f) if fid else unmatched.append(f))

    states = {s.prd_id: s for s in db.query(SyncState).filter(SyncState.prd_id.in_([p.id for p in prds])).all()}

    def save_state(prd_id: str, name: str, h: str) -> None:
        st = states.get(prd_id) or SyncState(prd_id=prd_id)
        st.file_name, st.last_hash, st.updated_at = name, h, utcnow()
        db.add(st)
        states[prd_id] = st

    def safe_write(name: str, content: str) -> bool:
        """Export is best-effort: a failed write leaves the ledger row intact (never rolls back)."""
        try:
            backend.write(PRDS_SUBDIR, name, content)
            return True
        except Exception:
            report["export_status"] = "failed"
            return False

    def conflict(prd, f: dict, db_hash: str, file_hash: str) -> None:
        # `conflicts` stays the PRD id list the web reads; the detail carries hashes, name,
        # size and path only: the file is already at that path and the ledger body is
        # get_prd, so neither body is carried.
        report["conflicts"].append(prd.id)
        report["conflict_details"].append({
            "prd_id": prd.id, "status": prd.status, "ledger_hash": db_hash, "file_hash": file_hash,
            "file_name": f["name"], "byte_length": len(f["content"].encode("utf-8")),
            "path": f"{PRDS_SUBDIR}/{f['name']}"})

    # 1. Reconcile each existing PRD with its file.
    for prd in prds:
        st = states.get(prd.id)
        f = files_by_id.get(prd.id)
        db_body = prd.body
        db_hash = _hash(db_body)
        name = st.file_name if st and st.file_name else _safe_name(prd)

        if f is None:
            if safe_write(name, render(prd)):
                save_state(prd.id, name, db_hash)
                report["exported"].append(prd.id)
            continue

        file_body = strip_front_matter(f["content"])
        file_hash = _hash(file_body)
        last = st.last_hash if st else None

        if last is None:
            # First time we see this file for this PRD: adopt if identical, else flag.
            if file_hash == db_hash:
                save_state(prd.id, f["name"], db_hash)
                report["in_sync"] += 1
            else:
                conflict(prd, f, db_hash, file_hash)
            continue

        db_changed = db_hash != last
        file_changed = file_hash != last
        if db_changed and file_changed:
            if file_hash == db_hash:
                save_state(prd.id, f["name"], db_hash)
                report["in_sync"] += 1
            else:
                conflict(prd, f, db_hash, file_hash)  # both moved → don't clobber
        elif file_changed:
            if prd.status not in ("draft", "review") or prd.pending_rebaseline:
                # Drive is an export and draft-import channel, not the baseline.
                conflict(prd, f, db_hash, file_hash)
                continue
            prd_svc.update_prd(db, prd.id, body=file_body)
            prd_svc.create_version(db, prd.id, note="Body came from the Drive folder (file edit).")
            save_state(prd.id, f["name"], file_hash)
            report["updated_db"].append(prd.id)
        elif db_changed:
            if safe_write(name, render(prd)):
                save_state(prd.id, name, db_hash)
                report["updated_file"].append(prd.id)
        else:
            report["in_sync"] += 1

    # 2. Unmatched files → new PRDs (import). Write the id back into the same file.
    for f in unmatched:
        body = strip_front_matter(f["content"])
        title = extract_title(body) or f["name"].rsplit(".", 1)[0]
        prd = prd_svc.create_prd(db, title=title, project_id=project_id, body=body)
        safe_write(f["name"], render(prd))
        save_state(prd.id, f["name"], _hash(body))
        report["imported"].append(prd.id)

    db.commit()
    return report
