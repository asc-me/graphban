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


def test_accept_set_constraints_is_a_person_and_unblocks_compile(db, pid):
    """GRPH-1304. The flag is not a column a test pokes. A person accepts the current hash."""
    from app.models import DesignAcceptance

    prd = prd_svc.create_prd(db, title="Screens", project_id=pid, body=BODY)
    approve(db, prd)
    sid = ds_svc.open_design(db, pid, prd.id)["id"]
    with pytest.raises(errors.Validation, match="no constraints"):
        ds_svc.accept_set_constraints(db, pid, sid, "agent:GRPH-A2", "dana")
    ds_svc.put_set_constraints(db, pid, sid, "Teal only.", screens=[{"screen": "home"}])
    ds_svc.confirm_tier(db, pid, sid, "home", "BACKED", "route exists")
    with pytest.raises(errors.Validation, match="constraints"):
        _compile(db, pid, sid)
    with pytest.raises(errors.Validation, match="no person"):
        ds_svc.accept_set_constraints(db, pid, sid, "agent:GRPH-A2")
    with pytest.raises(errors.Validation, match="not a person"):
        ds_svc.accept_set_constraints(db, pid, sid, "agent:GRPH-A2", KEY)
    out = ds_svc.accept_set_constraints(db, pid, sid, "agent:GRPH-A2", "dana")
    assert out["constraints_accepted"] is True
    assert (out["accepted_by"], out["relayed_by"]) == ("dana", "agent:GRPH-A2")
    assert out["constraints_hash"] == content_hash("Teal only.")
    row = db.scalars(select(DesignAcceptance).where(DesignAcceptance.kind == "constraints")).one()
    assert (row.person, row.prompt_hash, row.screen) == ("dana", out["constraints_hash"], "")
    _compile(db, pid, sid)
    again = ds_svc.put_set_constraints(db, pid, sid, "Amber only.", screens=[{"screen": "home"}])
    assert again["constraints_accepted"] is False


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


def test_mcp_accept_set_constraints_from_a_bare_credential_is_a_relay(client, auth, db, pid, world):
    """Over MCP a call with no agent_id is `key:<name>`; it relays a named person or nothing."""
    from tests.test_documents import _mcp

    _, sid = world
    db.get(DesignSet, sid).constraints_accepted = False
    db.commit()
    key = client.post("/api/api-keys", json={"name": "conskey"}, headers=auth).json()["plaintext"]
    h = {"X-API-Key": key}

    def call(args, n):
        return _mcp(client, h, "accept_set_constraints",
                    {"design_set_id": sid, "project_id": pid, **args}, n).json()

    for n, args in enumerate([{}, {"accepted_by": "key:conskey"}], 1):
        body = call(args, n)
        assert "error" in body or body["result"].get("isError"), body
    ok = call({"accepted_by": "dana"}, 3)["result"]["structuredContent"]
    assert (ok["accepted_by"], ok["relayed_by"], ok["constraints_accepted"]) == (
        "dana", "key:conskey", True)
    _compile(db, pid, sid)


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


