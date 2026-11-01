"""PRD-P63 §6 primitive 7/7 (GRPH-1274): `accept.human_clause_blocks`, the `human_only_clause`
primitive bound to a library rule.

An acceptance clause flagged `{needs: human-only}` refuses an agent's sign-off and blocks the
item, naming the clause. A person (`capabilities.human`) is not refused. Unflagged prose never
counts. On/off comes from the layered resolution.
"""
from app.services import fleet
from app.services import rule_pack as P
from app.services import rule_toggles as T
from app.services import rules as R
from tests.test_fleet_adversarial_gate import (  # noqa: F401  (fixtures + helpers)
    SABOTAGE, _ok, _ready_for_review, _rpc, db, key, proj)
from tests.test_rule_pack import FIX, TRUST, build

RULE = "accept.human_clause_blocks"
CLAUSE = "a real exhausted vendor account walk"
FLAGGED = f"## Acceptance\n- {CLAUSE} {{needs: human-only}}\n"
PROSE_CLAUSE = "a human reads the walk and the human-only part is prose"
PROSE = f"## Acceptance\n- {PROSE_CLAUSE}\n"


def _library(monkeypatch, *, on: bool):
    monkeypatch.setattr(T, "LIBRARY", {})
    T.register(T.LibraryRule(
        id=RULE, stage="review", title="Human clause", summary="s",
        enforcement="enforced", enforcer=R.Enforcer("graphban_source", "app/services/fleet.py"),
        default_on=on))


def _item(client, key, description, reviewer_caps=None):
    item, reviewer = _ready_for_review(client, key, effort=0)
    _ok(client, key, "update_item", {"id": item, "description": description})
    if reviewer_caps:
        reviewer = _ok(client, key, "register_agent", {
            "branch": "gb/test", "label": "p", "role_hint": "reviewer",
            "capabilities": reviewer_caps})
    return item, reviewer


def _sign(client, key, item, reviewer, clause=CLAUSE):
    return _rpc(client, key, "sign_off", {
        "id": item, "agent_id": reviewer["agent_id"],
        "evidence": [SABOTAGE, {"kind": "test", "detail": f"{clause}: passed"}]})


def _status(client, key, item):
    return _ok(client, key, "get_item_details", {"id": item})


def test_on_refuses_an_agent_blocks_the_item_and_names_the_clause(client, key, monkeypatch):
    _library(monkeypatch, on=True)
    item, reviewer = _item(client, key, FLAGGED)

    err = _sign(client, key, item, reviewer)["structuredContent"]["error"]

    assert err["code"] == "conflict" and RULE in err["message"]
    got = _status(client, key, item)
    assert got["status"] == "blocked" and CLAUSE in got["blocker"]


def test_on_a_person_is_not_refused(client, key, monkeypatch):
    _library(monkeypatch, on=True)
    item, reviewer = _item(client, key, FLAGGED, {"human": True, "instance": "p"})

    out = _sign(client, key, item, reviewer)["structuredContent"]

    assert out["status"] == "done"


def test_an_unflagged_clause_never_counts(client, key, monkeypatch):
    _library(monkeypatch, on=True)
    item, reviewer = _item(client, key, PROSE)

    assert _sign(client, key, item, reviewer, PROSE_CLAUSE)["structuredContent"]["status"] == "done"


def test_off_does_not_block(client, key, monkeypatch):
    _library(monkeypatch, on=False)
    item, reviewer = _item(client, key, FLAGGED)

    err = _sign(client, key, item, reviewer)["structuredContent"]["error"]

    assert RULE not in err["message"]
    assert _status(client, key, item)["status"] == "review"


def test_an_empty_library_is_no_gate(client, key, monkeypatch):
    monkeypatch.setattr(T, "LIBRARY", {})
    item, reviewer = _item(client, key, FLAGGED)

    err = _sign(client, key, item, reviewer)["structuredContent"]["error"]

    assert RULE not in err["message"]
    assert _status(client, key, item)["status"] == "review"


def test_the_refusal_names_its_rule(client, key, monkeypatch, db):
    from app.models import Event

    _library(monkeypatch, on=True)
    item, reviewer = _item(client, key, FLAGGED)
    _sign(client, key, item, reviewer)

    ev = db.query(Event).filter(Event.action == "refusal").order_by(Event.id.desc()).first()
    assert ev is not None and ev.meta["rule_id"] == RULE


def test_the_primitive_is_published_and_registered():
    prim = P.PRIMITIVES["human_only_clause"]
    assert prim.level == "enforced" and fleet.HUMAN_CLAUSE_RULE == RULE
    assert RULE in R.LIBRARY_BOUND_RULE_IDS and RULE in fleet.SIGN_OFF_RULES


def test_the_pinned_fixture_template_loads_enforced():
    saved = (dict(T.LIBRARY), P.CURRENT)
    try:
        pack = P.load(*build(FIX / "api1"), trusted_keys=TRUST)
        assert RULE not in pack.refused
        t = pack.templates[RULE]
        assert t["primitive"] == "human_only_clause" and t["claims"] == "enforced"
        assert P._library_rule(RULE, t, "").enforcement == "enforced"
    finally:
        T.LIBRARY.clear()
        T.LIBRARY.update(saved[0])
        P.CURRENT = saved[1]
