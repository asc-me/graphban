"""Refusals name the rule that refused them (GRPH-1158 / PRD-P61 §6)."""
import importlib.util
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app import errors
from app.db import SessionLocal
from app.models import Event
from app.services import rules as R

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "refusal_ratchet.py"


def _ratchet():
    spec = importlib.util.spec_from_file_location("refusal_ratchet", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _events(*actions):
    db = SessionLocal()
    try:
        return list(db.scalars(select(Event).where(Event.action.in_(actions))))
    finally:
        db.close()


def test_refused_is_an_http_exception_with_an_optional_rule():
    e = errors.Refused(409, "no", rule_id="gate.approval_earned_by_grill")
    assert isinstance(e, HTTPException) and e.status_code == 409
    assert errors.Refused(403, "no").rule_id is None
    assert e.mcp_code == "conflict" and errors.Refused(403, "x").mcp_code == "unauthorized"


def test_approval_by_update_is_refused_with_its_rule_and_recorded(client, auth):
    prd = client.post("/api/prds", json={"title": "P", "project_id": "core"}, headers=auth).json()
    r = client.patch(f"/api/prds/{prd['id']}", json={"status": "approved"}, headers=auth)
    assert r.status_code == 409
    assert r.json()["rule_id"] == "gate.approval_earned_by_grill"
    [ev] = _events("refusal")
    assert ev.meta["rule_id"] == "gate.approval_earned_by_grill"
    # Route TEMPLATE, not the concrete path; attached to the PRD it targeted.
    assert ev.meta["where"] == "PATCH /api/prds/{prd_id}"
    assert (ev.target_type, ev.target_id) == ("prd", prd["id"])
    assert ev.actor_type == "user"


def test_an_unmapped_refusal_is_recorded_as_such(client, auth):
    from app.main import app

    @app.get("/api/_t/refused/{item_id}")
    def _boom(item_id: str):
        raise errors.Refused(403, "policy, but no rule yet")

    r = client.get("/api/_t/refused/X-1")
    assert r.status_code == 403 and r.json()["rule_id"] is None
    [ev] = _events("refusal_unmapped")
    assert ev.meta["where"] == "GET /api/_t/refused/{item_id}"
    assert "rule_id" not in ev.meta and ev.meta["status"] == 403


def test_not_found_is_not_a_refusal(client, auth):
    assert client.patch("/api/prds/nope", json={"title": "x"}, headers=auth).status_code == 404
    assert _events("refusal", "refusal_unmapped") == []


def test_every_rule_id_cited_in_code_exists_in_the_registry():
    cited = set(_ratchet().scan()["rule_ids"])
    assert cited, "the scan found no rule_id at all — it is not looking"
    assert cited <= set(R.RULES_BY_ID), cited - set(R.RULES_BY_ID)


def test_ratchet_counts_a_refusal_without_a_rule_and_a_bare_409(tmp_path):
    m = _ratchet()
    (tmp_path / "a.py").write_text(
        "raise Refused(409, 'x', rule_id='a.b')\n"
        "raise Refused(409, 'x')\n"
        "raise Refused(409, 'x', rule_id=None)\n"
        "raise HTTPException(409, 'x')\n"
        "raise HTTPException(status_code=403, detail='x')\n"
        "raise HTTPException(404, 'x')\n")
    c = m.scan(tmp_path)
    assert (c["refusal_sites"], c["naming_a_rule"]) == (3, 1)
    assert c["refused_without_rule_id"] == 2 and c["bare_403_409"] == 2


def test_ratchet_fails_when_a_count_goes_up(monkeypatch):
    m = _ratchet()
    assert m.check(m.scan()) == []
    monkeypatch.setitem(m.BASELINE, "bare_403_409", 0)
    assert m.check(m.scan())


def test_publish_writes_nothing_when_the_ratchet_fails(tmp_path, monkeypatch):
    m = _ratchet()
    out = tmp_path / "c.json"
    assert m.main(["publish", str(out)]) == 0 and out.exists()
    out.unlink()
    monkeypatch.setitem(m.BASELINE, "bare_403_409", 0)
    assert m.main(["publish", str(out)]) == 1 and not out.exists()


@pytest.mark.parametrize("rule", ["review.not_own_work", "review.role_gated_tools"])
def test_mcp_rules_are_registered(rule):
    assert rule in R.RULES_BY_ID


def test_a_rule_id_held_in_a_constant_is_resolved_and_checked(tmp_path):
    """The registry test must see `rule_id=OBSERVE_RULE`, or a typo in that constant's value
    passes it. Sabotage: make `_Resolver` return `...` for a Name — this fails."""
    m = _ratchet()
    (tmp_path / "g.py").write_text(
        "RULE = 'x.from_constant'\n"
        "raise Refused(409, 'x', rule_id=RULE)\n")
    (tmp_path / "h.py").write_text(
        "import g\n"
        "raise Refused(409, 'x', rule_id=g.RULE)\n"
        "raise Refused(409, 'x', rule_id=compute())\n"
        "meta = {'source': 'agent', 'rule_id': RULE_IN_META}\n"
        "RULE_IN_META = 'x.in_meta'\n"
        "body = {'rule_id': exc.rule_id}\n")
    c = m.scan(tmp_path)
    assert c["rule_ids"] == ["x.from_constant", "x.in_meta"]
    assert (c["refusal_sites"], c["naming_a_rule"]) == (3, 2)
    assert c["unresolved"] == ["h.py:3"]


def test_the_real_tree_cites_its_constants_and_resolves_every_rule_id():
    from app.services import gitops, memory

    c = _ratchet().scan()
    assert c["unresolved"] == [], c["unresolved"]
    assert gitops.OBSERVE_RULE in c["rule_ids"]
    assert memory.ADJUDICATION_RULE in c["rule_ids"]


def test_a_refusal_built_for_the_mcp_path_is_a_site():
    """`return _refused(errors.Refused(...))` is a refusal without a `raise`; leaving it out
    of M would let a site dodge the coverage line by not being raised."""
    c = _ratchet().scan()
    assert "gate.decompose_requires_approval" in c["rule_ids"]


@pytest.mark.parametrize("rule", [
    "review.adversarial_evidence", "review.attest_the_head", "review.predicates_compared",
    "review.clause_capability", "review.acceptance_covered", "memory.independent_adjudication",
    "memory.write_access", "memory.retire_window",
])
def test_the_converted_gates_are_registered_and_cited(rule):
    assert rule in R.RULES_BY_ID
    assert rule in _ratchet().scan()["rule_ids"]


def test_the_rejected_lens_sees_typed_refusals():
    from app.services import events

    assert {"refusal", "refusal_unmapped"} <= set(events.REFUSAL_ACTIONS)
    # Old rows stay readable: they are keyed by these.
    assert {"sign_off_refused", "role_refused", "agent_reject_shard"} <= set(
        events.REFUSAL_ACTIONS)


def test_a_typed_refusal_lands_in_its_projects_rejected_lens(client, auth):
    """`PATCH /api/prds/{prd_id}` names no project; the refusal must still be in the PRD's
    project, or Activity shows nothing refused. Sabotage: drop `_target_project` — fails."""
    from app.services import events

    prd = client.post("/api/prds", json={"title": "P", "project_id": "core"}, headers=auth).json()
    client.patch(f"/api/prds/{prd['id']}", json={"status": "approved"}, headers=auth)
    db = SessionLocal()
    try:
        out = events.list_events(db, project_ids=["core"], lens="rejected")
    finally:
        db.close()
    assert [r["action"] for r in out["results"]] == ["refusal"]


def test_a_shard_the_agent_discards_names_the_adjudication_rule(client):
    from app.models import MemoryShard
    from app.services import memory

    db = SessionLocal()
    try:
        shard = MemoryShard(id="s_refusal", text="a candidate", project_id="core",
                            status="candidate")
        db.add(shard)
        db.commit()
        memory.agent_reject(db, shard, origin="agent-x")
    finally:
        db.close()
    [ev] = _events("agent_reject_shard")
    assert ev.meta["rule_id"] == memory.ADJUDICATION_RULE


def test_coverage_absent_is_unpublished_not_zero(tmp_path):
    out = R.refusal_coverage(tmp_path / "missing.json")
    assert out["published"] is False and out["n"] is None and out["m"] is None


def test_coverage_reads_what_publish_wrote(tmp_path):
    out = tmp_path / "data" / "c.json"
    assert _ratchet().main(["publish", str(out), "abc123"]) == 0
    got = R.refusal_coverage(out)
    c = _ratchet().scan()
    assert got["published"] is True and got["commit"] == "abc123"
    assert (got["n"], got["m"]) == (c["naming_a_rule"], c["refusal_sites"])


def test_no_coverage_is_committed_by_a_pr():
    """It is a build artefact (GRPH-1185): gitignored, and nothing tracks it. A tracked copy
    would be a number no build of THIS tree computed, shipped by every package after it."""
    import subprocess

    repo = SCRIPT.parents[1]
    rel = R.REFUSAL_COVERAGE_FILE.relative_to(repo)
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", str(rel)], cwd=repo,
                             capture_output=True)
    ignored = subprocess.run(["git", "check-ignore", "-q", "--no-index", str(rel)], cwd=repo)
    assert tracked.returncode != 0, f"{rel} is committed"
    assert ignored.returncode == 0, f"{rel} is not gitignored"


