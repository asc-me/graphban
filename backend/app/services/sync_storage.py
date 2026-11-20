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
from app.models import DocumentChunk, Organization, Project, SyncState, SyncStorageConfig, utcnow
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
    out["destination"] = destination_fact(db, editable=False)
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


# ---- publish destination (GRPH-1761) -------------------------------------------------------
# The console stores a URL and a branch. The box pulls them. Nothing here shells out to git,
# and nothing here turns publishing on — a link, an unlink, or a changed pair turns it off.

_URL_MAX = 1024
_BRANCH_MAX = 255
_REASON_MAX = 400


class DestinationOwned(Exception):
    """This box is linked. The org admin owns the destination; a local save is refused."""


def _clip(value: str, limit: int, name: str) -> str:
    value = (value or "").strip()
    if "\x00" in value or len(value) > limit:
        raise errors.Validation(f"{name} is not usable")
    return value


def _branch(value: str) -> str:
    """A branch is whatever the admin typed. Empty is allowed and means unconfigured.
    Nothing substitutes `main`."""
    return _clip(value, _BRANCH_MAX, "branch")


def _repo_path(value: str) -> str:
    """Empty is unconfigured, not the sync-folder default. A non-empty path must be absolute.
    It is saved even when this box cannot read it."""
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) > MAX_PATH or "\x00" in value:
        raise errors.Validation("path is not a usable path")
    if not os.path.isabs(value):
        raise errors.Validation("path must be an absolute path on this box",
                                hint=f"got {value!r}")
    return os.path.normpath(value)


def is_linked(db: Session) -> bool:
    from app.services import code_sync

    return bool(code_sync.link_status(db).get("linked"))


def _match(db: Session, url: str, *, org_id: str | None = None) -> dict:
    """Exact string equality with `Project.repo_url`. `checked` is always true: an empty
    project list means this looked and nothing matched, not that nobody looked."""
    projects: list[dict] = []
    if url:
        q = select(Project).where(Project.repo_url == url)
        if org_id is not None:
            q = q.where(Project.org_id == org_id)
        projects = [{"project_id": p.id, "repo_url": p.repo_url} for p in db.scalars(q)]
    return {"checked": True, "projects": projects}


def destination_fact(db: Session, *, editable: bool = False) -> dict:
    """The box's destination. `state` is configured only when the active value and the branch
    are both set. `pull.state` null means this box has not pulled. `previous` null means there
    is no earlier path — not that a path was cleared."""
    row = get_config(db)
    source = row.destination_source or ""
    linked = is_linked(db)
    if source == "org":
        active, branch = row.received_url, row.received_branch
        url, path = active, ""
    elif source == "local":
        active, branch = row.repo_path, row.repo_branch
        url, path = "", active
    else:
        active, branch = "", ""
        url, path = "", ""
    return {
        "state": "configured" if active and branch else "unconfigured",
        "source": source or None,
        "url": url,
        "path": path,
        "branch": branch if source else "",
        "previous": {"path": row.previous_path} if row.previous_path else None,
        "editable": bool(editable) and not linked,
        "publishing": bool(row.publish_enabled),
        # Whether a credential is set, and nothing else: never the path, never the bytes.
        "credential": {"set": credential_set(row)},
        "pull": {
            "state": row.destination_pull_state or None,
            "reason": row.destination_pull_reason or "",
        },
        "code_remote_match": _match(db, url if source == "org" else ""),
    }


def org_destination(db: Session, org: Organization) -> dict:
    """What the org admin saved. No publishing flag: the switch lives on the box."""
    url = org.publish_repo_url or ""
    branch = org.publish_branch or ""
    return {
        "url": url,
        "branch": branch,
        "state": "configured" if url and branch else "unconfigured",
        "code_remote_match": _match(db, url, org_id=org.id),
    }


def save_org(db: Session, org: Organization, *, url: str | None = None,
             branch: str | None = None) -> dict:
    """Store the org's URL and branch. Any non-empty string is allowed, including a value
    equal to a project's `repo_url` (`github.com/acme/app`). Empty is unconfigured."""
    if url is not None:
        org.publish_repo_url = _clip(url, _URL_MAX, "url")
    if branch is not None:
        org.publish_branch = _branch(branch)
    db.commit()
    db.refresh(org)
    return org_destination(db, org)


def save_local(db: Session, *, path: str, branch: str) -> dict:
    """The unlinked box's path and branch. Refused, with the stored pair left as it is, when
    the box is linked."""
    if is_linked(db):
        raise DestinationOwned("the org admin owns the destination")
    path = _repo_path(path)
    branch = _branch(branch)
    row = get_config(db)
    changed = (path != row.repo_path or branch != row.repo_branch
               or row.destination_source == "org")
    if path != row.repo_path and row.repo_path:
        row.previous_path = row.repo_path
    row.repo_path = path
    row.repo_branch = branch
    row.received_url = ""
    row.received_branch = ""
    row.destination_source = "local" if (path or branch) else ""
    if changed:
        row.publish_enabled = False
    db.commit()
    return destination_fact(db, editable=True)