def test_mcp_review_loop_round_trip(client, auth, db, pid, world, tmp_path, monkeypatch):
    """The whole loop over MCP, so the outputSchema probe sees every review tool emit."""
    import base64

    from app.config import settings
    from tests.test_documents import _mcp

    monkeypatch.setattr(settings, "sync_dir", str(tmp_path))
    _, sid = world

    def key(name):
        k = client.post("/api/api-keys", json={"name": name}, headers=auth).json()["plaintext"]
        return {"X-API-Key": k}

    compiler, relay = key("compilerkey"), key("relaykey")
    n = iter(range(1, 100))

    def call(h, name, args):
        body = _mcp(client, h, name, {"project_id": pid, **args}, next(n)).json()
        assert "error" not in body and not body["result"].get("isError"), body
        return body["result"]["structuredContent"]

    ph = call(compiler, "compile_prompts", {"design_set_id": sid})["prompts"][0]["prompt_hash"]
    call(relay, "accept_prompt", {"design_set_id": sid, "screen": "home", "accepted_by": "dana"})
    canvas = call(relay, "put_canvas", {
        "design_set_id": sid, "screen": "home", "mime": "text/html", "prompt_hash": ph,
        "bytes_b64": base64.b64encode(b"<html>mcp</html>").decode()})["content_hash"]
    f = call(relay, "file_finding", {"design_set_id": sid, "screen": "home",
                                     "kind": "prompt_defect", "text": "Header is purple",
                                     "canvas_hash": canvas})
    assert f["recompiled"]["prompt_hash"] != ph
    assert call(relay, "resolve_finding",
                {"finding_id": f["id"], "status": "applied"})["status"] == "applied"
    ph2 = f["recompiled"]["prompt_hash"]
    call(relay, "accept_prompt", {"design_set_id": sid, "screen": "home", "accepted_by": "dana"})
    call(relay, "put_canvas", {
        "design_set_id": sid, "screen": "home", "mime": "text/html", "prompt_hash": ph2,
        "bytes_b64": base64.b64encode(b"<html>mcp2</html>").decode()})
    out = call(relay, "accept_design", {"design_set_id": sid, "accepted_by": "dana"})
    assert (out["status"], out["accepted_by"]) == ("accepted", "dana")


def _zip(files: dict) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    return buf.getvalue()


def test_zip_canvas_records_members_and_changes(db, pid, world, be):
    _, sid = world
    h = _compile(db, pid, sid)[0]["prompt_hash"]
    ds_svc.accept_prompt(db, pid, sid, "home", "agent:GRPH-A2", "dana")
    a = ds_svc.put_canvas(db, pid, sid, "home",
                          _zip({"home.dc.html": "a", "ladder.dc.html": "b"}),
                          "application/zip", h, backend=be, root="home.dc.html")
    assert a["bound"] and a["root"] == "home.dc.html"
    assert [m["path"] for m in a["members"]] == ["home.dc.html", "ladder.dc.html"]
    b = ds_svc.put_canvas(db, pid, sid, "home",
                          _zip({"home.dc.html": "a", "ladder.dc.html": "B"}),
                          "application/zip", h, backend=be, root="home.dc.html")
    assert b["changed"] == ["ladder.dc.html"] and b["removed"] == []
    with pytest.raises(errors.Validation, match="not a member"):
        ds_svc.put_canvas(db, pid, sid, "home", _zip({"x": "1", "y": "2"}),
                          "application/zip", h, backend=be, root="z")
    with pytest.raises(errors.Validation, match="not a zip"):
        ds_svc.put_canvas(db, pid, sid, "home", b"<html/>", "text/html", h, backend=be,
                          root="x")


def test_zip_canvas_after_a_single_file_canvas_reports_removed(db, pid, world, be):
    """A zip replacing a single-file canvas: every member is new, so every member changed. A zip
    dropping a file lists it in `removed`."""
    _, sid = world
    h, _ = _ready(db, pid, sid, be)  # a single-file canvas is current
    a = ds_svc.put_canvas(db, pid, sid, "home", _zip({"home.dc.html": "a", "ladder.dc.html": "b"}),
                          "application/zip", h, backend=be, root="home.dc.html")
    assert a["bound"] and a["changed"] == ["home.dc.html", "ladder.dc.html"]
    assert a["removed"] == []
    b = ds_svc.put_canvas(db, pid, sid, "home", _zip({"home.dc.html": "a2"}),
                          "application/zip", h, backend=be)
    assert b["root"] == "home.dc.html"  # one file: it is the root
    assert b["changed"] == ["home.dc.html"] and b["removed"] == ["ladder.dc.html"]
    screen = ds_svc.get_design_set(db, pid, sid)["screens"][0]
    assert screen["root"] == "home.dc.html"
    assert [m["path"] for m in screen["members"]] == ["home.dc.html"]


