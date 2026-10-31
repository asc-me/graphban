"""PRD-62 S4 (GRPH-1250): routing claims over entitlement headroom.

Pins acceptance 5 (the entitlement resetting sooner is chosen, and the attempt record says so)
and 6 (a build that would leave no reviewer-eligible vendor is not scheduled), D10 claim shaping,
and the post-grill decision: a reserved claim that fails ends its reservation at once.
"""
from datetime import datetime, timedelta, timezone

import pytest

from test_entitlements import _HOLDER, _ev, _ok, _url, hosted, org  # noqa: F401  (fixtures)


def _ent(client, org, vendor, *, reset=None, kind="api_key"):
    body = {"vendor": vendor, "kind": kind, "secret_ref": "KEY_" + vendor.upper()}
    if reset:
        body["next_reset_at"] = reset.isoformat()
    r = client.post(_url(org), json=body, headers=org["owner"])
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _runner(client, org, ents, vendor="anthropic"):
    out = _ok(client, org["key"], "register_agent",
              {"label": f"r-{vendor}", "entitlements": ents,
               "capabilities": {"vendor": vendor, "model": "m"}})
    return out["agent_id"]


def _report(client, org, ent, agent, event, **extra):
    return _ok(client, org["key"], "report_entitlement_event",
               {"id": ent, "event": event, "agent_id": agent, **extra})


def _item(title="work"):
    from app.db import SessionLocal
    from app.services import items

    db = SessionLocal()
    try:
        return items.create_item(db, title=title, status="next", project_id=_P["id"]).id
    finally:
        db.close()


_P: dict = {}


@pytest.fixture()
def world(client, org):
    _P["id"] = org["project"]["id"]
    return org


def _claim(agent, item_id=None):
    from app.db import SessionLocal
    from app.services import items

    db = SessionLocal()
    try:
        got = (items.claim_item(db, item_id, agent) if item_id
               else items.claim_next(db, agent, _P["id"]))
        return got.id if got else None
    finally:
        db.close()


def _decisions(item_id):
    from app.db import SessionLocal
    from app.services import routing

    db = SessionLocal()
    try:
        return routing.attempts(db, item_id)
    finally:
        db.close()


def test_the_sooner_reset_is_chosen_and_the_attempt_record_says_so(client, world):
    # `late` is created first, so creation order alone would pick it: only the reset decides
    late = _ent(client, world, "openai", reset=datetime.now(timezone.utc) + timedelta(days=3))
    soon = _ent(client, world, "anthropic", reset=datetime.now(timezone.utc) + timedelta(hours=1))
    a = _runner(client, world, [late, soon])
    for e in (soon, late):
        _report(client, world, e, a, "registered")
    _runner(client, world, [_ent(client, world, "google")], vendor="google")  # reviewer vendor
    item = _item()
    assert _claim(a, item) == item
    (rec,) = [d for d in _decisions(item) if d["kind"] == "claim"]
    assert rec["entitlement_id"] == soon and "soonest reset" in rec["reason"]
    assert rec["agent_vendor"] == "anthropic" and rec["agent_model"] == "m"
    assert rec["matched"] is True


def test_a_stated_reset_beats_an_earlier_created_entitlement_with_none(client, world):
    unstated = _ent(client, world, "openai")
    stated = _ent(client, world, "anthropic", reset=datetime.now(timezone.utc) + timedelta(days=3))
    a = _runner(client, world, [unstated, stated])
    for e in (unstated, stated):
        _report(client, world, e, a, "registered")
    _runner(client, world, [_ent(client, world, "google")], vendor="google")  # reviewer vendor
    item = _item()
    assert _claim(a, item) == item
    (rec,) = [d for d in _decisions(item) if d["kind"] == "claim"]
    assert rec["entitlement_id"] == stated
    assert f"chosen over {unstated} by soonest reset" in rec["reason"]


def test_the_reason_does_not_claim_soonest_reset_when_no_reset_decided(client, world):
    first = _ent(client, world, "openai")
    second = _ent(client, world, "anthropic")
    a = _runner(client, world, [first, second])
    for e in (first, second):
        _report(client, world, e, a, "registered")
    _runner(client, world, [_ent(client, world, "google")], vendor="google")  # reviewer vendor
    item = _item()
    assert _claim(a, item) == item
    (rec,) = [d for d in _decisions(item) if d["kind"] == "claim"]
    assert rec["entitlement_id"] == first
    assert "soonest reset" not in rec["reason"] and "creation order decides" in rec["reason"]


