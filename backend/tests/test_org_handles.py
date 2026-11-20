"""GRPH-1789 (P93 S3): invitation handle clash and org-only handles."""
import pytest
from sqlalchemy import select

from app.models import HandleHold, OrgMembership, User
from tests.test_cross_tenant import _login, _make_org


@pytest.fixture()
def world(client, monkeypatch):
    from app.config import settings
    from app.db import SessionLocal
    from app.services import handles
    from app.services.email import outbox

    monkeypatch.setattr(settings, "hosted_mode", True)
    alex = _login(client, "alex@ascme-labs.com")
    dana = _login(client, "dana@ascme-labs.com")
    org_a = _make_org(client, alex, "Org A")
    _make_org(client, dana, "Org B")
    db = SessionLocal()
    alex_handle = db.get(User, "u1").handle
    db.get(User, "u2").handle = alex_handle   # dana's account handle clashes in org A only
    db.commit()
    seen = []
    real = handles.check_free

    def spy(db_, handle, org_ids, exclude_user=None):
        seen.append(list(org_ids))
        return real(db_, handle, org_ids, exclude_user=exclude_user)

    monkeypatch.setattr(handles, "check_free", spy)
    outbox.clear()
    client.post(f"/api/orgs/{org_a}/invites", json={"email": "dana@ascme-labs.com"}, headers=alex)
    token = outbox[0].text.rsplit("/invite/", 1)[1].split()[0]
    yield dict(client=client, alex=alex, dana=dana, org_a=org_a, token=token, db=db, seen=seen)
    db.close()


def test_clash_refused_with_suggestion_then_retry_sets_org_handle(world):
    c, dana, tok = world["client"], world["dana"], world["token"]
    r = c.post("/api/invites/accept", json={"token": tok}, headers=dana)
    assert r.status_code == 409, r.text
    d = r.json()["detail"]
    assert d["code"] == "handle_taken" and d["org_id"] == world["org_a"] and d["org_name"] == "Org A"
    assert world["seen"][-1] == [world["org_a"]]
    r = c.post("/api/invites/accept", json={"token": tok, "handle": d["suggested"]}, headers=dana)
    assert r.status_code == 200, r.text
    assert r.json()["handle"] == d["suggested"]
    db = world["db"]
    db.expire_all()
    seat = db.scalar(select(OrgMembership).where(
        OrgMembership.org_id == world["org_a"], OrgMembership.user_id == "u2"))
    assert seat.handle == d["suggested"]
    assert db.get(User, "u2").handle != d["suggested"]  # account handle untouched
    mine = {o["id"]: o for o in c.get("/api/orgs", headers=dana).json()}
    assert mine[world["org_a"]]["handle"] == d["suggested"]


def test_accept_with_taken_handle_is_refused_again(world):
    r = world["client"].post("/api/invites/accept", headers=world["dana"],
                             json={"token": world["token"], "handle": world["db"].get(User, "u1").handle})
    assert r.status_code == 409


def test_accept_check_reads_only_the_inviting_org(world):
    """Clash lives in an unrelated org: the join goes through with the account handle."""
    db = world["db"]
    from app.models import Organization
    db.add(Organization(id="org_other", name="Other"))
    db.flush()
    db.add(OrgMembership(org_id="org_other", user_id="u1", role="member", handle="dana-handle"))
    db.get(User, "u2").handle = "dana-handle"
    db.get(User, "u1").handle = "alex-moved"
    db.query(OrgMembership).filter_by(org_id=world["org_a"], user_id="u1").update({"handle": "alex-moved"})
    db.commit()
    r = world["client"].post("/api/invites/accept", json={"token": world["token"]}, headers=world["dana"])
    assert r.status_code == 200, r.text
    assert r.json()["handle"] is None
    assert all(ids == [world["org_a"]] for ids in world["seen"])


def test_patch_handle_holds_old_and_scopes_to_org(world):
    c, dana = world["client"], world["dana"]
    d = c.post("/api/invites/accept", json={"token": world["token"]}, headers=dana).json()["detail"]
    c.post("/api/invites/accept", json={"token": world["token"], "handle": d["suggested"]}, headers=dana)
    r = c.patch(f"/api/orgs/{world['org_a']}/me/handle", json={"handle": "dana-new"}, headers=dana)
    assert r.status_code == 200 and r.json()["handle"] == "dana-new"
    db = world["db"]
    db.expire_all()
    holds = db.scalars(select(HandleHold).where(HandleHold.user_id == "u2")).all()
    assert [(h.org_id, h.handle) for h in holds] == [(world["org_a"], d["suggested"])]
    # another member of org A cannot take the held handle; a member of org B can.
    r = c.patch(f"/api/orgs/{world['org_a']}/me/handle", json={"handle": d["suggested"]},
                headers=world["alex"])
    assert r.status_code == 409
    from app.services import handles
    org_b = next(o.org_id for o in db.scalars(select(OrgMembership).where(OrgMembership.user_id == "u2"))
                 if o.org_id != world["org_a"])
    assert handles.check_free(db, d["suggested"], [org_b]) is None


def test_patch_handle_refused_for_org_you_are_not_in(world):
    r = world["client"].patch(f"/api/orgs/{world['org_a']}/me/handle", json={"handle": "whoever"},
                              headers=world["dana"])
    assert r.status_code in (403, 404)