def test_finding_names_a_member_and_an_edit_elsewhere_is_visible(db, pid, world, be):
    """The review miss: the finding is about Ladder, the next export edits Harness only."""
    _, sid = world
    h = _compile(db, pid, sid)[0]["prompt_hash"]
    ds_svc.accept_prompt(db, pid, sid, "home", "agent:GRPH-A2", "dana")
    first = _zip({"harness.dc.html": "h1", "ladder.dc.html": "l1"})
    c1 = ds_svc.put_canvas(db, pid, sid, "home", first, "application/zip", h, backend=be,
                           root="harness.dc.html")["content_hash"]
    with pytest.raises(errors.Validation, match="not a file of canvas"):
        ds_svc.file_finding(db, pid, sid, "home", "design_edit", "Ladder rungs overlap", c1,
                            actor="agent:GRPH-A3", member="nope.dc.html")
    f = ds_svc.file_finding(db, pid, sid, "home", "design_edit", "Ladder rungs overlap", c1,
                            actor="agent:GRPH-A3", member="ladder.dc.html")
    assert f["member"] == "ladder.dc.html" and f["member_hash"]
    # the correction recompiled the screen: accept it and export against the new hash
    h2 = f["recompiled"]["prompt_hash"]
    ds_svc.accept_prompt(db, pid, sid, "home", "agent:GRPH-A2", "dana")
    out = ds_svc.put_canvas(db, pid, sid, "home",
                            _zip({"harness.dc.html": "h2", "ladder.dc.html": "l1"}),
                            "application/zip", h2, backend=be, root="harness.dc.html")
    assert out["findings"] == [{"finding_id": f["id"], "member": "ladder.dc.html",
                                "member_changed": False, "member_present": True,
                                "changed_elsewhere": ["harness.dc.html"]}]
    drift = ds_svc.get_design_set(db, pid, sid)["screens"][0]["findings"][0]["drift"]
    assert drift["member_changed"] is False and drift["changed_elsewhere"] == ["harness.dc.html"]
    # the named member read back from the reviewed canvas
    assert ds_svc.read_canvas(db, pid, sid, c1, backend=be, member="ladder.dc.html")[0] == b"l1"
    # fixed in the member it names: visible as such
    fixed = ds_svc.put_canvas(db, pid, sid, "home",
                              _zip({"harness.dc.html": "h2", "ladder.dc.html": "l2"}),
                              "application/zip", h2, backend=be, root="harness.dc.html")
    assert fixed["findings"][0]["member_changed"] is True
    ds_svc.resolve_finding(db, pid, f["id"], "applied")
    acc = ds_svc.accept_design(db, pid, sid, "agent:GRPH-A2", "dana")
    assert acc["screens"][0]["root"] == "harness.dc.html"
    assert acc["screens"][0]["findings"][0]["drift"]["member_changed"] is True


def test_zip_canvas_bounds_entries_and_decompressed_size(db, pid, world, be):
    """A zip bomb: a few KB that expands to many MB is refused while it is read, whatever the
    archive declares, and so is a zip with too many entries."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("home.dc.html", b"\0" * (4 * 1024 * 1024))
    bomb = buf.getvalue()
    assert len(bomb) < 64 * 1024
    with pytest.raises(errors.Validation, match="expands past"):
        ds_svc._zip_members(bomb, max_bytes=1024 * 1024)
    with pytest.raises(errors.Validation, match="at most 2"):
        ds_svc._zip_members(_zip({"a": "1", "b": "2", "c": "3"}), max_members=2)
    _, sid = world
    h = _compile(db, pid, sid)[0]["prompt_hash"]
    ds_svc.accept_prompt(db, pid, sid, "home", "agent:GRPH-A2", "dana")
    many = _zip({f"f{i}.html": "x" for i in range(ds_svc.ZIP_MAX_MEMBERS + 1)})
    with pytest.raises(errors.Validation, match="files; at most"):
        ds_svc.put_canvas(db, pid, sid, "home", many, "application/zip", h, backend=be)