def test_a_source_tree_reports_unpublished_never_zero():
    """A dev tree or editable install has no file: `published: False`, not 0 of M."""
    if R.REFUSAL_COVERAGE_FILE.exists():
        pytest.skip("a hand-run publish left a file in this checkout")
    got = R.refusal_coverage()
    assert got["published"] is False and got["n"] is None


def test_a_refused_shard_retire_lands_in_the_shards_project(client, auth):
    """A read-only member's retire is a `memory.write_access` refusal on the shard, recorded
    in the shard's project. Sabotage: drop the shard branch of `_target_project` — fails."""
    from app.models import Membership
    from app.services import events

    pid = client.post("/api/projects", json={"name": "Shards"}, headers=auth).json()["id"]
    sid = client.post("/api/memory/shards", json={"text": "pin pg16 in CI", "project_id": pid},
                      headers=auth).json()["id"]
    db = SessionLocal()
    try:
        for m in db.scalars(select(Membership).where(Membership.project_id == pid)):
            m.access = "read"
        db.commit()
    finally:
        db.close()
    r = client.post(f"/api/memory/shards/{sid}/retire", headers=auth)
    assert r.status_code == 403 and r.json()["rule_id"] == "memory.write_access"
    db = SessionLocal()
    try:
        out = events.list_events(db, project_ids=[pid], lens="rejected")
    finally:
        db.close()
    assert [(x["action"], x["target_type"], x["target_id"]) for x in out["results"]] == [
        ("refusal", "shard", sid)]


