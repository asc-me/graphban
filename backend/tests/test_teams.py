"""PRD-21 D5 — teams, and what a grant writes.

The design decision under test is that **a grant materializes**: it writes `Membership`
rows rather than adding a resolution step to `can_read` / `can_write`, because those are
the hottest authorization path in the app. So most of these assert on real membership rows
and on `authz`, not on a team API returning what it was told.

The subtle half is revocation. It **recomputes from the grants that remain** rather than
deleting rows attributable to the revoked team — the difference matters the moment anyone
gets the same project from two places.
"""
import pytest

SEED_PW = "graphban"


def _login(client, email, password=SEED_PW):
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def hosted(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "hosted_mode", True)
    return settings


def _token_of(invite: dict) -> str:
    return invite["accept_url"].rsplit("/", 1)[-1]


@pytest.fixture()
def org(client, hosted):
    owner = _login(client, "alex@ascme-labs.com")
    o = client.post("/api/orgs", json={"name": "Acme"}, headers=owner).json()
    project = client.post("/api/projects", json={"name": "Rocket"}, headers=owner).json()
    inv = client.post(f"/api/orgs/{o['id']}/invites",
                      json={"email": "ops@ascme-labs.com", "role": "member"},
                      headers=owner).json()
    ops = _login(client, "ops@ascme-labs.com")
    client.post("/api/invites/accept", json={"token": _token_of(inv)}, headers=ops)
    rows = client.get(f"/api/orgs/{o['id']}/members", headers=owner).json()
    ops_id = next(r["user"]["id"] for r in rows if r["user"]["email"] == "ops@ascme-labs.com")
    return {"owner": owner, "ops": ops, "ops_id": ops_id, "org": o, "project": project}


def _access(client, auth, project_id, user_id):
    members = client.get(f"/api/projects/{project_id}/members", headers=auth).json()
    row = next((m for m in members if m["user"]["id"] == user_id), None)
    return row["access"] if row else None


