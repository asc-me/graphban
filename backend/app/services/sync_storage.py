"""Sync storage: the three facts the Specs panel shows (GRPH-1320 / GRPH-P66).

1. **The folder path.** The path is the whole setup: a local folder, not an account. Pointing it
   at a Google Drive Desktop folder is how Drive is reached. Project `P` syncs
   `<folder>/<P>/PRDs`.
2. **The blob directory.** A local path holding immutable bytes keyed by sha256. The ledger row
   is the record; the directory only holds the bytes.
3. **The index.** Available, or unavailable with the reason. An unavailable index never hides
   the ledger rows.

A path the box cannot read is a failure on THAT fact; it does not clear the documents listed.
A file it cannot read (including a corrupt one) is a read failure, not a conflict, and never a
blank body. An approved PRD whose file does not match the ledger stays a conflict, and a failed
export does not roll the ledger back — both are `drive_sync`'s rules, which this module reads
through `drive_sync.classify` rather than restating.
"""
from __future__ import annotations

import os
from datetime import timezone

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import errors
from app.models import DocumentChunk, SyncState, SyncStorageConfig, utcnow
from app.services import drive_sync
from app.services import prds as prd_svc

SINGLETON_ID = "deployment"
MAX_PATH = 1024

#: Row sync state per `drive_sync.classify` action. What a run WOULD do, read without acting.
ROW_STATES = {
    "read_failed": "read_failed",
    "export_new": "not_exported",
    "adopt": "in_sync",
    "converged": "in_sync",
    "in_sync": "in_sync",
    "conflict": "conflict",
    "import_file": "file_changed",
    "export_ledger": "ledger_changed",
}
#: When the folder itself cannot be read, a row's sync state is not known. The row stays listed.
UNKNOWN = "unknown"


def _row(db: Session) -> SyncStorageConfig | None:
    return db.get(SyncStorageConfig, SINGLETON_ID)


def get_config(db: Session) -> SyncStorageConfig:
    row = _row(db)
    if row is None:
        row = SyncStorageConfig(id=SINGLETON_ID)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def folder_path(row: SyncStorageConfig | None) -> str:
    from app.config import settings

    return (row.folder_path if row else "") or settings.sync_dir


def blob_dir(row: SyncStorageConfig | None) -> str:
    from app.config import settings

    return (row.blob_dir if row else "") or os.path.join(settings.sync_dir, "blobs")


def project_root(row: SyncStorageConfig | None, project_id: str) -> str:
    return os.path.join(folder_path(row), project_id)


def blob_backend(db: Session | None = None) -> drive_sync.LocalFolderBackend:
    """The blob store `documents.put_blob` / `read_blob` use: the box's blob directory."""
    from app.config import settings

    row = _row(db) if db is not None else None
    return drive_sync.LocalFolderBackend(settings.sync_dir, blob_root=blob_dir(row))


