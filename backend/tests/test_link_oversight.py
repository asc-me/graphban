"""GRPH-P104 S5: human oversight — the org view, audit, stale alerts, retention."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import (Alert, Event, Organization, OrgMembership, PrdLinkMessage, Project, User,
                        utcnow)
from app.services import links as L
from app.services import oversight as O
from app.services import prds as prds_svc
from app.services import suggestions as S
from app.services import threads as T
from app.services.links import Caller


@pytest.fixture()
def world(client):
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        db.add(Organization(id="o1", name="O1"))
        db.flush()
        db.add_all([Project(id="pa", name="A", tag="PAA", org_id="o1"),
                    Project(id="pb", name="B", tag="PBB", org_id="o1"),
                    User(id="u_owner", name="Olive", handle="olive", email="o@x.io", password_hash="x")])
        db.flush()
        db.add(OrgMembership(org_id="o1", user_id="u_owner", role="owner"))
        a = prds_svc.create_prd(db, title="A one", project_id="pa")
        b = prds_svc.create_prd(db, title="B two", project_id="pb")
        yield db, a, b
    finally:
        db.close()


def _a():
    return Caller(readable=["pa"], writable=["pa"], is_agent=True, label="agent-a", owner="Ann")


def _b():
    return Caller(readable=["pb"], writable=["pb"], label="Bob", owner="Bob")


def _linked(world):
    db, a, b = world
    link = L.request_prd_link(db, _a(), a.id, b.id, reason="r")
    L.accept_prd_link(db, _b(), link.id)
    return db, a, b, link


def _age(db, msg_id, hours):
    db.get(PrdLinkMessage, msg_id).created_at = utcnow() - timedelta(hours=hours)
    db.commit()


def _alerts(db):
    return list(db.scalars(select(Alert).where(Alert.kind == O.KIND, Alert.cleared_at.is_(None))))


def test_stale_suggestion_alerts_org_owner_then_prd_close_withdraws(world):
    db, a, b, link = _linked(world)
    m = S.suggest(db, _a(), link.id, {"title": "Do it"})
    assert O.sweep(db, "o1")["raised"] == 0  # fresh: not stale
    _age(db, m["id"], 100)
    assert O.sweep(db, "o1")["raised"] == 1
    assert O.sweep(db, "o1")["raised"] == 0  # one live alert per recipient
    [al] = _alerts(db)
    assert al.user_id == "u_owner" and al.project_id == "pb"
    a.close_record = {"closed": True}
    db.commit()
    out = O.sweep(db, "o1")
    assert out["withdrawn"] == 1 and out["cleared"] == 1 and not _alerts(db)
    msg = db.get(PrdLinkMessage, m["id"])
    assert (msg.outcome, msg.outcome_code) == ("declined", "prd_closed")


def test_decision_clears_alert_and_unlink_withdraws(world):
    db, a, b, link = _linked(world)
    m = S.suggest(db, _a(), link.id, {"title": "Do it"})
    _age(db, m["id"], 100)
    O.sweep(db, "o1")
    S.decline_suggestion(db, _b(), link.id, m["id"], reason="no")
    O.sweep(db, "o1")
    assert not _alerts(db)
    m2 = S.suggest(db, _a(), link.id, {"title": "Again"})
    L.unlink_prd_link(db, Caller(readable=["pa"], writable=["pa"], label="Ann"), link.id)
    assert db.get(PrdLinkMessage, m2["id"]).outcome_code == "unlinked"


def test_overview_lists_and_audits(world):
    db, a, b = world
    link = L.request_prd_link(db, _a(), a.id, b.id, reason="r")
    user = db.get(User, "u_owner")
    from app.models import Membership
    db.add_all([Membership(project_id="pa", user_id="u_owner", role="owner"),
                Membership(project_id="pb", user_id="u_owner", role="owner")])
    db.commit()
    view = O.overview(db, user, "o1")
    assert [t["status"] for t in view["threads"]] == ["pending"]
    L.accept_prd_link(db, _b(), link.id)
    m = S.suggest(db, _a(), link.id, {"title": "Do it"})
    T.post(db, _a(), link.id, kind="question", body="why?")
    view = O.overview(db, user, "o1")
    [row] = view["threads"]
    assert row["latest_kind"] == "question" and row["suggestions"][0]["outcome"] == "undecided"
    actions = {e.action for e in db.scalars(select(Event).where(Event.target_type == "prd_link"))}
    assert {"prd_link_request", "prd_link_accept", "prd_link_message"} <= actions


def _approve(db, prd):
    window = prds_svc.grill_window(db, prd.id)
    prior = prds_svc.grill_history(db, prd.id, since=window)
    prds_svc.record_grill_turns(db, prd.id, prior + [{"role": "user", "text": "Answer."}])
    for name in prds_svc.DIMENSIONS:
        prds_svc.set_dimension(db, prd.id, name, "resolved")
    prds_svc.sync_status(db, prd)
    return prd


def test_close_prd_withdraws_at_once_and_clears_the_alert_but_keeps_filed_items(world):
    """S5 acceptance: closing A-P1 withdraws the undecided suggestion with `prd_closed` in the
    close itself, not at some later sweep; one already filed stays filed."""
    db, a, b, link = _linked(world)
    filed = S.suggest(db, _a(), link.id, {"title": "Filed one"})
    out = S.file_suggestion(db, _b(), link.id, filed["id"])
    item_id = db.get(PrdLinkMessage, filed["id"]).outcome_item_id
    assert out["decided"] and item_id
    m = S.suggest(db, _a(), link.id, {"title": "Do it"})
    _age(db, m["id"], 100)
    assert O.sweep(db, "o1")["raised"] == 1
    _approve(db, a)
    prds_svc.close_prd(db, a, dispositions=[
        {"section": s, "disposition": "deferred", "reason": "not v1"}
        for s in prds_svc.dropped_intent(db, a)], closed_by="user:1")
    msg = db.get(PrdLinkMessage, m["id"])
    assert (msg.outcome, msg.outcome_code) == ("declined", "prd_closed")
    assert not _alerts(db)
    assert db.get(PrdLinkMessage, filed["id"]).outcome == "filed"
    from app.models import Item
    assert db.get(Item, item_id) is not None
    actions = [e.action for e in db.scalars(select(Event).where(Event.target_type == "prd_link"))]
    assert "prd_link_suggestion_withdrawn" in actions and "prd_link_suggestion_filed" in actions


def test_org_owner_fallback_alert_is_in_their_bell_and_view(world):
    """Nobody on pb can be found, so the org owner gets the alert — and with no seat on either
    project they still see it in the bell and the link in the oversight view."""
    from app.services import alerts as alerts_svc

    db, a, b, link = _linked(world)
    m = S.suggest(db, _a(), link.id, {"title": "Do it"})
    _age(db, m["id"], 100)
    O.sweep(db)  # the periodic pass: every org
    bell = alerts_svc.list_for_user(db, "u_owner")
    assert [al["kind"] for al in bell["alerts"]] == [O.KIND]
    [row] = O.overview(db, db.get(User, "u_owner"), "o1")["threads"]
    assert row["stale"] and row["suggestions"][0]["stale"] and not row["can_decide"]


def test_stale_age_is_the_orgs(world):
    db, a, b, link = _linked(world)
    m = S.suggest(db, _a(), link.id, {"title": "Do it"})
    _age(db, m["id"], 5)
    assert O.sweep(db, "o1")["raised"] == 0
    O.set_stale_hours(db, db.get(User, "u_owner"), "o1", 4)
    assert O.sweep(db, "o1")["raised"] == 1