def note_link(db: Session) -> None:
    """A link arrived. Publishing goes off even when the pull that follows gets no answer.
    The path stays until a pull actually adopts the org's pair."""
    row = get_config(db)
    row.publish_enabled = False
    db.commit()


def note_unlink(db: Session) -> None:
    """An unlink arrived. A pair that came from the org is cleared and the previous path is
    not put back. A pair that was only ever local stays, and publishing is off either way.
    Calling this twice leaves the same row."""
    row = get_config(db)
    if row.destination_source == "org":
        row.received_url = ""
        row.received_branch = ""
        row.destination_source = ""
        row.repo_path = ""
        row.repo_branch = ""
    else:
        row.received_url = ""
        row.received_branch = ""
    row.publish_enabled = False
    db.commit()


def _adopt(row: SyncStorageConfig, url: str, branch: str) -> None:
    """Apply a destination the console actually answered with. The same pair again does not
    turn publishing off. The first adopt from a local path stashes that path and stops using it."""
    url = (url or "").strip()
    branch = (branch or "").strip()
    if (len(url) > _URL_MAX or len(branch) > _BRANCH_MAX or "\x00" in url or "\x00" in branch):
        row.destination_pull_state = "refused"
        row.destination_pull_reason = "the org destination is not usable"
        return
    changed_source = row.destination_source != "org"
    changed_value = row.received_url != url or row.received_branch != branch
    if changed_source and row.repo_path:
        row.previous_path = row.repo_path
    if changed_source:
        row.repo_path = ""
        row.repo_branch = ""
    row.received_url = url
    row.received_branch = branch
    row.destination_source = "org"
    if changed_source or changed_value:
        row.publish_enabled = False
    row.destination_pull_state = "ok"
    row.destination_pull_reason = ""


def _record_pull(db: Session, state: str, reason: str) -> None:
    """Write only the pull outcome. The destination and the publishing flag stay as they were."""
    row = get_config(db)
    row.destination_pull_state = state
    row.destination_pull_reason = (reason or "")[:_REASON_MAX]
    db.commit()


def fetch_org_destination(base_url: str, api_key: str):
    """GET the org's destination. Tests replace this; the box never pushes a destination."""
    import httpx

    return httpx.get(
        f"{base_url.rstrip('/')}/api/sync/destination",
        headers={"X-API-Key": api_key},
        timeout=30.0,
    )


def _credentials(db: Session, cloud_url: str, api_key: str) -> tuple[str, str] | None:
    if cloud_url and api_key:
        return cloud_url, api_key
    from app.services import code_sync

    try:
        return code_sync._target(db, "", "")
    except code_sync.NotLinked:
        return None


def pull_destination(db: Session, *, cloud_url: str = "", api_key: str = "",
                     fetch=None) -> dict:
    """Pull the org's URL and branch. No answer (network, timeout, 5xx, 408) records
    `unknown` and leaves the destination and publishing flag untouched. A 4xx is `refused`:
    an answer, and not "the org cleared the repo". A body with neither key is not a
    destination. Missing keys on a real destination are empty, never `main`."""
    creds = _credentials(db, cloud_url, api_key)
    if creds is None:
        return destination_fact(db, editable=False)
    url, key = creds
    fetch = fetch or fetch_org_destination
    try:
        resp = fetch(url, key)
    except Exception as e:  # network, timeout, HTTPError, or a transport that raised
        # Any failure to obtain a document is "no answer". It must not clear the pair.
        _record_pull(db, "unknown", f"{type(e).__name__}: {e}")
        return destination_fact(db, editable=False)
    status = getattr(resp, "status_code", None) or 0
    if status >= 500 or status in (408, 425, 429) or status <= 0:
        _record_pull(db, "unknown", f"the console answered {status}" if status else
                     "the console gave no answer")
        return destination_fact(db, editable=False)
    if status >= 400:
        _record_pull(db, "refused", f"the console answered {status}")
        return destination_fact(db, editable=False)
    try:
        data = resp.json()
    except (ValueError, TypeError) as e:
        _record_pull(db, "unknown", f"the console answered without a destination: {type(e).__name__}")
        return destination_fact(db, editable=False)
    if not isinstance(data, dict) or ("url" not in data and "branch" not in data):
        _record_pull(db, "unknown", "the console answered without a destination")
        return destination_fact(db, editable=False)
    row = get_config(db)
    _adopt(row, str(data.get("url") or ""), str(data.get("branch") or ""))
    db.commit()
    return destination_fact(db, editable=False)


def publish_target(db: Session) -> dict | None:
    """Where the next publish would write. None when either half is missing, so an
    unconfigured destination does not fall back to the previous path."""
    row = get_config(db)
    source = row.destination_source or ""
    if source == "org" and row.received_url and row.received_branch:
        return {"kind": "remote", "url": row.received_url, "branch": row.received_branch}
    if source == "local" and row.repo_path and row.repo_branch:
        return {"kind": "path", "path": row.repo_path, "branch": row.repo_branch}
    return None