def _unmapped(db, n, *, where="GET /api/x", status=403, age_days=0, project_id="core"):
    from datetime import datetime, timedelta, timezone
    ts = datetime.now(timezone.utc) - timedelta(days=age_days)
    db.add_all([Event(ts=ts, actor_type="system", surface="rest", action="refusal_unmapped",
                      project_id=project_id, meta={"where": where, "status": status})
                for _ in range(n)])
    db.commit()


def test_unmapped_groups_count_past_500_and_rank(client, auth):
    db = SessionLocal()
    try:
        _unmapped(db, 600, where="GET /api/big")
        _unmapped(db, 3, where="GET /api/small")
    finally:
        db.close()
    r = client.get("/api/rules/refusals/unmapped?project_id=core", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert [(g["where"], g["count"]) for g in body["groups"]] == [("GET /api/big", 600), ("GET /api/small", 3)]
    assert body["groups"][0]["daily"][-1] == 600 and len(body["groups"][0]["daily"]) == 7
    assert body["groups"][0]["last_at"]


def test_unmapped_window_excludes_older_and_reports_recording_since(client, auth):
    db = SessionLocal()
    try:
        _unmapped(db, 2, age_days=30, where="GET /api/old")
    finally:
        db.close()
    body = client.get("/api/rules/refusals/unmapped?project_id=core", headers=auth).json()
    assert body["groups"] == [] and body["recording_since"]
    wide = client.get("/api/rules/refusals/unmapped?project_id=core&days=60", headers=auth).json()
    assert [g["where"] for g in wide["groups"]] == ["GET /api/old"]


def test_unmapped_recording_since_is_null_when_nothing_recorded(client, auth):
    body = client.get("/api/rules/refusals/unmapped?project_id=core", headers=auth).json()
    assert body["recording_since"] is None and body["groups"] == []


def test_projectless_refusals_are_a_platform_bucket_for_the_owner_only(client, auth):
    from app.services import events as ev
    db = SessionLocal()
    try:
        _unmapped(db, 2, where="tool_x", project_id=None)
        _unmapped(db, 1, where="GET /api/mine")
        owner = ev.unmapped_refusals(db, "core", include_platform=True)
        reader = ev.unmapped_refusals(db, "core", include_platform=False)
    finally:
        db.close()
    assert [(g["where"], g["platform"]) for g in owner["groups"]] == [("tool_x", True), ("GET /api/mine", False)]
    assert [g["where"] for g in reader["groups"]] == ["GET /api/mine"]
