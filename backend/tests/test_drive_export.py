"""GRPH-1278: Drive is an export and draft-import channel, not the baseline."""
from app.db import SessionLocal
from app.models import Prd
from app.services import drive_sync
from app.services import prds as prd_svc


def _project(client, auth):
    client.post("/api/projects", json={"name": "Sync"}, headers=auth)  # id: sync


def _edit_file(tmp_path, old, new):
    fp = next((tmp_path / "PRDs").glob("*.md"))
    fp.write_text(fp.read_text().replace(old, new))
    return fp


def test_file_change_on_approved_prd_is_a_conflict_without_bodies(client, auth, tmp_path):
    _project(client, auth)
    db = SessionLocal()
    prd = prd_svc.create_prd(db, title="Gov", project_id="sync", body="# Gov\n\noriginal\n")
    drive_sync.sync(db, "sync", root_dir=tmp_path)
    prd_row = db.get(Prd, prd.id)
    prd_row.status = "approved"
    db.commit()
    fp = _edit_file(tmp_path, "original", "sneaky folder edit")
    rep = drive_sync.sync(db, "sync", root_dir=tmp_path)
    assert rep["updated_db"] == []
    assert rep["conflicts"] == [prd.id]
    (c,) = rep["conflict_details"]
    assert c["prd_id"] == prd.id and c["status"] == "approved"
    assert c["ledger_hash"] != c["file_hash"]
    assert c["file_name"] == fp.name
    assert c["byte_length"] == len(fp.read_bytes())
    assert c["path"] == f"PRDs/{fp.name}"
    assert "sneaky" not in str(c) and "original" not in str(c)
    db.expire_all()
    assert "original" in db.get(Prd, prd.id).body
    db.close()


def test_draft_still_updates_alongside_approved(client, auth, tmp_path):
    _project(client, auth)
    db = SessionLocal()
    draft = prd_svc.create_prd(db, title="Dr", project_id="sync", body="# Dr\n\nfirst\n")
    drive_sync.sync(db, "sync", root_dir=tmp_path)
    fp = next((tmp_path / "PRDs").glob("*.md"))
    fp.write_text(fp.read_text().replace("first", "second"))
    rep = drive_sync.sync(db, "sync", root_dir=tmp_path)
    assert draft.id in rep["updated_db"] and rep["conflicts"] == []
    db.close()


def test_pending_rebaseline_blocks_file_write(client, auth, tmp_path):
    _project(client, auth)
    db = SessionLocal()
    prd = prd_svc.create_prd(db, title="Rb", project_id="sync", body="# Rb\n\nkeep\n")
    drive_sync.sync(db, "sync", root_dir=tmp_path)
    db.get(Prd, prd.id).pending_rebaseline = {"reason": "x"}
    db.commit()
    _edit_file(tmp_path, "keep", "lose")
    rep = drive_sync.sync(db, "sync", root_dir=tmp_path)
    assert rep["conflicts"] == [prd.id]
    assert [c["prd_id"] for c in rep["conflict_details"]] == [prd.id]
    db.expire_all()
    assert "keep" in db.get(Prd, prd.id).body
    db.close()


def test_failed_export_reports_failed_and_keeps_row(client, auth, tmp_path):
    _project(client, auth)
    db = SessionLocal()
    prd = prd_svc.create_prd(db, title="Fail", project_id="sync", body="# Fail\n\nbody\n")

    class Broken(drive_sync.LocalFolderBackend):
        def write(self, subdir, name, content):
            raise OSError("drive offline")

    rep = drive_sync.sync(db, "sync", root_dir=tmp_path, backend=Broken(tmp_path))
    assert rep["export_status"] == "failed"
    assert db.get(Prd, prd.id) is not None
    assert drive_sync.sync(db, "sync", root_dir=tmp_path)["export_status"] == "ok"
    db.close()