# ---- opt in (GRPH-1762) ---------------------------------------------------------------------
# Publishing is a switch on this deployment, default off, turned on only by a box admin (the
# router checks who; this module is principal-free). Turning it on is refused, and the switch
# stays off, while the destination or the credential is missing. A destination change turns it
# off (`save_local`, `_adopt`, `note_link`, `note_unlink`) and leaves the credential in place.

CREDENTIAL_FILE = "file"
CREDENTIAL_GIT = "git"
REDACTED = "[credential]"


class PublishRefused(Exception):
    """Turning publishing on was refused. `field` names what is missing; the message never
    carries the credential path or its bytes."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field


def credential_set(row: SyncStorageConfig) -> bool:
    mode = row.credential_mode or ""
    return mode == CREDENTIAL_GIT or (mode == CREDENTIAL_FILE and bool(row.credential_path))


def save_credential(db: Session, *, path: str | None) -> dict:
    """Set the credential: a path to a file on this box, or empty to use the credential the
    box's git installation already has for the remote. `None` clears it (not set). The file
    is not read here — the push reads it, before it fetches — so an unreadable file is saved.
    A credential change does not turn publishing on or off."""
    row = get_config(db)
    if path is None:
        row.credential_mode, row.credential_path = "", ""
    else:
        value = (path or "").strip()
        if not value:
            row.credential_mode, row.credential_path = CREDENTIAL_GIT, ""
        else:
            if len(value) > MAX_PATH or "\x00" in value or not os.path.isabs(value):
                # The value is not echoed: it may be the secret itself pasted in the wrong box.
                raise errors.Validation("credential must be an absolute path to a file on this box")
            row.credential_mode, row.credential_path = CREDENTIAL_FILE, os.path.normpath(value)
    db.commit()
    return destination_fact(db, editable=True)


def _missing_destination(db: Session) -> str | None:
    """The first empty destination field, by its name on the panel, or None."""
    row = get_config(db)
    source = row.destination_source or ""
    if source == "org":
        if not row.received_url:
            return "url"
        return None if row.received_branch else "branch"
    if source == "local":
        if not row.repo_path:
            return "path"
        return None if row.repo_branch else "branch"
    # Nothing saved yet: a linked box is waiting on the org's URL, an unlinked one on its path.
    return "url" if is_linked(db) else "path"


def redact(db: Session, text: str) -> str:
    """`text` with the credential path and the credential file's bytes removed. Every publish
    error goes through this before it is stored or answered. A file this box cannot read has
    no bytes to leak; its path is still removed."""
    text = str(text or "")
    row = get_config(db)
    if (row.credential_mode or "") != CREDENTIAL_FILE or not row.credential_path:
        return text
    secrets: list[str] = [row.credential_path]
    try:
        with open(row.credential_path, "rb") as fh:
            raw = fh.read(65536)
    except OSError:
        raw = b""
    if raw:
        decoded = raw.decode("utf-8", "replace")
        secrets.append(decoded)
        secrets.extend(line.strip() for line in decoded.splitlines())
    # Longest first, so the whole file goes before any one line of it.
    for secret in sorted({s for s in secrets if len(s) >= 4}, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return text


def _push_now(db: Session) -> dict:
    """One push of the current earned set. The push itself is The push (GRPH-1764,
    `services/publication_push.py`); until that module is on this deployment the answer is
    `not_run` with that reason — an absence, never a published result."""
    try:
        from app.services import publication_push  # type: ignore[attr-defined]
    except ImportError:
        return {"result": "not_run", "reason": "this deployment has no push yet"}
    return publication_push.push(db)


def set_publishing(db: Session, *, enabled: bool, push=None) -> dict:
    """Turn publishing on or off. On is refused — the switch stays off — when a destination
    field or the credential is empty; the refusal names that field. On, once accepted, runs
    one push of the current earned set; a push failure leaves the switch on and every ledger
    row as it was, and its reason is redacted. Off never pushes. On while already on pushes
    nothing."""
    row = get_config(db)
    if not enabled:
        row.publish_enabled = False
        db.commit()
        return {**destination_fact(db, editable=True), "push": None}
    missing = _missing_destination(db)
    if missing:
        raise PublishRefused(missing, f"the destination {missing} is empty")
    if not credential_set(row):
        raise PublishRefused("credential", "the credential is not set")
    if row.publish_enabled:
        return {**destination_fact(db, editable=True), "push": None}
    row.publish_enabled = True
    db.commit()
    try:
        outcome = dict((push or _push_now)(db) or {})
    except Exception as e:  # noqa: BLE001 — a push failure never undoes the switch or a row
        db.rollback()
        outcome = {"result": "failed", "reason": f"{type(e).__name__}: {e}"}
    for key in ("reason", "error"):
        if key in outcome:
            outcome[key] = redact(db, outcome[key])
    return {**destination_fact(db, editable=True), "push": outcome}
