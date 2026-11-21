"""GRPH-1292 — sign_off refuses an unfinished dependency, and claim_review does not hand it out.

GRPH-1266's brief listed GRPH-1291 and the walk was signed off anyway: every predicate
passed, including acceptance coverage, because the description stated no clauses.
claim_cluster already withholds through prioritization.blocked_by. These two paths did not.
"""
from __future__ import annotations

from pathlib import Path

from app.models import Item
from app.services import fleet as fleet_svc
from app.services import links as links_svc
from tests.test_fleet_review import (  # noqa: F401 — fixtures
    _link_pr, _new_item, _ok, _refused, _register, agent_key, db, proj)

WHY = "the walk can land while the blocker is still a doc nit"
HEAD = "b" * 40


def _pair(client, key, db, proj):
    """A review row that depends on a second item still in next."""
    builder = _register(client, key, "worker", label="builder")
    reviewer = _register(client, key, "reviewer", label="reviewer")
    _new_item(client, key, "the walk")
    claimed = _ok(client, key, "claim_next", {"agent_id": builder["agent_id"]})
    assert claimed["claimed"], claimed
    work_id = claimed["item"]["id"]
    _ok(client, key, "update_item", {
        "id": work_id, "status": "review", "agent_id": builder["agent_id"]})
    _link_pr(work_id)
    dep_id = _new_item(client, key, "the blocker")["id"]
    links_svc.create_link(db, a=work_id, b=dep_id, type_="dependency", project_id=proj)
    return work_id, dep_id, reviewer


def test_sign_off_refuses_an_open_dependency_and_claim_review_withholds_it(
        client, agent_key, db, proj):
    """The refusal names the blocking item. A blank waiver is not a waiver.

    Sabotage: delete `raise DependenciesOpen(` from sign_off. This reaches done.
    Sabotage: delete the `WITHHELD_DEPENDENCY` return in review_withholding. claim_review
    hands the row out.
    """
    work_id, dep_id, reviewer = _pair(client, agent_key, db, proj)
    agent = reviewer["agent_id"]

    err = _refused(client, agent_key, "sign_off", {
        "id": work_id, "agent_id": agent,
        "evidence": [{"kind": "note", "detail": "read the diff"}]})
    assert err["code"] == "conflict", err
    assert dep_id in err["message"], err["message"]
    assert "dependencies_done" in err["message"]
    assert _ok(client, agent_key, "get_item_details", {"id": work_id})["status"] == "review"

    blank = _refused(client, agent_key, "sign_off", {
        "id": work_id, "agent_id": agent,
        "waive": {"dependencies_done": "   "}})
    assert dep_id in blank["message"], blank

    got = _ok(client, agent_key, "claim_review", {"agent_id": agent})
    assert got["claimed"] is False, got
    withheld = [w for w in got["withheld"] if w["item"] == work_id]
    assert withheld and withheld[0]["rule"] == fleet_svc.WITHHELD_DEPENDENCY, got
    assert dep_id in withheld[0]["reason"]

    signed = _ok(client, agent_key, "sign_off", {
        "id": work_id, "agent_id": agent,
        "waive": {"dependencies_done": WHY},
        "evidence": [{"kind": "note", "detail": "read the diff"}]})
    assert signed["status"] == "done"
    notes = [e.get("detail") or "" for e in signed["evidence"]
             if e.get("kind") == "note"]
    assert any("dependencies_done waived" in n and WHY in n and dep_id in n for n in notes), notes


def test_a_waived_open_dependency_is_on_the_attestation(client, agent_key, db, proj):
    """With a commit, the waiver is a predicate named dependencies_done, passed, and recorded."""
    work_id, dep_id, reviewer = _pair(client, agent_key, db, proj)
    signed = _ok(client, agent_key, "sign_off", {
        "id": work_id, "agent_id": reviewer["agent_id"], "commit": HEAD,
        "waive": {
            "dependencies_done": WHY,
            "commit_is_not_the_base": "this fixture has no supervisor base",
        },
        "evidence": [{"kind": "note", "detail": "read the diff"}]})
    assert signed["status"] == "done"
    atts = [e for e in signed["evidence"]
            if e.get("kind") == "attestation" and e.get("adapter") == "fleet.sign_off"]
    assert atts, signed["evidence"]
    pred = next(p for p in atts[0]["predicates"] if p["name"] == "dependencies_done")
    assert pred["passed"] is True
    assert pred["waived"] == WHY
    assert dep_id in pred["detail"]


def test_a_done_dependency_does_not_block_sign_off_or_review(client, agent_key, db, proj):
    """The control. A dependency that is done is not an open one."""
    work_id, dep_id, reviewer = _pair(client, agent_key, db, proj)
    dep = db.get(Item, dep_id)
    dep.status = "done"
    db.commit()

    got = _ok(client, agent_key, "claim_review", {"agent_id": reviewer["agent_id"]})
    assert got["claimed"] is True and got["item"]["id"] == work_id, got
    signed = _ok(client, agent_key, "sign_off", {
        "id": work_id, "agent_id": reviewer["agent_id"],
        "evidence": [{"kind": "note", "detail": "read the diff"}]})
    assert signed["status"] == "done"
    assert not any("dependencies_done waived" in (e.get("detail") or "")
                   for e in signed["evidence"])


def test_the_dependency_gate_is_called():
    """Sabotage the CALL. A helper with unit tests is not the gate.

    Deleting `raise DependenciesOpen(` from sign_off, the withhold return, or the
    MCP handler leaves the runtime tests above red and this one too.
    """
    root = Path(__file__).resolve().parents[1]
    fleet = (root / "app" / "services" / "fleet.py").read_text()
    mcp = (root / "app" / "mcp_server.py").read_text()
    assert "raise DependenciesOpen(" in fleet
    assert "return WITHHELD_DEPENDENCY," in fleet
    assert "except fleet_svc.DependenciesOpen" in mcp
