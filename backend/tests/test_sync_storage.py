"""GRPH-1320: sync storage on Specs — folder path, blob directory, index; row sync states."""
import hashlib

from app.config import settings
from app.db import SessionLocal
from app.models import Prd
from app.services import documents as docs_svc
from app.services import drive_sync
from app.services import prds as prd_svc

URL = "/api/platform/sync-storage"


def _project(client, auth):
    client.post("/api/projects", json={"name": "Sync"}, headers=auth)  # id: sync


def _prd(title, body, status=None):
    db = SessionLocal()
    prd = prd_svc.create_prd(db, title=title, project_id="sync", body=body)
    if status:
        db.get(Prd, prd.id).status = status
        db.commit()
    pid = prd.id
    db.close()
    return pid


def _configure(client, auth, folder, blobs=None):
    body = {"folder_path": str(folder)}
    if blobs is not None:
        body["blob_dir"] = str(blobs)
    r = client.put(f"{URL}?project_id=sync", json=body, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _rows(out):
    return {r["prd_id"]: r for r in out["rows"]}


def test_three_facts_and_rows_after_configuring(client, auth, tmp_path):
    _project(client, auth)
    pid = _prd("One", "# One\n\nbody\n")
    (tmp_path / "blobs").mkdir()
    out = _configure(client, auth, tmp_path, tmp_path / "blobs")
    assert out["folder"]["ok"] and out["folder"]["configured"]
    assert out["folder"]["path"] == str(tmp_path)
    assert out["folder"]["project_path"] == str(tmp_path / "sync")
    assert out["blob_dir"]["ok"] and out["blob_dir"]["path"] == str(tmp_path / "blobs")
    assert out["index"]["available"] is True and out["index"]["reason"] == ""
    assert _rows(out)[pid]["sync_state"] == "not_exported"
    # A GET is a read: nothing was written into the folder.
    assert not (tmp_path / "sync").exists()


def test_relative_path_is_refused(client, auth):
    _project(client, auth)
    r = client.put(f"{URL}?project_id=sync", json={"folder_path": "relative/dir"}, headers=auth)
    assert r.status_code == 422 and "absolute" in r.text


def test_unreadable_folder_is_a_failure_on_that_fact_and_keeps_rows(client, auth, tmp_path):
    _project(client, auth)
    pid = _prd("Kept", "# Kept\n\nbody\n")
    missing = tmp_path / "not-mounted"
    out = _configure(client, auth, missing)
    # Saved anyway, shown as a failure on the folder fact only.
    assert out["folder"]["path"] == str(missing)
    assert out["folder"]["ok"] is False and "does not exist" in out["folder"]["error"]
    assert out["index"]["available"] is True
    assert _rows(out)[pid]["sync_state"] == "unknown"
    ran = client.post(f"{URL}/run?project_id=sync", headers=auth).json()
    assert ran["run"] is None and ran["last_run"]["export_status"] == "not_run"
    assert str(missing) in ran["last_run"]["error"]
    assert _rows(ran)[pid]["title"] == "Kept"
    assert not missing.exists()


def test_run_exports_then_in_sync_and_approved_file_edit_is_a_conflict(client, auth, tmp_path):
    _project(client, auth)
    pid = _prd("Gov", "# Gov\n\noriginal\n")
    _configure(client, auth, tmp_path)
    ran = client.post(f"{URL}/run?project_id=sync", headers=auth).json()
    assert pid in ran["run"]["exported"] and ran["last_run"]["export_status"] == "ok"
    assert ran["last_run"]["at"].endswith("+00:00")  # an instant, not a local wall time
    assert _rows(ran)[pid]["sync_state"] == "in_sync"

    db = SessionLocal()
    db.get(Prd, pid).status = "approved"
    db.commit()
    db.close()
    fp = next((tmp_path / "sync" / "PRDs").glob("*.md"))
    fp.write_text(fp.read_text().replace("original", "folder edit"))

    out = client.get(f"{URL}?project_id=sync", headers=auth).json()
    assert _rows(out)[pid]["sync_state"] == "conflict"
    (c,) = out["report"]["conflict_details"]
    assert c["prd_id"] == pid and c["ledger_hash"] != c["file_hash"]
    assert "folder edit" not in str(out) and "original" not in str(c)

    ran = client.post(f"{URL}/run?project_id=sync", headers=auth).json()
    assert ran["run"]["conflicts"] == [pid] and ran["run"]["updated_db"] == []
    db = SessionLocal()
    assert "original" in db.get(Prd, pid).body
    db.close()


def test_corrupt_file_is_a_read_failure_not_a_conflict_or_blank(client, auth, tmp_path):
    _project(client, auth)
    pid = _prd("Bytes", "# Bytes\n\nthe ledger body\n")
    _configure(client, auth, tmp_path)
    client.post(f"{URL}/run?project_id=sync", headers=auth)
    fp = next((tmp_path / "sync" / "PRDs").glob("*.md"))
    corrupt = b"\xff\xfe\x00garbage\x80"
    fp.write_bytes(corrupt)
    stray = tmp_path / "sync" / "PRDs" / "stray.md"
    stray.write_bytes(b"\xc3\x28 not utf-8")

    out = client.get(f"{URL}?project_id=sync", headers=auth).json()
    row = _rows(out)[pid]
    assert row["sync_state"] == "read_failed" and "UTF-8" in row["error"]
    assert out["report"]["conflicts"] == []
    names = {f["file_name"]: f for f in out["report"]["read_failures"]}
    assert names[fp.name]["prd_id"] == pid and names["stray.md"]["prd_id"] is None

    ran = client.post(f"{URL}/run?project_id=sync", headers=auth).json()
    assert ran["run"]["conflicts"] == [] and ran["run"]["imported"] == []
    assert ran["run"]["exported"] == [] and ran["run"]["updated_file"] == []
    assert fp.read_bytes() == corrupt  # not exported over
    db = SessionLocal()
    assert db.get(Prd, pid).body == "# Bytes\n\nthe ledger body\n"  # not blanked
    assert db.query(Prd).filter(Prd.project_id == "sync").count() == 1  # stray not imported
    db.close()


def test_empty_body_whose_hash_matches_is_in_sync(client, auth, tmp_path):
    _project(client, auth)
    pid = _prd("Empty", "")
    _configure(client, auth, tmp_path)
    client.post(f"{URL}/run?project_id=sync", headers=auth)
    out = client.get(f"{URL}?project_id=sync", headers=auth).json()
    assert _rows(out)[pid]["sync_state"] == "in_sync"
    assert out["report"]["read_failures"] == [] and out["report"]["conflicts"] == []


def test_failed_export_does_not_roll_the_ledger_back(client, auth, tmp_path, monkeypatch):
    _project(client, auth)
    pid = _prd("Fail", "# Fail\n\nbody\n")
    _configure(client, auth, tmp_path)

    def broken(self, subdir, name, content):
        raise OSError("drive offline")

    monkeypatch.setattr(drive_sync.LocalFolderBackend, "write", broken)
    ran = client.post(f"{URL}/run?project_id=sync", headers=auth).json()
    assert ran["run"]["export_status"] == "failed" and ran["run"]["conflicts"] == []
    assert ran["last_run"]["export_status"] == "failed"
    assert _rows(ran)[pid]["sync_state"] == "not_exported"
    db = SessionLocal()
    assert db.get(Prd, pid).body == "# Fail\n\nbody\n"
    db.close()


def test_blob_directory_is_where_bytes_are_stored(client, auth, tmp_path):
    _project(client, auth)
    blobs = tmp_path / "my-blobs"
    blobs.mkdir()
    _configure(client, auth, tmp_path, blobs)
    data = b"\x89PNG canvas bytes"
    db = SessionLocal()
    docs_svc.put_blob(db, project_id="sync", kind="canvas", data=data, mime="image/png",
                      set_id="S1", screen="home")
    key = hashlib.sha256(data).hexdigest()
    assert (blobs / key[:2] / key).read_bytes() == data
    db.close()


def test_hosted_box_refuses_paths(client, auth, tmp_path, monkeypatch):
    _project(client, auth)
    monkeypatch.setattr(settings, "hosted_mode", True)
    r = client.put(f"{URL}?project_id=sync", json={"folder_path": str(tmp_path)}, headers=auth)
    assert r.status_code in (400, 403, 404)  # authz answers first; then the hosted refusal
    monkeypatch.setattr(settings, "hosted_mode", False)
    out = client.get(f"{URL}?project_id=sync", headers=auth).json()
    assert out["folder"]["configured"] is False  # nothing was saved


def test_hosted_refusal_after_authz(client, auth, tmp_path, monkeypatch):
    from app.routers import platform as platform_router

    _project(client, auth)
    monkeypatch.setattr(platform_router.settings, "hosted_mode", False)
    monkeypatch.setattr(platform_router, "_require_self_hosted_box",
                        lambda: (_ for _ in ()).throw(platform_router.HTTPException(400, "self-hosted")))
    r = client.put(f"{URL}?project_id=sync", json={"folder_path": str(tmp_path)}, headers=auth)
    assert r.status_code == 400 and "self-hosted" in r.text