def _team(client, auth, org_id, name="Platform"):
    r = client.post(f"/api/orgs/{org_id}/teams", json={"name": name}, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


# ---- a grant materializes -------------------------------------------------------
def test_a_grant_writes_real_membership_rows(client, org):
    """Not a resolution step at read time — an actual row, so every existing authz test
    keeps its meaning and the hottest path in the app is untouched."""
    t = _team(client, org["owner"], org["org"]["id"])
    client.post(f"/api/teams/{t['id']}/members/{org['ops_id']}", headers=org["owner"])
    client.put(f"/api/teams/{t['id']}/grants/{org['project']['id']}",
               json={"access": "write"}, headers=org["owner"])

    assert _access(client, org["owner"], org["project"]["id"], org["ops_id"]) == "write"
    # And it is real authorization, not bookkeeping: they can now read the project.
    assert client.get(f"/api/items?project_id={org['project']['id']}",
                      headers=org["ops"]).status_code == 200


def test_joining_a_team_materializes_the_grants_it_already_holds(client, org):
    t = _team(client, org["owner"], org["org"]["id"])
    client.put(f"/api/teams/{t['id']}/grants/{org['project']['id']}",
               json={"access": "read"}, headers=org["owner"])
    assert _access(client, org["owner"], org["project"]["id"], org["ops_id"]) is None

    client.post(f"/api/teams/{t['id']}/members/{org['ops_id']}", headers=org["owner"])
    assert _access(client, org["owner"], org["project"]["id"], org["ops_id"]) == "read"


def test_access_resolves_to_the_highest_across_teams(client, org):
    """Two teams, two levels. The more permissive one wins — a person is not less able
    because of the order two administrators happened to act in."""
    a = _team(client, org["owner"], org["org"]["id"], "Readers")
    b = _team(client, org["owner"], org["org"]["id"], "Writers")
    for t, level in ((a, "read"), (b, "write")):
        client.post(f"/api/teams/{t['id']}/members/{org['ops_id']}", headers=org["owner"])
        client.put(f"/api/teams/{t['id']}/grants/{org['project']['id']}",
                   json={"access": level}, headers=org["owner"])

    assert _access(client, org["owner"], org["project"]["id"], org["ops_id"]) == "write"


# ---- revocation recomputes, it does not delete ------------------------------------
def test_revoking_one_of_two_grants_keeps_the_access_the_other_provides(client, org):
    """The reason revocation recomputes instead of deleting rows attributable to the
    revoked team: someone still legitimately has this access, and bookkeeping that removed
    it would be a wrong answer nobody asked for."""
    a = _team(client, org["owner"], org["org"]["id"], "Readers")
    b = _team(client, org["owner"], org["org"]["id"], "Writers")
    for t, level in ((a, "read"), (b, "write")):
        client.post(f"/api/teams/{t['id']}/members/{org['ops_id']}", headers=org["owner"])
        client.put(f"/api/teams/{t['id']}/grants/{org['project']['id']}",
                   json={"access": level}, headers=org["owner"])

    r = client.delete(f"/api/teams/{b['id']}/grants/{org['project']['id']}", headers=org["owner"])
    assert r.status_code == 200, r.text
    assert r.json()["kept_access"] == [org["ops_id"]]
    # Dropped from write to read — recomputed, not deleted.
    assert _access(client, org["owner"], org["project"]["id"], org["ops_id"]) == "read"


def test_revoking_the_only_grant_removes_the_derived_access(client, org):
    t = _team(client, org["owner"], org["org"]["id"])
    client.post(f"/api/teams/{t['id']}/members/{org['ops_id']}", headers=org["owner"])
    client.put(f"/api/teams/{t['id']}/grants/{org['project']['id']}",
               json={"access": "write"}, headers=org["owner"])

    r = client.delete(f"/api/teams/{t['id']}/grants/{org['project']['id']}", headers=org["owner"])
    assert r.json()["kept_access"] == []
    assert _access(client, org["owner"], org["project"]["id"], org["ops_id"]) is None


def test_leaving_a_team_recomputes_rather_than_stripping(client, org):
    a = _team(client, org["owner"], org["org"]["id"], "Readers")
    b = _team(client, org["owner"], org["org"]["id"], "Writers")
    for t, level in ((a, "read"), (b, "write")):
        client.post(f"/api/teams/{t['id']}/members/{org['ops_id']}", headers=org["owner"])
        client.put(f"/api/teams/{t['id']}/grants/{org['project']['id']}",
                   json={"access": level}, headers=org["owner"])

    client.delete(f"/api/teams/{b['id']}/members/{org['ops_id']}", headers=org["owner"])
    assert _access(client, org["owner"], org["project"]["id"], org["ops_id"]) == "read"


# ---- direct always wins -----------------------------------------------------------
def test_a_direct_membership_survives_and_beats_a_grant(client, org):
    """A human's explicit decision is not something bulk administration may overwrite."""
    client.put(f"/api/projects/{org['project']['id']}/members/{org['ops_id']}",
               json={"access": "read"}, headers=org["owner"])

    t = _team(client, org["owner"], org["org"]["id"])
    client.post(f"/api/teams/{t['id']}/members/{org['ops_id']}", headers=org["owner"])
    client.put(f"/api/teams/{t['id']}/grants/{org['project']['id']}",
               json={"access": "write"}, headers=org["owner"])

    # The grant materialized nothing here: the direct row stands, unchanged.
    assert _access(client, org["owner"], org["project"]["id"], org["ops_id"]) == "read"

    # And revoking the grant does not take away what the human gave.
    client.delete(f"/api/teams/{t['id']}/grants/{org['project']['id']}", headers=org["owner"])
    assert _access(client, org["owner"], org["project"]["id"], org["ops_id"]) == "read"


def test_a_derived_membership_refuses_a_direct_edit_and_names_the_team(client, org):
    """The drift D5 accepts, made visible. Editing here would be undone by the next
    recompute, and an admin who thought they had changed something would be wrong."""
    t = _team(client, org["owner"], org["org"]["id"], "Platform")
    client.post(f"/api/teams/{t['id']}/members/{org['ops_id']}", headers=org["owner"])
    client.put(f"/api/teams/{t['id']}/grants/{org['project']['id']}",
               json={"access": "read"}, headers=org["owner"])

    r = client.put(f"/api/projects/{org['project']['id']}/members/{org['ops_id']}",
                   json={"access": "write"}, headers=org["owner"])
    assert r.status_code == 409
    assert "Platform" in r.json()["detail"]


# ---- disbanding --------------------------------------------------------------------
def test_disbanding_a_team_leaves_direct_access_alone(client, org):
    client.put(f"/api/projects/{org['project']['id']}/members/{org['ops_id']}",
               json={"access": "write"}, headers=org["owner"])
    t = _team(client, org["owner"], org["org"]["id"])
    client.post(f"/api/teams/{t['id']}/members/{org['ops_id']}", headers=org["owner"])
    client.put(f"/api/teams/{t['id']}/grants/{org['project']['id']}",
               json={"access": "read"}, headers=org["owner"])

    assert client.delete(f"/api/teams/{t['id']}", headers=org["owner"]).status_code == 200
    assert _access(client, org["owner"], org["project"]["id"], org["ops_id"]) == "write"


# ---- scope and authority -------------------------------------------------------------
def test_a_grant_cannot_reach_another_orgs_project(client, org, hosted):
    """A cross-org grant would be an access path the org roster cannot explain."""
    dana = _login(client, "dana@ascme-labs.com")
    client.post("/api/orgs", json={"name": "Dana Co"}, headers=dana)
    theirs = client.post("/api/projects", json={"name": "Theirs"}, headers=dana).json()

    t = _team(client, org["owner"], org["org"]["id"])
    r = client.put(f"/api/teams/{t['id']}/grants/{theirs['id']}",
                   json={"access": "read"}, headers=org["owner"])
    assert r.status_code == 404


def test_a_plain_member_cannot_administer_teams(client, org):
    t = _team(client, org["owner"], org["org"]["id"])
    assert client.post(f"/api/orgs/{org['org']['id']}/teams", json={"name": "Nope"},
                       headers=org["ops"]).status_code == 403
    assert client.put(f"/api/teams/{t['id']}/grants/{org['project']['id']}",
                      json={"access": "write"}, headers=org["ops"]).status_code == 403


def test_an_unknown_grant_level_is_refused_by_name(client, org):
    t = _team(client, org["owner"], org["org"]["id"])
    r = client.put(f"/api/teams/{t['id']}/grants/{org['project']['id']}",
                   json={"access": "admin"}, headers=org["owner"])
    assert r.status_code == 422 and "admin" in r.json()["detail"]


# ---- one row per (user, project) ------------------------------------------------


def test_two_sessions_materializing_the_same_pair_cannot_both_win(client, org):
    """The race `recompute` is exposed to, run for real rather than reasoned about.

    `recompute` reads whether a membership exists and then writes one. Two transactions
    doing that concurrently for one (user, project) both read `None` and both insert, and
    until AC 9e's constraint existed nothing rejected the second.

    A duplicate is not untidiness. `db.scalar` returns whichever row it happens to find,
    so a later revocation recomputes one of them and **leaves the other behind** — access
    surviving a revocation that was supposed to remove it.

    Written against the session layer, not the API, because that is where the window is:
    going through HTTP would serialize on the request handler and prove nothing.

    **This test must fail on the code before GRPH-420** — without the unique constraint
    both inserts commit and the pair ends up with two rows.
    """
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Membership

    user_id, project_id = org["ops_id"], org["project"]["id"]

    a, b = SessionLocal(), SessionLocal()
    try:
        for s in (a, b):
            assert s.scalar(
                select(Membership).where(
                    Membership.user_id == user_id, Membership.project_id == project_id
                )
            ) is None, "the pair must start empty or the race is not the thing under test"

        a.add(Membership(user_id=user_id, project_id=project_id,
                         role="member", access="read", origin="team"))
        a.commit()

        b.add(Membership(user_id=user_id, project_id=project_id,
                         role="member", access="write", origin="team"))
        with pytest.raises(Exception):   # IntegrityError under either driver
            b.commit()
        b.rollback()
    finally:
        a.close()
        b.close()

    check = SessionLocal()
    try:
        rows = check.scalars(
            select(Membership).where(
                Membership.user_id == user_id, Membership.project_id == project_id
            )
        ).all()
        assert len(rows) == 1, f"expected exactly one membership, found {len(rows)}"
    finally:
        check.close()
