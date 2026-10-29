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