def _validate(name: str, value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""  # empty: back to the default
    if len(value) > MAX_PATH or "\x00" in value:
        raise errors.Validation(f"{name} is not a usable path")
    if not os.path.isabs(value):
        raise errors.Validation(f"{name} must be an absolute path on this box",
                                hint=f"got {value!r}; for example /Users/me/Google Drive/Specs")
    return os.path.normpath(value)


def update_config(db: Session, *, folder_path: str | None = None,
                  blob_dir: str | None = None) -> SyncStorageConfig:
    """Save the box's sync configuration. A path the box cannot read is SAVED and then shown as
    a failure on its fact — the person may be about to mount it; refusing would hide that."""
    row = get_config(db)
    if folder_path is not None:
        row.folder_path = _validate("folder_path", folder_path)
    if blob_dir is not None:
        row.blob_dir = _validate("blob_dir", blob_dir)
    db.commit()
    db.refresh(row)
    return row


def path_fact(path: str, *, configured: bool) -> dict:
    """{path, configured, ok, writable, error}. `ok` means the box can read the directory."""
    error = ""
    if not os.path.exists(path):
        error = "does not exist on this box"
    elif not os.path.isdir(path):
        error = "is not a directory"
    elif not os.access(path, os.R_OK | os.X_OK):
        error = "the box cannot read it (permission denied)"
    else:
        try:
            os.listdir(path)
        except OSError as e:
            error = f"the box cannot read it ({e.strerror or e})"
    ok = not error
    return {"path": path, "configured": configured, "ok": ok,
            "writable": ok and os.access(path, os.W_OK), "error": error}


def index_fact(db: Session, project_id: str) -> dict:
    """The document index: available, or unavailable with the reason."""
    from app.config import settings

    try:
        chunks = db.execute(select(func.count()).select_from(DocumentChunk).where(
            DocumentChunk.project_id == project_id)).scalar_one()
    except SQLAlchemyError as e:
        db.rollback()
        return {"available": False, "reason": f"the index cannot be read: {type(e).__name__}",
                "chunks": None, "mode": None}
    mode = "lexical" if settings.embed_provider == "stub" else "vector"
    return {"available": True, "reason": "", "chunks": chunks, "mode": mode}


def _scan(db: Session, project_id: str, root: str) -> tuple[list[dict], dict]:
    """(rows, preview report) — what a run would do, computed read-only."""
    prds = prd_svc.list_prds(db, project_id=project_id)
    files = drive_sync.LocalFolderBackend(root).list(drive_sync.PRDS_SUBDIR)
    by_id: dict[str, dict] = {}
    unreadable: dict[str, dict] = {}
    for f in files:
        if f.get("content") is None:
            unreadable[f["name"]] = f
            continue
        fid = drive_sync.parse_front_matter_id(f["content"])
        if fid:
            by_id[fid] = f
    states = {s.prd_id: s for s in db.scalars(
        select(SyncState).where(SyncState.prd_id.in_([p.id for p in prds])))}
    report = _empty_report(root)
    report["read_failures"] = [
        {"file_name": n, "path": f"{drive_sync.PRDS_SUBDIR}/{n}", "error": f.get("error") or "",
         "prd_id": None} for n, f in unreadable.items()]
    rows = []
    for prd in prds:
        st, f = states.get(prd.id), by_id.get(prd.id)
        action, db_hash, file_hash = drive_sync.classify(
            prd, st, f, unreadable_names=set(unreadable))
        file_name = (f or {}).get("name") or (st.file_name if st else "")
        row = {"prd_id": prd.id, "title": prd.title, "status": prd.status,
               "sync_state": ROW_STATES[action], "file_name": file_name, "error": ""}
        if action == "conflict":
            report["conflicts"].append(prd.id)
            report["conflict_details"].append({
                "prd_id": prd.id, "status": prd.status, "ledger_hash": db_hash,
                "file_hash": file_hash, "file_name": f["name"],
                "byte_length": len(f["content"].encode("utf-8")),
                "path": f"{drive_sync.PRDS_SUBDIR}/{f['name']}"})
        elif action == "read_failed":
            for rf in report["read_failures"]:
                if rf["file_name"] == file_name:
                    rf["prd_id"] = prd.id
                    row["error"] = rf["error"]
        elif ROW_STATES[action] == "in_sync":
            report["in_sync"] += 1
        rows.append(row)
    return rows, report


def _empty_report(root: str) -> dict:
    return {"folder": root, "prds_dir": os.path.join(root, drive_sync.PRDS_SUBDIR),
            "exported": [], "imported": [], "updated_db": [], "updated_file": [],
            "conflicts": [], "conflict_details": [], "export_status": "ok", "in_sync": 0,
            "read_failures": []}


def _iso(dt) -> str | None:
    """SQLite hands back a naive datetime; it was written as UTC, so say so."""
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()


def status(db: Session, project_id: str) -> dict:
    """`frozen_status` with every PRD id shown as the rendered key (the project's current tag
    plus the PRD number), the same key GET /api/prds/{id} returns. The frozen id is absent."""
    out = frozen_status(db, project_id)
    keys = {p.id: p.key for p in prd_svc.list_prds(db, project_id=project_id)}

    def k(pid):
        return keys.get(pid, pid) if pid else pid

    out["rows"] = [{**r, "prd_id": k(r["prd_id"])} for r in out["rows"]]
    rep = out["report"]
    if rep is not None:
        out["report"] = {
            **rep,
            "conflicts": [k(i) for i in rep["conflicts"]],
            "conflict_details": [{**d, "prd_id": k(d["prd_id"])} for d in rep["conflict_details"]],
            "read_failures": [{**f, "prd_id": k(f["prd_id"])} for f in rep["read_failures"]],
        }
    return out


def frozen_status(db: Session, project_id: str) -> dict:
    """The three facts, the row sync states, and a preview report, keyed on the frozen PRD id
    (the join key for sync state). Writes nothing (beyond creating the empty config row on
    first read)."""
    row = get_config(db)
    folder = path_fact(folder_path(row), configured=bool(row.folder_path))
    root = project_root(row, project_id)
    folder["project_path"] = root
    blobs = path_fact(blob_dir(row), configured=bool(row.blob_dir))
    if folder["ok"]:
        try:
            rows, report = _scan(db, project_id, root)
        except OSError as e:
            folder.update(ok=False, error=f"the box cannot read {root} ({e.strerror or e})")
            rows, report = None, None
    else:
        rows, report = None, None
    if rows is None:
        # The folder fact failed: the documents stay listed, their sync state is not known.
        rows = [{"prd_id": p.id, "title": p.title, "status": p.status, "sync_state": UNKNOWN,
                 "file_name": "", "error": ""} for p in prd_svc.list_prds(db, project_id=project_id)]
    return {
        "folder": folder,
        "blob_dir": blobs,
        "index": index_fact(db, project_id),
        "rows": rows,
        "report": report,
        "last_run": {
            "at": _iso(row.last_run_at),
            "export_status": row.last_export_status or None,
            "error": row.last_error,
        },
    }


def run(db: Session, project_id: str) -> dict:
    """Run sync for this project, then refresh the three facts and the row sync states.

    An unreadable folder is reported on the folder fact and nothing is written. A failed export
    is `export_status: failed` on the report; the ledger rows stay as they were."""
    row = get_config(db)
    folder = path_fact(folder_path(row), configured=bool(row.folder_path))
    result: dict | None = None
    if not folder["ok"]:
        # Nothing was read or written: a folder fact failure, not an export failure.
        error = f"{folder['path']} {folder['error']}"
        export_status = "not_run"
    else:
        try:
            result = drive_sync.sync(db, project_id, root_dir=project_root(row, project_id))
            result = {**_empty_report(project_root(row, project_id)), **result}
            export_status, error = result["export_status"], ""
        except OSError as e:
            db.rollback()
            export_status, error = "failed", f"{type(e).__name__}: {e.strerror or e}"
    row = get_config(db)
    row.last_run_at = utcnow()
    row.last_export_status = export_status
    row.last_error = error[:400]
    db.commit()
    return {**status(db, project_id), "run": result}
