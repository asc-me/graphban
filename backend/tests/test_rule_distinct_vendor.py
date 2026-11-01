"""PRD-P63 §6 primitive 2/7 (GRPH-1269): `review.distinct_vendor`, the `reviewer_property`
primitive bound to a library rule.

With the rule ON a sign-off needs a reviewer whose declared vendor differs from the builder's,
compared through the same attribution `reviewer_diversity` reports. An unknown vendor never
satisfies it: absence never reads as clean. On/off and params come from the layered resolution.
"""
from app.services import fleet
from app.services import rule_pack as P
from app.services import rule_toggles as T
from app.services import rules as R
from tests.test_fleet_adversarial_gate import (  # noqa: F401  (fixtures + helpers)
    CUT_FROM, _ok, _rpc, db, key, proj)
from tests.test_rule_pack import FIX, TRUST, build

RULE = "review.distinct_vendor"


def _library(monkeypatch, *, on: bool, params: dict | None = None):
    monkeypatch.setattr(T, "LIBRARY", {})
    T.register(T.LibraryRule(
        id=RULE, stage="review", title="Distinct vendor", summary="s",
        enforcement="enforced", enforcer=R.Enforcer("graphban_source", "app/services/fleet.py"),
        default_on=on,
        default_params=params if params is not None else {"property": "distinct_vendor"}))


def _ready(client, key, *, builder: str | None, reviewer: str | None):
    """An item built by an agent of vendor `builder` and waiting on one of vendor `reviewer`.
    `None` declares no vendor at all."""
    def caps(instance, vendor):
        return {"instance": instance, **({"vendor": vendor} if vendor else {})}

    worker = _ok(client, key, "register_agent",
                 {"branch": "gb/test", "label": "w", "capabilities": caps("w", builder)})
    _ok(client, key, "create_item", {"title": "some work", "status": "next", "effort": 0})
    c = _ok(client, key, "claim_next", {"agent_id": worker["agent_id"]})
    _ok(client, key, "update_item",
        {"id": c["item"]["id"], "agent_id": worker["agent_id"],
         "evidence": [{"kind": "note", "detail": f"gbfleet: branch cut from {CUT_FROM} (`gb/test`)"}]})
    _ok(client, key, "update_item",
        {"id": c["item"]["id"], "status": "review", "agent_id": worker["agent_id"]})
    rev = _ok(client, key, "register_agent",
              {"branch": "gb/test", "label": "r", "role_hint": "reviewer",
               "capabilities": caps("r", reviewer)})
    return c["item"]["id"], rev["agent_id"]


def _refusal(res):
    return res["structuredContent"]["error"]


def _sign(client, key, item, agent):
    return _rpc(client, key, "sign_off", {"id": item, "agent_id": agent})


# ---- both states ------------------------------------------------------------------------

def test_on_refuses_a_same_vendor_signoff(client, key, monkeypatch):
    _library(monkeypatch, on=True)
    item, rev = _ready(client, key, builder="anthropic", reviewer="anthropic")

    err = _refusal(_sign(client, key, item, rev))

    assert err["code"] == "conflict" and RULE in err["message"]
    assert "builder's" in err["message"]
    assert _ok(client, key, "get_item_details", {"id": item})["status"] == "review"


def test_on_allows_a_different_vendor(client, key, monkeypatch):
    _library(monkeypatch, on=True)
    item, rev = _ready(client, key, builder="anthropic", reviewer="openai")

    assert _ok(client, key, "sign_off", {"id": item, "agent_id": rev})["status"] == "done"


def test_off_allows_the_same_vendor(client, key, monkeypatch):
    _library(monkeypatch, on=False)
    item, rev = _ready(client, key, builder="anthropic", reviewer="anthropic")

    assert _ok(client, key, "sign_off", {"id": item, "agent_id": rev})["status"] == "done"


