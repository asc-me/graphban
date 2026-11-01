"""GRPH-1279 — design set: open, preamble pinning, constraints, tier confirmation, stale, frozen."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app import errors
from app.hashing import content_hash
from app.models import CodeNode, DesignSet, Document, PlatformConfig, Project
from app.services import design_sets as ds_svc
from app.services import documents as docs
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


def _rebaseline(db, prd, body):
    prd_svc.request_rebaseline(db, prd, reason_type="correction", reason="x", requested_by="agent")
    prd_svc.update_prd(db, prd.id, body=body)
    approve(db, prd_svc.get_prd(db, prd.id))


def test_open_stores_the_baseline_hash_and_one_set_per_prd(db, pid):
    prd = _prd(db, pid)
    out = ds_svc.open_design(db, pid, prd.id)
    assert out["status"] == "open" and out["prd_baselined"] is True
    assert out["prd_hash"] == content_hash(prd_svc.baseline_of(db, prd.id).body)
    assert out["preamble_version"] == 0
    with pytest.raises(errors.Validation):
        ds_svc.open_design(db, pid, prd.id)


def test_unbaselined_prd_drafts_but_cannot_be_accepted(db, pid):
    prd = _prd(db, pid, baselined=False)
    out = ds_svc.open_design(db, pid, prd.id)
    assert out["prd_baselined"] is False and out["prd_hash"] == content_hash(prd.body)
    assert any("no baseline" in b for b in out["acceptance_blockers"])
    ds_svc.put_set_constraints(db, pid, out["id"], "Teal only.")  # drafting works


def test_preamble_pins_the_cited_baseline_and_is_stored_on_open(db, pid):
    base = _prd(db, pid, title="Visual system")
    pre = ds_svc.put_preamble(db, pid, "Be calm.", base.id, created_by=KEY)
    assert pre["preamble_version"] == 1
    assert pre["prd_hash"] == content_hash(prd_svc.baseline_of(db, base.id).body)
    out = ds_svc.open_design(db, pid, _prd(db, pid).id)
    assert out["preamble_version"] == 1 and out["preamble_hash"] == pre["prd_hash"]
    with pytest.raises(errors.Validation):  # no baseline on the cited PRD
        ds_svc.put_preamble(db, pid, "x", _prd(db, pid, baselined=False, title="d").id, created_by=KEY)


def test_preamble_edit_stales_the_set_and_its_prompts_stay_searchable(db, pid):
    base = _prd(db, pid, title="Visual system")
    ds_svc.put_preamble(db, pid, "Be calm.", base.id, created_by=KEY)
    out = ds_svc.open_design(db, pid, _prd(db, pid).id)
    # A prompt row of the set, accepted: search with include_design returns it.
    prompt = Document(id="DOC-p1", project_id=pid, kind="design_prompt", set_id=out["id"],
                      screen="home", version=1, status="active", content_hash="h",
                      governing=True, accepted=True)
    docs._chunk(prompt, "## Home\nteal navbar zebra")
    db.add(prompt)
    db.commit()
    assert docs.search_docs(db, pid, "zebra", include_design=True)["results"][0]["status"] == "accepted"
    ds_svc.put_preamble(db, pid, "Be bold.", base.id, created_by=KEY)
    got = ds_svc.get_design_set(db, pid, out["id"])
    assert got["status"] == "stale" and got["stale_reason"] == "preamble"
    hit = docs.search_docs(db, pid, "zebra", include_design=True)["results"][0]
    assert hit["status"] == "stale" and hit["stale_reason"] == "preamble"
    assert docs.search_docs(db, pid, "zebra")["results"] == []
    with pytest.raises(errors.Validation):  # a stale set is the existing set
        ds_svc.open_design(db, pid, out["prd_id"])


def test_prd_baseline_change_stales_the_set_and_names_the_hash(db, pid):
    prd = _prd(db, pid)
    out = ds_svc.open_design(db, pid, prd.id)
    _rebaseline(db, prd, BODY.replace("Eight point grid.", "Four point grid."))
    got = ds_svc.get_design_set(db, pid, out["id"])
    assert got["status"] == "stale" and got["stale_reason"] == "prd_baseline"
    moved = got["stale_detail"]["prd_baseline"]
    assert moved["was"] == out["prd_hash"] and moved["now"] != moved["was"]


def test_rebaselining_the_preamble_cited_prd_stales_sets_on_other_prds(db, pid):
    base = _prd(db, pid, title="Visual system")
    pre = ds_svc.put_preamble(db, pid, "Be calm.", base.id, created_by=KEY)
    out = ds_svc.open_design(db, pid, _prd(db, pid).id)
    _rebaseline(db, base, BODY.replace("teal", "amber"))
    got = ds_svc.get_design_set(db, pid, out["id"])
    assert got["status"] == "stale" and got["stale_reason"] == "prd_baseline"
    moved = got["stale_detail"]["prd_baseline"]
    assert moved["prd_id"] == base.id and moved["was"] == pre["prd_hash"]
    assert moved["now"] == content_hash(prd_svc.baseline_of(db, base.id).body) != moved["was"]
    # Re-pinning a fresh preamble against the new baseline and revising clears it.
    ds_svc.put_preamble(db, pid, "Be calm.", base.id, created_by=KEY)
    assert ds_svc.put_set_constraints(db, pid, out["id"], "Teal only.")["status"] == "open"


def test_open_against_a_preamble_citing_a_superseded_baseline_is_stale_at_once(db, pid):
    base = _prd(db, pid, title="Visual system")
    pre = ds_svc.put_preamble(db, pid, "Be calm.", base.id, created_by=KEY)
    _rebaseline(db, base, BODY.replace("teal", "amber"))
    out = ds_svc.open_design(db, pid, _prd(db, pid).id)
    assert out["status"] == "stale" and out["stale_reason"] == "prd_baseline"
    assert out["stale_detail"]["prd_baseline"]["prd_id"] == base.id
    assert out["stale_detail"]["prd_baseline"]["was"] == pre["prd_hash"]
    assert out["acceptance_blockers"]
    # An unrelated rebaseline does not change what the set already says.
    _rebaseline(db, _prd(db, pid, title="Unrelated"), BODY.replace("grid", "lattice"))
    got = ds_svc.get_design_set(db, pid, out["id"])
    assert got["status"] == "stale" and got["stale_reason"] == "prd_baseline"


def test_revising_does_not_clear_a_superseded_preamble_cite(db, pid):
    base = _prd(db, pid, title="Visual system")
    ds_svc.put_preamble(db, pid, "Be calm.", base.id, created_by=KEY)
    out = ds_svc.open_design(db, pid, _prd(db, pid).id)
    _rebaseline(db, base, BODY.replace("teal", "amber"))
    again = ds_svc.put_set_constraints(db, pid, out["id"], "Teal only.")
    assert again["status"] == "stale" and again["stale_reason"] == "prd_baseline"
    assert again["acceptance_blockers"]
    _rebaseline(db, _prd(db, pid, title="Unrelated"), BODY.replace("grid", "lattice"))
    assert ds_svc.get_design_set(db, pid, out["id"])["stale_reason"] == "prd_baseline"
    # Re-putting the preamble against the new baseline is what clears it.
    ds_svc.put_preamble(db, pid, "Be calm.", base.id, created_by=KEY)
    assert ds_svc.put_set_constraints(db, pid, out["id"], "Teal only.")["status"] == "open"


def test_first_preamble_stales_a_set_opened_without_one(db, pid):
    out = ds_svc.open_design(db, pid, _prd(db, pid).id)
    assert out["preamble_version"] == 0
    ds_svc.put_preamble(db, pid, "Be calm.", _prd(db, pid, title="Visual system").id, created_by=KEY)
    got = ds_svc.get_design_set(db, pid, out["id"])
    assert got["status"] == "stale" and got["stale_reason"] == "preamble"
    assert got["stale_detail"]["preamble"]["was"] == ""


def test_revising_a_stale_set_repins_it(db, pid):
    prd = _prd(db, pid)
    out = ds_svc.open_design(db, pid, prd.id)
    _rebaseline(db, prd, BODY.replace("Eight point grid.", "Four point grid."))
    again = ds_svc.put_set_constraints(db, pid, out["id"], "Teal only.", sections=["Tokens"])
    assert again["status"] == "open" and again["prd_hash"] != out["prd_hash"]
    assert again["constraints_hash"] == content_hash("Teal only.")
    assert again["constraints_accepted"] is False and again["sections"] == ["Tokens"]


def test_tier_proposal_is_a_suggestion_until_confirmed(db, pid):
    prd = _prd(db, pid)
    out = ds_svc.open_design(db, pid, prd.id)
    db.add(CodeNode(id="cn_r1", project_id=pid, path="/home", kind="file"))
    db.commit()
    ds_svc.put_set_constraints(db, pid, out["id"], "c", screens=[
        {"screen": "home", "routes": ["/home"]},
        {"screen": "mix", "routes": ["/home", "/nope"]},
        {"screen": "none", "routes": ["/nope"]}])
    got = {s["screen"]: s for s in ds_svc.get_design_set(db, pid, out["id"])["screens"]}
    assert [got[k]["tier_proposal"] for k in ("home", "mix", "none")] == ["BACKED", "HALF", "UNBACKED"]
    assert got["home"]["tier"] == "UNBACKED" and got["home"]["tier_confirmed"] is False
    done = ds_svc.confirm_tier(db, pid, out["id"], "home", "BACKED", "route exists")
    home = next(s for s in done["screens"] if s["screen"] == "home")
    assert home["tier"] == "BACKED" and home["tier_confirmed"] and home["tier_note"] == "route exists"
    with pytest.raises(errors.Validation):
        ds_svc.confirm_tier(db, pid, out["id"], "home", "BACKED", "  ")
    with pytest.raises(errors.Validation):
        ds_svc.confirm_tier(db, pid, out["id"], "home", "GOLD", "x")


def test_graph_never_pushed_or_sync_graph_off_proposes_unbacked(db, pid):
    assert ds_svc.propose_tier(db, pid, ["/x"])[0] == "UNBACKED"
    db.add(CodeNode(id="cn_r2", project_id=pid, path="/x", kind="file"))
    db.commit()
    assert ds_svc.propose_tier(db, pid, ["/x"])[0] == "BACKED"
    cfg = db.get(PlatformConfig, pid) or PlatformConfig(project_id=pid)
    cfg.sync_graph = False
    db.add(cfg)
    db.commit()
    assert ds_svc.propose_tier(db, pid, ["/x"])[0] == "UNBACKED"


def test_closed_prd_freezes_the_set_and_refuses_writes(db, pid):
    prd = _prd(db, pid)
    out = ds_svc.open_design(db, pid, prd.id)
    ds_svc.confirm_tier(db, pid, out["id"], "home", "HALF", "partly")
    dispositions = [{"section": s, "disposition": "deferred", "reason": "n/a"}
                    for s in prd_svc.dropped_intent(db, prd)]
    prd_svc.close_prd(db, prd, dispositions=dispositions, closed_by="user:1")
    assert db.get(DesignSet, out["id"]).status == "frozen"
    assert ds_svc.get_design_set(db, pid, out["id"])["screens"]  # still readable
    with pytest.raises(errors.Validation):
        ds_svc.put_set_constraints(db, pid, out["id"], "x")
    with pytest.raises(errors.Validation):
        ds_svc.confirm_tier(db, pid, out["id"], "home", "BACKED", "x")
    with pytest.raises(errors.Validation):
        ds_svc.open_design(db, pid, prd.id)


def test_mcp_round_trip(client, auth, db, pid):
    from tests.test_documents import _mcp

    base = _prd(db, pid, title="Visual system")
    prd = _prd(db, pid)
    key = client.post("/api/api-keys", json={"name": "ds-e2e"}, headers=auth).json()["plaintext"]
    h = {"X-API-Key": key}

    def call(name, args, n):
        r = _mcp(client, h, name, {**args, "project_id": pid}, n)
        assert "error" not in r.json(), r.text
        return r.json()["result"]["structuredContent"]

    call("put_preamble", {"body": "Calm.", "cites_prd_id": base.id}, 1)
    opened = call("open_design", {"prd_id": prd.id}, 2)
    assert opened["preamble_version"] == 1
    call("put_set_constraints", {"design_set_id": opened["id"], "body": "Teal.",
                                  "screens": [{"screen": "home", "routes": ["/home"]}]}, 3)
    got = call("confirm_tier", {"design_set_id": opened["id"], "screen": "home",
                                 "tier": "UNBACKED", "note": "no route yet"}, 4)
    assert got["screens"][0]["tier_confirmed"] is True
    assert call("get_design_set", {"design_set_id": opened["id"]}, 5)["status"] == "open"