def test_a_build_that_would_leave_no_reviewer_vendor_is_not_scheduled(client, world):
    only = _ent(client, world, "anthropic")
    a = _runner(client, world, [only])
    _report(client, world, only, a, "registered")
    item = _item()
    assert _claim(a, item) is None, "nobody else could review a build on the only vendor"
    other = _ent(client, world, "openai")
    _runner(client, world, [other], vendor="openai")  # unknown counts as reviewer headroom
    assert _claim(a, item) == item


def test_an_item_is_held_for_the_runner_with_headroom_then_lapses_to_the_ladder(client, world):
    held = _ent(client, world, "anthropic")
    b = _runner(client, world, [held])
    _report(client, world, held, b, "registered")
    broke = _ent(client, world, "openai")
    x = _runner(client, world, [broke], vendor="openai")
    _report(client, world, broke, x, "spawn_failed", vendor_text="boom")
    _ent(client, world, "google")  # reviewer reserve; no runner declares it
    item = _item()
    assert _claim(x, item) is None
    (res,) = [d for d in _decisions(item) if d["kind"] == "reservation"]
    assert res["entitlement_id"] == held
    # lapse
    from app.db import SessionLocal
    from app.models import RoutingDecision
    db = SessionLocal()
    row = db.query(RoutingDecision).filter_by(kind="reservation").one()
    row.reserved_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit(); db.close()
    # with the reservation lapsed the item is offered again: the holder's own claim works
    assert _claim(b, item) == item


def test_a_reserved_claim_that_fails_ends_its_reservation_at_once(client, world):
    held = _ent(client, world, "anthropic")
    b = _runner(client, world, [held])
    _report(client, world, held, b, "registered")
    broke = _ent(client, world, "google")
    x = _runner(client, world, [broke], vendor="google")
    _report(client, world, broke, x, "spawn_failed", vendor_text="boom")
    _ent(client, world, "mistral")  # reviewer reserve; no runner declares it, so it builds nothing
    item = _item()
    assert _claim(x, item) is None  # reserved for `held`, the only buildable entitlement
    assert _claim(b, item) == item
    # B's spawn then hits a vendor limit the server did not know about
    _report(client, world, held, b, "limit_hit", vendor_text="You've hit your limit")
    res = [r for r in _decisions(item) if r["kind"] == "reservation"][0]
    assert res["ended_reason"] == "limit_hit"
    # Released and routed again from the ledger NOW, not at TTL: nothing buildable is left, so
    # the ladder's next rung is X's to take.
    assert _claim(x, item) == item


def _three_runners(client, world):
    a_ent = _ent(client, world, "anthropic")
    a = _runner(client, world, [a_ent])
    _report(client, world, a_ent, a, "registered")
    d_ent = _ent(client, world, "openai")
    d = _runner(client, world, [d_ent], vendor="openai")
    _report(client, world, d_ent, d, "registered")
    x_ent = _ent(client, world, "google")
    x = _runner(client, world, [x_ent], vendor="google")
    _report(client, world, x_ent, x, "spawn_failed", vendor_text="boom")
    return a, d, x


def test_a_headroomless_claim_does_not_reserve_while_several_runners_have_headroom(client, world):
    a, d, x = _three_runners(client, world)
    item = _item()
    assert _claim(x, item) is None, "X has no headroom; A and D do"
    assert not [r for r in _decisions(item) if r["kind"] == "reservation"]
    assert _claim(d, item) == item, "D has its own headroom and a reviewer vendor (A)"


def test_claim_next_by_a_headroomless_runner_does_not_lock_the_queue(client, world):
    a, d, x = _three_runners(client, world)
    items = [_item(f"w{i}") for i in range(3)]
    assert _claim(x) is None
    assert not any(r["kind"] == "reservation" for i in items for r in _decisions(i))
    assert _claim(d) in items and _claim(a) in items


def test_a_live_reservation_still_offers_the_item_to_a_runner_with_headroom(client, world):
    held = _ent(client, world, "anthropic")
    b = _runner(client, world, [held])
    _report(client, world, held, b, "registered")
    broke = _ent(client, world, "google")
    x = _runner(client, world, [broke], vendor="google")
    _report(client, world, broke, x, "spawn_failed", vendor_text="boom")
    _ent(client, world, "mistral")  # reviewer reserve; no runner declares it
    item = _item()
    assert _claim(x, item) is None
    assert [r for r in _decisions(item) if r["kind"] == "reservation"]
    # A third runner arrives with headroom while the reservation is live: it is not locked out.
    other = _ent(client, world, "openai")
    d = _runner(client, world, [other], vendor="openai")
    _report(client, world, other, d, "registered")
    assert _claim(x, item) is None, "the reservation still holds against the headroomless"
    assert _claim(d, item) == item
