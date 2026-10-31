"""PRD-62 S5 (GRPH-1251): headroom forecast and utilization on the Observe Harness page.

Pins: a projection says why it is absent rather than inventing a date; the org rollup is None
(unknown) when nothing states a budget; waves ended by capacity and items deferred are reported
as not recorded, never 0; R7 fires on routine unused or early weeks and not on a week with
unreported spend; the page endpoint is premium and reaches the card list.
"""
from datetime import datetime, timedelta, timezone

from tests.test_entitlements import _ev, _new_ent, _url, hosted, org  # noqa: F401


def _age(ent, *, days=0, created_days=None, event_ids=None):
    """Move this entitlement's events back `days`; optionally back-date the entitlement."""
    from app.db import SessionLocal
    from app.models import Entitlement, EntitlementEvent

    db = SessionLocal()
    try:
        for e in db.query(EntitlementEvent).filter(EntitlementEvent.entitlement_id == ent):
            if event_ids is None or e.id in event_ids:
                e.created_at = datetime.now(timezone.utc) - timedelta(days=days)
        if created_days is not None:
            db.get(Entitlement, ent).created_at = (
                datetime.now(timezone.utc) - timedelta(days=created_days))
        db.commit()
    finally:
        db.close()


def _set(client, org, ent, **fields):
    r = client.patch(f"{_url(org)}/{ent}", json=fields, headers=org["owner"])
    assert r.status_code == 200, r.text


def _cap(client, org):
    r = client.get(f"/api/harness/capacity?project_id={org['project']['id']}",
                   headers=org["owner"])
    assert r.status_code == 200, r.text
    return r.json()


def _week_of_events(client, org, ent, days_ago, *, tokens=None, limit=False):
    """Report events now, then push exactly those back `days_ago` days."""
    from app.db import SessionLocal
    from app.models import EntitlementEvent

    db = SessionLocal()
    before = {e.id for e in db.query(EntitlementEvent)}
    db.close()
    _ev(client, org, ent, "registered")
    _ev(client, org, ent, "spend", **({"tokens": tokens} if tokens is not None else {}))
    if limit:
        _ev(client, org, ent, "limit_hit", vendor_text="usage limit reached")
    db = SessionLocal()
    new = {e.id for e in db.query(EntitlementEvent)} - before
    db.close()
    _age(ent, days=days_ago, event_ids=new)


def test_projection_names_why_it_is_absent(client, org):
    ent = _new_ent(client, org)
    (row,) = _cap(client, org)["entitlements"]
    assert row["projected_exhaustion"] == {"at": None, "reason": "no token_budget stated"}
    _set(client, org, ent, token_budget=1000)
    (row,) = _cap(client, org)["entitlements"]
    assert row["projected_exhaustion"]["reason"] == "no reported spend this week"
    assert row["projected_exhaustion"]["at"] is None


def test_projection_extends_this_weeks_burn_to_the_budget(client, org):
    ent = _new_ent(client, org)
    _set(client, org, ent, token_budget=1000, reset_cadence="weekly, Mondays")
    _ev(client, org, ent, "registered")
    _ev(client, org, ent, "spend", tokens=100)
    (row,) = _cap(client, org)["entitlements"]
    assert row["week"]["tokens"] == 100 and row["week"]["reported"] == 1
    assert row["reset_cadence"] == "weekly, Mondays"
    assert row["projected_exhaustion"]["at"] is not None
    _ev(client, org, ent, "spend", tokens=900)
    (row,) = _cap(client, org)["entitlements"]
    assert row["projected_exhaustion"]["at"] is not None
    assert "already" in row["projected_exhaustion"]["basis"]


def test_unreported_spend_is_a_count_in_the_week(client, org):
    ent = _new_ent(client, org)
    _ev(client, org, ent, "registered")
    (row,) = _cap(client, org)["entitlements"]
    assert row["week"]["unreported"] == 1 and row["week"]["tokens"] == 0


def test_utilization_is_unknown_without_a_budget_and_never_zero_for_unrecorded(client, org):
    ent = _new_ent(client, org)
    u = _cap(client, org)["utilization"]
    assert u["share_used"] is None and u["entitlements_without_budget"] == 1
    assert u["waves_ended_by_capacity"]["count"] is None
    assert u["items_deferred_to_reset"]["count"] is None
    _set(client, org, ent, token_budget=1000)
    _week_of_events(client, org, ent, 10, tokens=250)
    u = _cap(client, org)["utilization"]
    assert u["share_used"] == 0.25 and u["tokens_used"] == 250


def test_r7_fires_on_routinely_unused_weeks(client, org):
    ent = _new_ent(client, org)
    _set(client, org, ent, token_budget=1000)
    _age(ent, created_days=60)
    for d in (3, 10, 17, 24):
        _week_of_events(client, org, ent, d, tokens=100)
    r = client.get(f"/api/harness/recommendations?project_id={org['project']['id']}",
                   headers=org["owner"])
    cards = [c for c in r.json()["cards"] if c["rule"] == "R7"]
    assert len(cards) == 1 and "unused" in cards[0]["title"]
    assert "R7" in r.json()["rules"]


def test_r7_ignores_weeks_with_unreported_spend(client, org):
    ent = _new_ent(client, org)
    _set(client, org, ent, token_budget=1000)
    _age(ent, created_days=60)
    for d in (3, 10, 17, 24):
        _week_of_events(client, org, ent, d)  # spend with no number: unknown, not unused
    r = client.get(f"/api/harness/recommendations?project_id={org['project']['id']}",
                   headers=org["owner"])
    assert [c for c in r.json()["cards"] if c["rule"] == "R7"] == []


def test_r7_fires_on_routinely_running_out_early(client, org):
    ent = _new_ent(client, org)
    _age(ent, created_days=60)
    for d in (3, 10, 17):
        _week_of_events(client, org, ent, d, tokens=900, limit=True)
    r = client.get(f"/api/harness/recommendations?project_id={org['project']['id']}",
                   headers=org["owner"])
    (card,) = [c for c in r.json()["cards"] if c["rule"] == "R7"]
    assert "runs out early" in card["title"]


def test_r7_needs_enough_whole_weeks_of_history(client, org):
    ent = _new_ent(client, org)  # created just now: no whole week has elapsed
    _set(client, org, ent, token_budget=1000)
    r = client.get(f"/api/harness/recommendations?project_id={org['project']['id']}",
                   headers=org["owner"])
    assert [c for c in r.json()["cards"] if c["rule"] == "R7"] == []


def test_capacity_is_premium(client, org):
    from tests.test_entitlements import _set_plan

    _set_plan(org["id"], "free")
    r = client.get(f"/api/harness/capacity?project_id={org['project']['id']}",
                   headers=org["owner"])
    assert r.status_code == 402
