"""GRPH-1280 — design review loop: compile, accept_prompt, put_canvas, findings, accept_design."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app import errors
from app.hashing import content_hash
from app.models import DesignCorrection, DesignSet, Document, DocumentChunk, Project
from app.services import design_sets as ds_svc
from app.services import documents as docs
from app.services import prds as prd_svc
from app.services.drive_sync import LocalFolderBackend
from tests.prd_approve import approve

BODY = "## Tokens\nUse the teal accent everywhere.\n\n## Spacing\nEight point grid.\n"
KEY = "key:alex"
COMPILER = "agent:GRPH-A1"


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


@pytest.fixture()
def be(tmp_path):
    return LocalFolderBackend(tmp_path)


@pytest.fixture()
def world(db, pid):
    prd = prd_svc.create_prd(db, title="Screens", project_id=pid, body=BODY)
    approve(db, prd)
    ds = ds_svc.open_design(db, pid, prd.id)
    ds_svc.put_set_constraints(db, pid, ds["id"], "Teal only.", screens=[{"screen": "home"}])
    ds_svc.confirm_tier(db, pid, ds["id"], "home", "BACKED", "route exists")
    row = db.get(DesignSet, ds["id"])
    row.constraints_accepted = True
    db.commit()
    return prd, ds["id"]


def _compile(db, pid, sid, screen=None, actor=COMPILER):
    return ds_svc.compile_prompts(db, pid, sid, screen=screen, actor=actor)["prompts"]


def _ready(db, pid, sid, be, person="dana"):
    """Compiled, accepted, canvas bound: everything but accept_design."""
    h = _compile(db, pid, sid)[0]["prompt_hash"]
    ds_svc.accept_prompt(db, pid, sid, "home", "agent:GRPH-A2", person)
    c = ds_svc.put_canvas(db, pid, sid, "home", b"<html>1</html>", "text/html", h, backend=be)
    return h, c["content_hash"]


def test_compile_is_pure_and_refuses_unconfirmed_tier(db, pid, world):
    _, sid = world
    a = _compile(db, pid, sid)
    b = _compile(db, pid, sid)
    assert a[0]["prompt_hash"] == b[0]["prompt_hash"] and a[0]["new"] and not b[0]["new"]
    assert len(ds_svc._prompts(db, sid)) == 1
    ds_svc.put_set_constraints(db, pid, sid, "Teal only.", screens=[{"screen": "away"}])
    with pytest.raises(errors.Validation) as ei:
        _compile(db, pid, sid)
    assert "home" in str(ei.value) and "away" in str(ei.value) or "away" in str(ei.value)
    assert len(ds_svc._prompts(db, sid)) == 1  # nothing written


def test_compile_refuses_unaccepted_constraints_and_candidates_are_not_indexed(db, pid, world):
    _, sid = world
    db.get(DesignSet, sid).constraints_accepted = False
    db.commit()
    with pytest.raises(errors.Validation, match="constraints"):
        _compile(db, pid, sid)
    db.get(DesignSet, sid).constraints_accepted = True
    db.commit()
    _compile(db, pid, sid)
    hits = docs.search_docs(db, pid, "teal", include_design=True)["results"]
    assert all(r["kind"] == "prd" for r in hits)


def test_accept_prompt_needs_a_person_who_is_not_the_compiler(db, pid, world):
    _, sid = world
    _compile(db, pid, sid)
    with pytest.raises(errors.Validation, match="no person"):
        ds_svc.accept_prompt(db, pid, sid, "home", "agent:GRPH-A2")
    with pytest.raises(errors.Validation, match="compiler"):
        ds_svc.accept_prompt(db, pid, sid, "home", COMPILER, "dana")  # compiler relays
    ds_svc.accept_prompt(db, pid, sid, "home", "agent:GRPH-A2", "dana")
    from app.models import DesignAcceptance

    a = db.scalars(select(DesignAcceptance)).first()
    assert (a.person, a.relayed_by, a.kind) == ("dana", "agent:GRPH-A2", "prompt")
    # accepting a prompt neither accepts the design nor embeds it
    assert db.get(DesignSet, sid).status == "open"
    assert all(c.embedding is None for c in db.scalars(select(DocumentChunk)))


def test_a_credential_is_a_relay_never_a_person(db, pid, world, be):
    """Bounce of 2ba16655: omitting agent_id makes any agent `key:<name>`, which used to count
    as a person accepting with no accepted_by."""
    _, sid = world
    _compile(db, pid, sid, actor="GRPH-A9")
    with pytest.raises(errors.Validation, match="no person"):
        ds_svc.accept_prompt(db, pid, sid, "home", KEY)
    with pytest.raises(errors.Validation, match="not a person"):
        ds_svc.accept_prompt(db, pid, sid, "home", "agent:GRPH-A2", KEY)
    with pytest.raises(errors.Validation, match="compiler"):
        ds_svc.accept_prompt(db, pid, sid, "home", KEY, "GRPH-A9")
    out = ds_svc.accept_prompt(db, pid, sid, "home", KEY, "dana")
    assert (out["accepted_by"], out["relayed_by"]) == ("dana", KEY)
    with pytest.raises(errors.Validation, match="no person"):
        ds_svc.accept_design(db, pid, sid, KEY)


def test_a_registered_agent_id_is_not_a_person(db, pid, world):
    from app.models import Agent

    _, sid = world
    _compile(db, pid, sid)
    db.add(Agent(id="GRPH-A77", project_id=pid, number=77))
    db.commit()
    with pytest.raises(errors.Validation, match="not a person"):
        ds_svc.accept_prompt(db, pid, sid, "home", "agent:GRPH-A2", "GRPH-A77")


def test_accept_design_refuses_an_outdated_accepted_prompt(db, pid, world, be):
    """Bounce of 2ba16655: a correction recompiled the screen, then the OLD bytes were bound
    again against the OLD accepted prompt hash, and accept_design embedded the old prompt."""
    _, sid = world
    h, canvas = _ready(db, pid, sid, be)
    f = ds_svc.file_finding(db, pid, sid, "home", "prompt_defect", "Header is purple", canvas,
                            actor="agent:GRPH-A3")
    assert f["recompiled"]["prompt_hash"] != h  # only this screen, right after the correction
    ds_svc.resolve_finding(db, pid, f["id"], "applied")
    ds_svc.put_canvas(db, pid, sid, "home", b"<html>1</html>", "text/html", h, backend=be)
    with pytest.raises(errors.Validation, match="newer prompt"):
        ds_svc.accept_design(db, pid, sid, "agent:GRPH-A2", "dana")
    h2 = f["recompiled"]["prompt_hash"]
    ds_svc.accept_prompt(db, pid, sid, "home", "agent:GRPH-A2", "dana")
    ds_svc.put_canvas(db, pid, sid, "home", b"<html>2</html>", "text/html", h2, backend=be)
    out = ds_svc.accept_design(db, pid, sid, "agent:GRPH-A2", "dana")
    assert out["status"] == "accepted"
    gov = [d for d in ds_svc._prompts(db, sid, "home") if d.governing]
    assert [d.content_hash for d in gov] == [h2]
    assert "Header is purple" in ds_svc._chunk_text(gov[0])


def test_canvas_orphan_and_binding(db, pid, world, be):
    _, sid = world
    h, canvas = _ready(db, pid, sid, be)
    other = ds_svc.put_canvas(db, pid, sid, "home", b"drawn from old", "text/html", "deadbeef",
                              backend=be)
    assert other["orphan"] and not other["bound"]
    assert db.get(DesignSet, sid).screens[0].canvas_hash == canvas  # previous canvas stays
    assert ds_svc.read_canvas(db, pid, sid, other["content_hash"], backend=be)[0] == b"drawn from old"
    again = ds_svc.put_canvas(db, pid, sid, "home", b"drawn from old", "text/html", h, backend=be)
    assert again["bound"] and again["content_hash"] == other["content_hash"]
    assert db.get(DesignSet, sid).screens[0].canvas_hash == other["content_hash"]


def test_findings_corrections_and_recompile(db, pid, world, be):
    _, sid = world
    h, canvas = _ready(db, pid, sid, be)
    with pytest.raises(errors.Validation, match="compiled"):
        ds_svc.file_finding(db, pid, sid, "home", "prompt_defect", "x", canvas, actor=COMPILER)
    f = ds_svc.file_finding(db, pid, sid, "home", "prompt_defect", "Header is purple",
                            canvas, actor="agent:GRPH-A3")
    assert db.get(DesignSet, sid).screens[0].canvas_hash == ""  # superseded
    dup = ds_svc.file_finding(db, pid, sid, "home", "prompt_defect", "Header is purple",
                              canvas, actor="agent:GRPH-A4")
    assert dup["duplicate"] and dup["id"] == f["id"]
    h2 = f["recompiled"]["prompt_hash"]  # the screen recompiled once the correction stored
    assert h2 != h and _compile(db, pid, sid)[0]["prompt_hash"] == h2
    assert db.get(Document, f["recompiled"]["document_id"]).created_by == COMPILER
    assert len(list(db.scalars(select(DesignCorrection)))) == 1
    assert ds_svc.resolve_finding(db, pid, f["id"], "applied")["status"] == "applied"


def test_authority_conflict_records_winner_and_spec_gap_rules(db, pid, world, be):
    _, sid = world
    h, canvas = _ready(db, pid, sid, be)
    with pytest.raises(errors.Validation, match="winner"):
        ds_svc.file_finding(db, pid, sid, "home", "authority_conflict", "fake counts", canvas,
                            actor="agent:GRPH-A3")
    a = ds_svc.file_finding(db, pid, sid, "home", "authority_conflict", "fake counts", canvas,
                            winner="prd", actor="agent:GRPH-A3")
    assert a["winner"] == "prd"
    assert "draw the absence" in db.get(DesignCorrection, a["correction_id"]).text
    with pytest.raises(errors.Validation, match="heading"):
        ds_svc.file_finding(db, pid, sid, "home", "spec_gap", "nobody says", actor="agent:GRPH-A3")
    g = ds_svc.file_finding(db, pid, sid, "home", "spec_gap", "Spacing is silent on mobile",
                            actor="agent:GRPH-A3")
    assert g["status"] == "open" and g["correction_id"] is None
    ds_svc.file_finding(db, pid, sid, "home", "spec_gap", "Spacing is silent on mobile",
                        actor="agent:GRPH-A5")
    assert len(ds_svc._findings(db, sid)) == 2
    _compile(db, pid, sid)  # compile keeps working with an open spec_gap
    ds_svc.put_canvas(db, pid, sid, "home", b"x", "text/html", "other", backend=be)
    with pytest.raises(errors.Validation, match="reason"):
        ds_svc.resolve_finding(db, pid, g["id"], "withdrawn")


def test_accept_design_refusals_and_success(db, pid, world, be):
    prd, sid = world
    with pytest.raises(errors.Validation, match="no accepted prompt"):
        ds_svc.accept_design(db, pid, sid, "agent:GRPH-A2", "dana")
    h, canvas = _ready(db, pid, sid, be)
    gap = ds_svc.file_finding(db, pid, sid, "home", "spec_gap", "Spacing is silent on mobile",
                              actor="agent:GRPH-A3")
    with pytest.raises(errors.Validation, match="open findings"):
        ds_svc.accept_design(db, pid, sid, "agent:GRPH-A2", "dana")
    ds_svc.resolve_finding(db, pid, gap["id"], "withdrawn", "covered elsewhere")
    with pytest.raises(errors.Validation, match="compiled"):
        ds_svc.accept_design(db, pid, sid, "agent:GRPH-A2", COMPILER)
    with pytest.raises(errors.Validation, match="no person"):
        ds_svc.accept_design(db, pid, sid, "agent:GRPH-A2")
    out = ds_svc.accept_design(db, pid, sid, "agent:GRPH-A2", "dana")
    assert out["status"] == "accepted" and out["accepted_by"] == "dana"
    assert docs.search_docs(db, pid, "teal")["results"][0]["kind"] == "prd"
    hits = docs.search_docs(db, pid, "teal review record", include_design=True)["results"]
    assert {r["kind"] for r in hits} >= {"design_prompt", "design_review"}
    assert all(r["kind"] != "canvas" for r in hits)
    # a later compile is a candidate and does not unseat the accepted prompt
    ds_svc.file_finding(db, pid, sid, "home", "design_edit", "Bigger header", canvas,
                        actor="agent:GRPH-A3")
    _compile(db, pid, sid)
    assert db.get(DesignSet, sid).status == "accepted"
    assert any(r["kind"] == "design_prompt" and r["content_hash"] for r in
               docs.search_docs(db, pid, "teal", include_design=True)["results"])


def test_accept_design_refused_when_prd_hash_is_stale(db, pid, world, be):
    prd, sid = world
    _ready(db, pid, sid, be)
    prd_svc.request_rebaseline(db, prd, reason_type="correction", reason="x", requested_by="agent")
    prd_svc.update_prd(db, prd.id, body=BODY.replace("Eight", "Four"))
    approve(db, prd_svc.get_prd(db, prd.id))
    with pytest.raises(errors.Validation, match="stale"):
        ds_svc.accept_design(db, pid, sid, "agent:GRPH-A2", "dana")


def test_rebaseline_request_withdraws_spec_gaps(db, pid, world, be):
    prd, sid = world
    _ready(db, pid, sid, be)
    g = ds_svc.file_finding(db, pid, sid, "home", "spec_gap", "Spacing is silent on mobile",
                            actor="agent:GRPH-A3")
    prd_svc.request_rebaseline(db, prd, reason_type="correction", reason="x", requested_by="agent")
    assert [f.status for f in ds_svc._findings(db, sid)] == ["withdrawn"]
    assert g["id"] == ds_svc._findings(db, sid)[0].id


def test_frozen_set_refuses_compile_and_canvas(db, pid, world, be):
    prd, sid = world
    ds_svc.freeze_for_prd(db, prd.id)
    with pytest.raises(errors.Validation, match="frozen"):
        _compile(db, pid, sid)
    with pytest.raises(errors.Validation, match="frozen"):
        ds_svc.put_canvas(db, pid, sid, "home", b"x", "text/html", "h", backend=be)


def test_mcp_accept_prompt_from_a_bare_credential_is_a_relay(client, auth, db, pid, world):
    """Over MCP a call with no agent_id is `key:<name>`; it relays a named person or nothing."""
    from tests.test_documents import _mcp

    _, sid = world
    _compile(db, pid, sid)
    key = client.post("/api/api-keys", json={"name": "fleetkey"}, headers=auth).json()["plaintext"]
    h = {"X-API-Key": key}

    def call(args, n):
        return _mcp(client, h, "accept_prompt",
                    {"design_set_id": sid, "screen": "home", "project_id": pid, **args}, n).json()

    for n, args in enumerate([{}, {"accepted_by": "key:fleetkey"}], 1):
        body = call(args, n)
        assert "error" in body or body["result"].get("isError"), body
    ok = call({"accepted_by": "dana"}, 3)["result"]["structuredContent"]
    assert (ok["accepted_by"], ok["relayed_by"]) == ("dana", "key:fleetkey")
