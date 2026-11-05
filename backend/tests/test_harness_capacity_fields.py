"""GRPH-1337: the fields the Harness Capacity tab reads from GET /api/harness/capacity."""
from datetime import datetime, timedelta, timezone

from tests.test_capacity_forecast import _cap, _week_of_events
from tests.test_entitlements import _ev, _new_ent, hosted, org  # noqa: F401


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
    assert [g["count"] for g in rd["groups"]] == [0] * 5


def test_routing_decisions_group_by_verdict_with_verbatim_reasons(client, org):
    from app.db import SessionLocal
    from app.models import Item, RoutingDecision

    ent = _new_ent(client, org)
    r = client.post("/api/items", json={"title": "x", "project_id": org["project"]["id"]},
                    headers=org["owner"])
    assert r.status_code in (200, 201), r.text
    db = SessionLocal()
    try:
        item = db.query(Item).filter_by(project_id=org["project"]["id"]).first()
        now = datetime.now(timezone.utc)
        for i in range(7):
            db.add(RoutingDecision(item_id=item.id, kind="claim", entitlement_id=ent,
                                   reason=f"why {i}", created_at=now))
        db.add(RoutingDecision(item_id=item.id, kind="reservation", entitlement_id=ent,
                               reason="held for x", created_at=now))
        db.add(RoutingDecision(item_id=item.id, kind="claim", entitlement_id=None,
                               reason="review reserve: no vendor", created_at=now))
        db.add(RoutingDecision(item_id=item.id, kind="claim", entitlement_id=None,
                               reason="old", created_at=now - timedelta(days=30)))
        db.commit()
    finally:
        db.close()
    rd = _cap(client, org)["routing_decisions"]
    g = {x["verdict"]: x for x in rd["groups"]}
    assert rd["reason"] is None
    assert g["claimed"]["count"] == 7 and len(g["claimed"]["recent"]) == 5
    assert g["claimed"]["recent"][0]["reason"] == "why 6"
    assert g["held"]["count"] == 1 and g["review_reserve"]["count"] == 1
    assert g["next_rung"]["count"] == 0  # the 30-day-old row is outside the window
