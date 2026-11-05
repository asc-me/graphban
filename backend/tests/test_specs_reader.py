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
    assert out["created_by"].startswith("key:")
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
