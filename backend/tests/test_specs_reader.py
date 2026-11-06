"""GRPH-1319 — the Spec reader's REST surface: PRD hashes, preamble, set constraints."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.hashing import content_hash
from app.models import Document, Project
from app.services import design_sets as ds_svc
from app.services import prds as prd_svc
from tests.prd_approve import approve

BODY = "## Tokens\nUse the teal accent everywhere.\n\n## Spacing\nEight point grid.\n"
KEY = "key:alex"


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def pid(db):
    return db.scalars(select(Project.id)).first()


def _prd(db, pid, baselined=True, title="Screens", body=BODY):
    prd = prd_svc.create_prd(db, title=title, project_id=pid, body=body)
    if baselined:
        approve(db, prd)
    return prd


def test_prd_facts_label_a_draft_with_no_baseline(client, auth, db, pid):
    prd = _prd(db, pid, baselined=False)
    r = client.get(f"/api/specs/prds/{prd.id}", params={"project_id": pid}, headers=auth)
    assert r.status_code == 200
    out = r.json()
    assert out["status"] == "draft" and out["baseline_hash"] is None
    assert out["content_hash"] == content_hash(BODY) and out["design_set"] is None


def test_saving_an_approved_prd_writes_a_draft_and_keeps_the_baseline(client, auth, db, pid):
    prd = _prd(db, pid)
    before = client.get(f"/api/specs/prds/{prd.id}", params={"project_id": pid}, headers=auth).json()
    assert before["status"] == "approved" and before["baseline_hash"] == content_hash(BODY)
    r = client.patch(f"/api/prds/{prd.id}", headers=auth,
                     json={"body": BODY + "\n## Later\nMore.\n", "status": "draft"})
    assert r.status_code == 200
    after = client.get(f"/api/specs/prds/{prd.id}", params={"project_id": pid}, headers=auth).json()
    assert after["status"] == "draft"
    assert after["baseline_hash"] == before["baseline_hash"]
    assert after["content_hash"] != before["content_hash"]
    rows = client.get("/api/prds", params={"project_id": pid}, headers=auth).json()
    assert [p["id"] for p in rows].count(prd.id) == 1


def test_missing_preamble_is_404_not_an_empty_body(client, auth, pid):
    r = client.get("/api/specs/preamble", params={"project_id": pid}, headers=auth)
    assert r.status_code == 404


def test_put_preamble_bumps_version_and_names_sets_pinned_to_the_old_one(client, auth, db, pid):
    base = _prd(db, pid, title="Visual system")
    ds_svc.put_preamble(db, pid, "Be calm.", base.id, created_by=KEY)
    ds = ds_svc.open_design(db, pid, _prd(db, pid).id)
    r = client.put("/api/specs/preamble", headers=auth,
                   json={"project_id": pid, "body": "Be bold.", "cites_prd_id": base.id})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["version"] == 2 and out["body_state"] == "stored" and out["body"] == "Be bold."
    me = client.get("/api/auth/me", headers=auth).json()
    assert out["created_by"] == f"user:{me['id']}"
    stale = {s["id"]: s for s in out["stale_sets"]}
    assert stale[ds["id"]]["status"] == "stale" and stale[ds["id"]]["stale_reason"] == "preamble"
    assert [h["version"] for h in out["history"]] == [1, 2]


def test_preamble_whose_sections_do_not_rebuild_the_hash_is_unreadable_not_blank(client, auth, db, pid):
    base = _prd(db, pid, title="Visual system")
    # Two blank lines between sections: `extract` keeps the sections, not the spacing.
    ds_svc.put_preamble(db, pid, "Intro.\n\n\n## A\nOne.", base.id, created_by=KEY)
    out = client.get("/api/specs/preamble", params={"project_id": pid}, headers=auth).json()
    assert out["body_state"] == "unreadable" and out["body"] is None
    assert [s["heading"] for s in out["sections"]] == ["", "A"]


def test_pending_body_is_pending(client, auth, db, pid):
    base = _prd(db, pid, title="Visual system")
    ds_svc.put_preamble(db, pid, "Be calm.", base.id, created_by=KEY)
    doc = db.scalars(select(Document).where(Document.kind == "preamble")).first()
    doc.blob_status = "pending"
    db.commit()
    out = client.get("/api/specs/preamble", params={"project_id": pid}, headers=auth).json()
    assert out["body_state"] == "pending" and out["body"] is None


def test_constraints_accept_names_the_session_person_and_unblocks_compile(client, auth, db, pid):
    prd = _prd(db, pid)
    sid = ds_svc.open_design(db, pid, prd.id)["id"]
    ds_svc.put_set_constraints(db, pid, sid, "Teal only.", screens=[{"screen": "home"}])
    ds_svc.confirm_tier(db, pid, sid, "home", "BACKED", "route exists")

    view = client.get(f"/api/specs/design-sets/{sid}", headers=auth).json()
    assert view["constraints_accepted"] is False and view["body"] == "Teal only."
    assert view["screens"][0]["compile_refused"] == ["the set constraints are not accepted"]
    assert view["screens"][0]["prompt"] == "none"
    assert client.post(f"/api/specs/design-sets/{sid}/compile", json={}, headers=auth).status_code == 422

    r = client.post(f"/api/specs/design-sets/{sid}/accept-constraints", headers=auth)
    assert r.status_code == 200, r.text
    view = r.json()
    assert view["constraints_accepted"] is True and view["screens"][0]["compile_refused"] == []
    assert view["history"][-1]["person"] and not view["history"][-1]["person"].startswith("key:")

    r = client.post(f"/api/specs/design-sets/{sid}/compile", json={"screen": "home"}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["screens"][0]["prompt"] == "compiled"

    # A later put of the constraints body returns the set to not accepted.
    ds_svc.put_set_constraints(db, pid, sid, "Amber only.", screens=[{"screen": "home"}])
    view = client.get(f"/api/specs/design-sets/{sid}", headers=auth).json()
    assert view["constraints_accepted"] is False and view["history"][-1]["current"] is False

    facts = client.get(f"/api/specs/prds/{prd.id}", params={"project_id": pid}, headers=auth).json()
    assert facts["design_set"]["id"] == sid


def test_stale_prompt_stays_stale_after_acceptance(client, auth, db, pid):
    base = _prd(db, pid, title="Visual system")
    ds_svc.put_preamble(db, pid, "Be calm.", base.id, created_by=KEY)
    sid = ds_svc.open_design(db, pid, _prd(db, pid).id)["id"]
    ds_svc.put_set_constraints(db, pid, sid, "Teal only.", screens=[{"screen": "home"}])
    ds_svc.confirm_tier(db, pid, sid, "home", "BACKED", "route exists")
    client.post(f"/api/specs/design-sets/{sid}/accept-constraints", headers=auth)
    client.post(f"/api/specs/design-sets/{sid}/compile", json={}, headers=auth)
    ds_svc.put_preamble(db, pid, "Be bold.", base.id, created_by=KEY)
    view = client.get(f"/api/specs/design-sets/{sid}", headers=auth).json()
    assert view["status"] == "stale" and view["screens"][0]["prompt"] == "stale"


def test_unknown_set_is_404(client, auth):
    assert client.get("/api/specs/design-sets/DS-nope", headers=auth).status_code == 404


def test_library_lists_prd_preamble_and_design_documents(client, auth, db, pid):
    """One read: a draft with no baseline, the preamble, set constraints, and a screen.

    Bodies stay on the reader. This payload does not carry them, and a missing preamble
    is null rather than a 404.
    """
    bare = client.get("/api/specs/library", params={"project_id": pid}, headers=auth)
    assert bare.status_code == 200, bare.text
    assert bare.json()["preamble"] is None

    # A preamble cites a baselined PRD. The draft beside it has no baseline of its own.
    cited = _prd(db, pid, title="Visual system")
    prd = _prd(db, pid, baselined=False, title="Library draft")
    ds_svc.put_preamble(db, pid, "Be calm.", cited.id, created_by=KEY)
    sid = ds_svc.open_design(db, pid, prd.id)["id"]
    ds_svc.put_set_constraints(db, pid, sid, "Stay calm.", screens=[{"screen": "Home"}])

    r = client.get("/api/specs/library", params={"project_id": pid}, headers=auth)
    assert r.status_code == 200, r.text
    out = r.json()
    assert "Be calm." not in r.text and "Stay calm." not in r.text

    row = next(p for p in out["prds"] if p["id"] == prd.id)
    assert row["status"] == "draft" and row["title"] == "Library draft"
    assert row["baseline_hash"] is None and row["baseline_version"] is None
    assert row["content_hash"] == content_hash(BODY) and row["conflict"] is None
    assert prd.id not in out["conflict_ids"]

    pre = out["preamble"]
    assert set(pre) == {"id", "title", "version", "content_hash"}
    assert pre["title"] == "Preamble" and pre["content_hash"] == content_hash("Be calm.")

    ours = [d for d in out["documents"] if d["prd_id"] == prd.id]
    assert [(d["kind"], d["title"], d["status"]) for d in ours] == [
        ("constraints", "Set constraints", "not accepted"),
        ("screen", "Home", "none"),
    ]
    constraints, screen = ours
    assert constraints["set_id"] == sid and constraints["hash"] == content_hash("Stay calm.")
    assert constraints["screen"] == "" and screen["screen"] == "Home" and screen["hash"] == ""
    assert set(constraints) == {
        "kind", "set_id", "prd_id", "screen", "title", "status", "hash", "set_status",
    }


def test_library_conflict_carries_hashes_not_bodies(client, auth, db, pid, tmp_path):
    """An approved file that no longer matches is a conflict: hashes and path, neither body.

    Exporting the other PRDs in the project does not put them in `conflict_ids`.
    """
    prd = _prd(db, pid, baselined=True, title="Governed")
    saved = client.put(
        "/api/platform/sync-storage", params={"project_id": pid},
        json={"folder_path": str(tmp_path)}, headers=auth)
    assert saved.status_code == 200, saved.text
    ran = client.post("/api/platform/sync-storage/run", params={"project_id": pid}, headers=auth)
    assert ran.status_code == 200, ran.text
    assert prd.id in ran.json()["run"]["exported"]

    folder = tmp_path / pid / "PRDs"
    mine = next(f for f in folder.glob("*.md") if "Tokens" in f.read_text())
    mine.write_text(mine.read_text().replace("Tokens", "FOLDER"))

    r = client.get("/api/specs/library", params={"project_id": pid}, headers=auth)
    assert r.status_code == 200, r.text
    assert "FOLDER" not in r.text
    out = r.json()
    row = next(p for p in out["prds"] if p["id"] == prd.id)
    assert row["status"] == "approved" and row["sync_state"] == "conflict"
    assert row["baseline_hash"] == content_hash(BODY)
    assert row["content_hash"] == content_hash(BODY)
    assert out["conflict_ids"] == [prd.id]
    fact = row["conflict"]
    assert set(fact) == {"prd_id", "ledger_hash", "file_hash", "file_name", "byte_length", "path"}
    assert fact["ledger_hash"] != fact["file_hash"]
    assert fact["path"].endswith(mine.name)
    assert "FOLDER" not in str(fact) and "Tokens" not in str(fact)
    assert all(p["sync_state"] == "conflict" for p in out["prds"] if p["id"] in out["conflict_ids"])


def test_preamble_gate_takes_a_signed_in_person_and_refuses_an_agent_seat(db, pid):
    from app import errors
    from app.services import documents as doc_svc

    base = _prd(db, pid, title="Visual system")
    doc_svc.put_document(db, project_id=pid, kind="preamble", body="Be calm.",
                         prd_id=base.id, created_by="user:u-1")
    rows = db.scalars(select(Document).where(Document.project_id == pid,
                                             Document.kind == "preamble")).all()
    assert [r.created_by for r in rows] == ["user:u-1"]
    with pytest.raises(errors.Validation):
        doc_svc.put_document(db, project_id=pid, kind="preamble", body="Be bold.",
                             prd_id=base.id, created_by="GRPH-A1")


def test_library_and_sync_storage_show_the_rendered_key_after_a_retag(client, auth, db, pid, tmp_path):
    """The tag is not a prefix of the frozen id: library and sync-storage use the rendered key."""
    from app.services import projects as project_svc

    prd = _prd(db, pid, baselined=True, title="Governed")
    project_svc.retag_project(db, pid, "ZQ")
    db.expire_all()
    key = db.get(type(prd), prd.id).key
    assert key.startswith("ZQ") and key != prd.id
    assert client.put("/api/platform/sync-storage", params={"project_id": pid},
                      json={"folder_path": str(tmp_path)}, headers=auth).status_code == 200
    assert client.post("/api/platform/sync-storage/run", params={"project_id": pid},
                       headers=auth).status_code == 200
    mine = next(f for f in (tmp_path / pid / "PRDs").glob("*.md") if "Tokens" in f.read_text())
    mine.write_text(mine.read_text().replace("Tokens", "FOLDER"))

    out = client.get("/api/specs/library", params={"project_id": pid}, headers=auth).json()
    row = next(p for p in out["prds"] if p["title"] == "Governed")
    assert row["id"] == key and row["sync_state"] == "conflict"
    assert out["conflict_ids"] == [key] and row["conflict"]["prd_id"] == key
    assert client.get(f"/api/prds/{key}", headers=auth).json()["id"] == key
    storage = client.get("/api/platform/sync-storage", params={"project_id": pid}, headers=auth)
    st = storage.json()
    assert key in [r["prd_id"] for r in st["rows"]]
    assert st["report"]["conflicts"] == [key]
    assert st["report"]["conflict_details"][0]["prd_id"] == key
    ids = [p["id"] for p in out["prds"]] + out["conflict_ids"] + [r["prd_id"] for r in st["rows"]]
    assert prd.id not in ids and prd.id not in st["report"]["conflicts"]
