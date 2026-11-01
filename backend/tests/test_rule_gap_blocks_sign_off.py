"""PRD-P63 §6 primitive 3/7 (GRPH-1270): `review.gap_blocks_sign_off`, the `gap_followup_link`
primitive bound to a library rule.

Every builder note marked `done: false` must name a follow-up item (its `url` is that item's
key) before sign-off. On/off comes from the layered resolution, never a constant.
"""
from app.services import fleet
from app.services import rule_pack as P
from app.services import rule_toggles as T
from app.services import rules as R
from tests.test_fleet_adversarial_gate import (  # noqa: F401  (fixtures + helpers)
    SABOTAGE, _ok, _ready_for_review, _rpc, db, key, proj)
from tests.test_rule_pack import FIX, TRUST, build

RULE = "review.gap_blocks_sign_off"


def _library(monkeypatch, *, on: bool):
    monkeypatch.setattr(T, "LIBRARY", {})
    T.register(T.LibraryRule(
        id=RULE, stage="review", title="Gaps need a follow-up", summary="s",
        enforcement="enforced", enforcer=R.Enforcer("graphban_source", "app/services/fleet.py"),
        default_on=on))


def _key(client, key, item_id):
    return _ok(client, key, "get_item_details", {"id": item_id})["id"]


def _gap(client, key, item, **extra):
    _ok(client, key, "update_item", {
        "id": item, "evidence": [{"kind": "note", "detail": "retry path not done",
                                  "done": False, **extra}]})


def _sign(client, key, item, reviewer):
    return _rpc(client, key, "sign_off", {"id": item, "agent_id": reviewer["agent_id"],
                                          "evidence": [SABOTAGE]})


def test_on_refuses_an_unlinked_gap_naming_it(client, key, monkeypatch):
    _library(monkeypatch, on=True)
    item, reviewer = _ready_for_review(client, key, effort=0)
    _gap(client, key, item)

    err = _sign(client, key, item, reviewer)["structuredContent"]["error"]

    assert err["code"] == "conflict" and RULE in err["message"]
    assert "retry path not done" in err["message"]
    assert _ok(client, key, "get_item_details", {"id": item})["status"] == "review"


def test_on_allows_a_gap_linked_to_a_follow_up_item(client, key, monkeypatch):
    _library(monkeypatch, on=True)
    item, reviewer = _ready_for_review(client, key, effort=0)
    followup = _ok(client, key, "create_item", {"title": "retry path", "status": "backlog"})
    _gap(client, key, item, url=_key(client, key, followup["id"]))

    out = _ok(client, key, "sign_off", {"id": item, "agent_id": reviewer["agent_id"],
                                        "evidence": [SABOTAGE]})

    assert out["status"] == "done"


def test_a_link_to_nothing_or_to_itself_does_not_count(client, key, monkeypatch):
    _library(monkeypatch, on=True)
    item, reviewer = _ready_for_review(client, key, effort=0)
    own = _key(client, key, item)
    _gap(client, key, item, url="GRPH-99999")
    _gap(client, key, item, url=own)

    err = _sign(client, key, item, reviewer)["structuredContent"]["error"]

    assert RULE in err["message"] and "2 not-done" in err["message"]


def test_done_notes_need_no_link(client, key, monkeypatch):
    _library(monkeypatch, on=True)
    item, reviewer = _ready_for_review(client, key, effort=0)
    _ok(client, key, "update_item", {"id": item, "evidence": [
        {"kind": "note", "detail": "all of it done", "done": True}]})

    assert _sign(client, key, item, reviewer)["structuredContent"]["status"] == "done"


def test_off_allows_the_unlinked_gap(client, key, monkeypatch):
    _library(monkeypatch, on=False)
    item, reviewer = _ready_for_review(client, key, effort=0)
    _gap(client, key, item)

    assert _sign(client, key, item, reviewer)["structuredContent"]["status"] == "done"


def test_an_empty_library_is_no_gate(client, key, monkeypatch):
    monkeypatch.setattr(T, "LIBRARY", {})
    item, reviewer = _ready_for_review(client, key, effort=0)
    _gap(client, key, item)

    assert _sign(client, key, item, reviewer)["structuredContent"]["status"] == "done"


def test_the_refusal_names_its_rule(client, key, monkeypatch, db):
    from app.models import Event

    _library(monkeypatch, on=True)
    item, reviewer = _ready_for_review(client, key, effort=0)
    _gap(client, key, item)
    _sign(client, key, item, reviewer)

    ev = db.query(Event).filter(Event.action == "refusal").order_by(Event.id.desc()).first()
    assert ev is not None and ev.meta["rule_id"] == RULE


def test_the_primitive_is_published_and_its_template_loads(monkeypatch):
    prim = P.PRIMITIVES["gap_followup_link"]
    assert prim.level == "enforced" and fleet.GAP_BLOCKS_RULE == RULE

    template = (f'id = "{RULE}"\nstage = "Review"\ntitle = "Gaps need a follow-up"\n'
                'summary = "Every not-done note links a follow-up item."\n'
                'primitive = "gap_followup_link"\nclaims = "enforced"\ndefault = "on"\n')
    pack = P.load(*build(FIX / "api1", {"templates/evidence.toml": template}),
                  trusted_keys=TRUST)
    assert RULE not in pack.refused and pack.templates[RULE]["primitive"] == "gap_followup_link"