def test_an_empty_library_is_no_gate(client, key, monkeypatch):
    monkeypatch.setattr(T, "LIBRARY", {})
    item, rev = _ready(client, key, builder="anthropic", reviewer="anthropic")

    assert _ok(client, key, "sign_off", {"id": item, "agent_id": rev})["status"] == "done"


def test_a_project_override_turns_it_on_through_the_resolution(client, key, monkeypatch, db,
                                                              proj):
    """State is read through S2a, never a constant: the platform default is off, the project's
    own toggle turns it on."""
    from app.models import User

    _library(monkeypatch, on=False)
    T.set_project_toggle(db, proj, RULE, {"on": True}, actor=db.query(User).first(),
                         reason="cross-vendor review")
    db.commit()
    item, rev = _ready(client, key, builder="anthropic", reviewer="anthropic")

    assert RULE in _refusal(_sign(client, key, item, rev))["message"]


# ---- unknown vendors refuse -------------------------------------------------------------

def test_on_refuses_an_undeclared_reviewer_vendor(client, key, monkeypatch):
    _library(monkeypatch, on=True)
    item, rev = _ready(client, key, builder="anthropic", reviewer=None)

    err = _refusal(_sign(client, key, item, rev))

    assert RULE in err["message"] and "reviewer declared no vendor" in err["message"]


def test_on_refuses_an_undeclared_builder_vendor(client, key, monkeypatch):
    _library(monkeypatch, on=True)
    item, rev = _ready(client, key, builder=None, reviewer="openai")

    err = _refusal(_sign(client, key, item, rev))

    assert RULE in err["message"] and "builder declared no vendor" in err["message"]


def test_on_refuses_two_undeclared_vendors(client, key, monkeypatch):
    _library(monkeypatch, on=True)
    item, rev = _ready(client, key, builder=None, reviewer=None)

    assert RULE in _refusal(_sign(client, key, item, rev))["message"]


def test_vendor_aliases_are_one_vendor(client, key, monkeypatch):
    """`grok` and `xai` are one company, as `reviewer_diversity` already reports."""
    _library(monkeypatch, on=True)
    item, rev = _ready(client, key, builder="grok", reviewer="xai")

    assert RULE in _refusal(_sign(client, key, item, rev))["message"]


def test_an_unknown_property_refuses_rather_than_passes(client, key, monkeypatch):
    _library(monkeypatch, on=True, params={"property": "distinct_planet"})
    item, rev = _ready(client, key, builder="anthropic", reviewer="openai")

    assert "distinct_planet" in _refusal(_sign(client, key, item, rev))["message"]


def test_the_refusal_names_its_rule(client, key, monkeypatch, db):
    from app.models import Event

    _library(monkeypatch, on=True)
    item, rev = _ready(client, key, builder="anthropic", reviewer="anthropic")
    _sign(client, key, item, rev)

    ev = db.query(Event).filter(Event.action == "refusal").order_by(Event.id.desc()).first()
    assert ev is not None and ev.meta["rule_id"] == RULE


# ---- the contract and the pinned fixture ------------------------------------------------

def test_the_primitive_is_published_with_its_params():
    prim = P.PRIMITIVES["reviewer_property"]
    assert prim.level == "enforced"
    assert prim.params["property"].type == "str"
    assert fleet.DISTINCT_VENDOR_RULE == RULE
    assert RULE in R.LIBRARY_BOUND_RULE_IDS


def test_the_pinned_fixture_template_loads_enforced():
    saved = (dict(T.LIBRARY), P.CURRENT)
    try:
        pack = P.load(*build(FIX / "api1"), trusted_keys=TRUST)
        assert RULE not in pack.refused
        t = pack.templates[RULE]
        assert t["primitive"] == "reviewer_property" and t["claims"] == "enforced"
        assert t["params"] == {"property": "distinct_vendor"}
        rule = P._library_rule(RULE, t, "")
        assert rule.enforcement == "enforced" and rule.default_on is False
    finally:
        T.LIBRARY.clear()
        T.LIBRARY.update(saved[0])
        P.CURRENT = saved[1]
