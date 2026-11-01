"""PRD-P63 §6 primitive 4/7 (GRPH-1271): `build.engine_evidence`, the `evidence_required`
primitive bound to a library rule.

With the rule ON a migration-class item (an `alembic/versions` touchpoint) moves to review only
with a test receipt recording a FULL Postgres suite run. A partial run does not satisfy it.
On/off and params come from the layered resolution.
"""
from app.services import items as items_svc
from app.services import rule_pack as P
from app.services import rule_toggles as T
from app.services import rules as R
from tests.test_fleet_adversarial_gate import (  # noqa: F401  (fixtures + helpers)
    CUT_FROM, _ok, _rpc, db, key, proj)
from tests.test_rule_pack import FIX, TRUST, build

RULE = "build.engine_evidence"
MIGRATION = ["backend/alembic/versions/0999_x.py"]
FULL = {"kind": "test", "detail": "Postgres full suite: 4210 passed"}
PARTIAL = {"kind": "test", "detail": "Postgres on 4 files only: 61 passed"}


def _library(monkeypatch, *, on: bool, params: dict | None = None):
    monkeypatch.setattr(T, "LIBRARY", {})
    T.register(T.LibraryRule(
        id=RULE, stage="build", title="Engine evidence", summary="s",
        enforcement="enforced", enforcer=R.Enforcer("graphban_source", "app/services/items.py"),
        default_on=on,
        default_params=params if params is not None else
        {"path": "alembic/versions/", "engine": "postgres"}))


def _held(client, key, touchpoints):
    worker = _ok(client, key, "register_agent",
                 {"branch": "gb/test", "label": "w", "capabilities": {"vendor": "anthropic"}})
    _ok(client, key, "create_item", {"title": "some work", "status": "next", "effort": 0})
    c = _ok(client, key, "claim_next", {"agent_id": worker["agent_id"]})
    item = c["item"]["id"]
    _ok(client, key, "update_item",
        {"id": item, "agent_id": worker["agent_id"], "touchpoints": touchpoints,
         "evidence": [{"kind": "note",
                       "detail": f"gbfleet: branch cut from {CUT_FROM} (`gb/test`)"}]})
    return item, worker["agent_id"]


def _to_review(client, key, item, agent, evidence=None):
    args = {"id": item, "status": "review", "agent_id": agent}
    if evidence:
        args["evidence"] = evidence
    return _rpc(client, key, "update_item", args)


def _refusal(res):
    return res["structuredContent"]["error"]


def test_on_refuses_a_migration_item_with_no_engine_receipt(client, key, monkeypatch):
    _library(monkeypatch, on=True)
    item, agent = _held(client, key, MIGRATION)

    err = _refusal(_to_review(client, key, item, agent))

    assert err["code"] == "conflict" and RULE in err["message"]
    assert _ok(client, key, "get_item_details", {"id": item})["status"] == "in_progress"


def test_on_refuses_a_partial_suite_receipt(client, key, monkeypatch):
    _library(monkeypatch, on=True)
    item, agent = _held(client, key, MIGRATION)

    err = _refusal(_to_review(client, key, item, agent, [PARTIAL]))

    assert RULE in err["message"] and "full" in err["message"]


def test_on_refuses_a_receipt_that_denies_the_run(client, key, monkeypatch):
    _library(monkeypatch, on=True)
    item, agent = _held(client, key, MIGRATION)

    assert RULE in _refusal(_to_review(
        client, key, item, agent, [{"kind": "test", "detail": "Postgres full suite NOT run"}]
    ))["message"]


def test_on_allows_a_full_suite_receipt(client, key, monkeypatch):
    _library(monkeypatch, on=True)
    item, agent = _held(client, key, MIGRATION)

    out = _ok(client, key, "update_item",
              {"id": item, "status": "review", "agent_id": agent, "evidence": [FULL]})

    assert out["status"] == "review"


def test_on_leaves_a_non_migration_item_alone(client, key, monkeypatch):
    _library(monkeypatch, on=True)
    item, agent = _held(client, key, ["backend/app/services/items.py"])

    assert _ok(client, key, "update_item",
               {"id": item, "status": "review", "agent_id": agent})["status"] == "review"


def test_off_allows_the_same_item_with_no_receipt(client, key, monkeypatch):
    _library(monkeypatch, on=False)
    item, agent = _held(client, key, MIGRATION)

    assert _ok(client, key, "update_item",
               {"id": item, "status": "review", "agent_id": agent})["status"] == "review"


def test_an_empty_library_is_no_gate(client, key, monkeypatch):
    monkeypatch.setattr(T, "LIBRARY", {})
    item, agent = _held(client, key, MIGRATION)

    assert _ok(client, key, "update_item",
               {"id": item, "status": "review", "agent_id": agent})["status"] == "review"


def test_a_project_override_turns_it_on_through_the_resolution(client, key, monkeypatch, db,
                                                              proj):
    from app.models import User

    _library(monkeypatch, on=False)
    T.set_project_toggle(db, proj, RULE, {"on": True}, actor=db.query(User).first(),
                         reason="migrations need the real engine")
    db.commit()
    item, agent = _held(client, key, MIGRATION)

    assert RULE in _refusal(_to_review(client, key, item, agent))["message"]


def test_the_refusal_names_its_rule(client, key, monkeypatch, db):
    from app.models import Event

    _library(monkeypatch, on=True)
    item, agent = _held(client, key, MIGRATION)
    _to_review(client, key, item, agent)

    ev = db.query(Event).filter(Event.action == "refusal").order_by(Event.id.desc()).first()
    assert ev is not None and ev.meta["rule_id"] == RULE


def test_the_primitive_is_published_with_its_params():
    prim = P.PRIMITIVES["evidence_required"]
    assert prim.level == "enforced"
    assert prim.params["path"].type == "str" and prim.params["engine"].type == "str"
    assert items_svc.ENGINE_EVIDENCE_RULE == RULE
    assert RULE in R.LIBRARY_BOUND_RULE_IDS


def test_the_pinned_fixture_template_loads_enforced():
    saved = (dict(T.LIBRARY), P.CURRENT)
    try:
        pack = P.load(*build(FIX / "api1"), trusted_keys=TRUST)
        assert RULE not in pack.refused
        t = pack.templates[RULE]
        assert t["primitive"] == "evidence_required" and t["claims"] == "enforced"
        rule = P._library_rule(RULE, t, "")
        assert rule.enforcement == "enforced" and rule.default_on is False
    finally:
        T.LIBRARY.clear()
        T.LIBRARY.update(saved[0])
        P.CURRENT = saved[1]
