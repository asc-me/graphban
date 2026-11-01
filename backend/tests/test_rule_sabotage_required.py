"""PRD-P63 §6 primitive 1/7 (GRPH-1260): `review.sabotage_required`, the `sabotage_receipt`
primitive bound to a library rule.

Closes the GRPH-P62 hole where `adversarial_evidence` passed every effort-0 item by exemption:
with the rule ON a sign-off needs a sabotage the reviewer ran in that call, whose tests failed,
at ANY effort. On/off and params come from the layered resolution, never a constant.
"""
import pytest

from app.services import fleet
from app.services import rule_pack as P
from app.services import rule_toggles as T
from app.services import rules as R
from tests.test_fleet_adversarial_gate import (  # noqa: F401  (fixtures + helpers)
    SABOTAGE, _ok, _ready_for_review, _rpc, db, key, proj)

RULE = "review.sabotage_required"


def _library(monkeypatch, *, on: bool, params: dict | None = None):
    monkeypatch.setattr(T, "LIBRARY", {})
    T.register(T.LibraryRule(
        id=RULE, stage="review", title="Sabotage required", summary="s",
        enforcement="enforced", enforcer=R.Enforcer("graphban_source", "app/services/fleet.py"),
        default_on=on, default_params=params if params is not None else {"min_tests_failed": 1}))


def _refusal(res):
    return res["structuredContent"]["error"]


def test_on_refuses_an_effort_zero_item_without_a_sabotage(client, key, monkeypatch):
    _library(monkeypatch, on=True)
    item, reviewer = _ready_for_review(client, key, effort=0)

    err = _refusal(_rpc(client, key, "sign_off", {"id": item, "agent_id": reviewer["agent_id"]}))

    assert err["code"] == "conflict" and RULE in err["message"]
    assert _ok(client, key, "get_item_details", {"id": item})["status"] == "review"


def test_on_allows_it_with_a_failing_reviewer_sabotage(client, key, monkeypatch):
    _library(monkeypatch, on=True)
    item, reviewer = _ready_for_review(client, key, effort=0)

    out = _ok(client, key, "sign_off",
              {"id": item, "agent_id": reviewer["agent_id"], "evidence": [SABOTAGE]})

    assert out["status"] == "done"


def test_off_allows_the_same_signoff_with_none(client, key, monkeypatch):
    _library(monkeypatch, on=False)
    item, reviewer = _ready_for_review(client, key, effort=0)

    assert _ok(client, key, "sign_off",
               {"id": item, "agent_id": reviewer["agent_id"]})["status"] == "done"


def test_an_empty_library_is_no_gate(client, key, monkeypatch):
    monkeypatch.setattr(T, "LIBRARY", {})
    item, reviewer = _ready_for_review(client, key, effort=0)

    assert _ok(client, key, "sign_off",
               {"id": item, "agent_id": reviewer["agent_id"]})["status"] == "done"


def test_a_sabotage_that_broke_nothing_does_not_satisfy(client, key, monkeypatch):
    _library(monkeypatch, on=True)
    item, reviewer = _ready_for_review(client, key, effort=0)

    err = _refusal(_rpc(client, key, "sign_off", {
        "id": item, "agent_id": reviewer["agent_id"],
        "evidence": [dict(SABOTAGE, tests_failed=0)]}))

    assert RULE in err["message"]


def test_min_tests_failed_two_refuses_a_sabotage_that_failed_one(client, key, monkeypatch):
    _library(monkeypatch, on=True, params={"min_tests_failed": 2})
    item, reviewer = _ready_for_review(client, key, effort=0)

    err = _refusal(_rpc(client, key, "sign_off", {
        "id": item, "agent_id": reviewer["agent_id"],
        "evidence": [dict(SABOTAGE, tests_failed=1)]}))
    assert "at least 2" in err["message"] and "failed 1" in err["message"]

    out = _ok(client, key, "sign_off", {
        "id": item, "agent_id": reviewer["agent_id"],
        "evidence": [dict(SABOTAGE, tests_failed=2)]})
    assert out["status"] == "done"


def test_the_authors_recorded_sabotage_is_not_the_reviewers(client, key, monkeypatch):
    """The rule asks for a sabotage the REVIEWER ran, so one already on the item does not count."""
    _library(monkeypatch, on=True)
    item, reviewer = _ready_for_review(client, key, effort=0)
    _ok(client, key, "update_item", {"id": item, "evidence": [SABOTAGE]})

    err = _refusal(_rpc(client, key, "sign_off", {"id": item, "agent_id": reviewer["agent_id"]}))

    assert RULE in err["message"]


def test_the_refusal_names_its_rule(client, key, monkeypatch, db):
    from app.models import Event

    _library(monkeypatch, on=True)
    item, reviewer = _ready_for_review(client, key, effort=0)
    _rpc(client, key, "sign_off", {"id": item, "agent_id": reviewer["agent_id"]})

    ev = db.query(Event).filter(Event.action == "refusal").order_by(Event.id.desc()).first()
    assert ev is not None and ev.meta["rule_id"] == RULE


def test_the_primitive_is_published_with_its_params():
    prim = P.PRIMITIVES["sabotage_receipt"]
    assert prim.level == "enforced"
    assert prim.params["min_tests_failed"].type == "int"
    assert fleet.SABOTAGE_REQUIRED_RULE == RULE
