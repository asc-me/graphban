"""GRPH-1337: the fields the Harness Capacity tab reads from GET /api/harness/capacity."""
from datetime import datetime, timedelta, timezone

from tests.test_capacity_forecast import _cap, _week_of_events
from tests.test_entitlements import _ev, _new_ent, hosted, org  # noqa: F401
from tests.test_routing import (_claim, _decisions, _ent, _item, _report, _runner,  # noqa: F401
                          world)


def test_daily_tokens_has_seven_days_and_unreported_days_are_null(client, org):
    ent = _new_ent(client, org)
    _week_of_events(client, org, ent, 2, tokens=250)
    (row,) = _cap(client, org)["entitlements"]
    d = row["daily_tokens"]
    assert len(d) == 7
    assert d[-3] == 250  # two days ago, oldest first
    assert [x for i, x in enumerate(d) if i != 4] == [None] * 6


def test_runner_org_and_seen_at(client, org):
    ent = _new_ent(client, org)
    body = _cap(client, org)
    assert body["org"] == {"name": "Acme", "entitlement_count": 1}
    (row,) = body["entitlements"]
    assert row["runner"]["label"] and row["runner"]["owner"]
    assert row["seen_at"] is None
    _ev(client, org, ent, "registered")
    _ev(client, org, ent, "limit_hit", vendor_text="usage limit reached")
    (row,) = _cap(client, org)["entitlements"]
    assert row["vendor_text"] == "usage limit reached" and row["seen_at"] == row["since"]


def test_routing_decisions_say_why_when_empty(client, org):
    _new_ent(client, org)
    rd = _cap(client, org)["routing_decisions"]
    assert rd["reason"] == "no claims routed in window"
def test_routing_decisions_say_why_when_empty(client, org):
    _new_ent(client, org)
    rd = _cap(client, org)["routing_decisions"]
    assert rd["reason"] == "no claims routed in window"
    g = {x["verdict"]: x for x in rd["groups"]}
    assert [x["verdict"] for x in rd["groups"]] == [
        "claimed", "held", "deferred", "review_reserve", "next_rung"]
    assert g["claimed"]["count"] == g["held"]["count"] == g["next_rung"]["count"] == 0


def test_verdicts_the_router_never_records_are_null_with_a_note_not_zero(client, world):
    # The review reserve refuses this build (the only vendor), and gate() writes no row for it.
    only = _ent(client, world, "anthropic")
    a = _runner(client, world, [only])
    _report(client, world, only, a, "registered")
    item = _item()
    assert _claim(a, item) is None
    rd = _cap(client, world)["routing_decisions"]
    g = {x["verdict"]: x for x in rd["groups"]}
    for key in ("review_reserve", "deferred"):
        assert g[key]["count"] is None and g[key]["recent"] == []
        assert g[key]["note"].startswith("not recorded")


def test_routing_decisions_group_rows_the_router_wrote_with_verbatim_reasons(client, world):
    # The same flow as test_routing's failed reserved claim: a reservation, a claim on it,
    # then a claim on no entitlement (the ladder's next rung). Every row comes from the router.
    held = _ent(client, world, "anthropic")
    b = _runner(client, world, [held])
    _report(client, world, held, b, "registered")
    broke = _ent(client, world, "google")
    x = _runner(client, world, [broke], vendor="google")
    _report(client, world, broke, x, "spawn_failed", vendor_text="boom")
    _ent(client, world, "mistral")  # reviewer reserve; no runner declares it
    item = _item()
    assert _claim(x, item) is None
    assert _claim(b, item) == item
    _report(client, world, held, b, "limit_hit", vendor_text="You've hit your limit")
    assert _claim(x, item) == item

    written = {d["kind"] + ("" if d["entitlement_id"] else "/none"): d["reason"]
               for d in _decisions(item)}
    rd = _cap(client, world)["routing_decisions"]
    assert rd["reason"] is None
    g = {v["verdict"]: v for v in rd["groups"]}
    assert g["held"]["count"] == 1 and g["claimed"]["count"] == 1 and g["next_rung"]["count"] == 1
    assert g["held"]["recent"][0]["reason"] == written["reservation"]
    assert g["held"]["recent"][0]["entitlement_id"] == held
    assert g["claimed"]["recent"][0]["reason"] == written["claim"]
    assert g["claimed"]["recent"][0]["entitlement_id"] == held
    assert g["next_rung"]["recent"][0]["reason"] == written["claim/none"]
    assert "next rung" in written["claim/none"]
    assert g["review_reserve"]["count"] is None and g["deferred"]["count"] is None


def test_recent_keeps_five_newest_and_the_window_drops_old_rows(client, world):
    from app.db import SessionLocal
    from app.models import RoutingDecision

    a_ent = _ent(client, world, "anthropic")
    a = _runner(client, world, [a_ent])
    _report(client, world, a_ent, a, "registered")
    _runner(client, world, [_ent(client, world, "openai")], vendor="openai")  # reviewer
    ids = [_item(f"w{i}") for i in range(7)]
    for i in ids:
        assert _claim(a, i) == i
    db = SessionLocal()
    try:  # age the first claim out of the 7-day window
        row = db.query(RoutingDecision).filter_by(item_id=ids[0], kind="claim").one()
        row.created_at = datetime.now(timezone.utc) - timedelta(days=30)
        db.commit()
    finally:
        db.close()
    g = {v["verdict"]: v for v in _cap(client, world)["routing_decisions"]["groups"]}
    assert g["claimed"]["count"] == 6 and len(g["claimed"]["recent"]) == 5
    assert g["claimed"]["recent"][0]["at"] >= g["claimed"]["recent"][-1]["at"]
