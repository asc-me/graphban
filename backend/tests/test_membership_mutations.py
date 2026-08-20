"""PRD-21 D8 — membership mutations.

Until these existed, members arrived by accepting an invite and stayed forever at the role
the invite carried (§3.5). Most of what follows asserts a *refusal*: the rank ladder means
nothing if it can be climbed from below, and a removal that leaves project access behind
is the worst kind of quiet — gone from the roster, still able to reach the work.
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
    """An owner (alex), an admin (dana) and a plain member (ops), plus one project."""
    owner = _login(client, "alex@ascme-labs.com")
    o = client.post("/api/orgs", json={"name": "Acme"}, headers=owner).json()
    project = client.post("/api/projects", json={"name": "Rocket"}, headers=owner).json()

    seats = {}
    for email, role in (("dana@ascme-labs.com", "admin"), ("ops@ascme-labs.com", "member")):
        inv = client.post(f"/api/orgs/{o['id']}/invites", json={"email": email, "role": role},
                          headers=owner).json()
        auth = _login(client, email)
        client.post("/api/invites/accept", json={"token": _token_of(inv)}, headers=auth)
        seats[role] = auth

    return {"owner": owner, "admin": seats["admin"], "member": seats["member"],
            "org": o, "project": project}


def _uid(client, auth, org_id, email):
    rows = client.get(f"/api/orgs/{org_id}/members", headers=auth).json()
    return next(r["user"]["id"] for r in rows if r["user"]["email"] == email)


# ---- role changes ---------------------------------------------------------------
def test_an_owner_promotes_a_member_to_admin(client, org):
    uid = _uid(client, org["owner"], org["org"]["id"], "ops@ascme-labs.com")
    r = client.patch(f"/api/orgs/{org['org']['id']}/members/{uid}",
                     json={"role": "admin"}, headers=org["owner"])
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "admin"


def test_an_owner_is_demotable_while_another_administrator_remains(client, org):
    """Replaces `test_the_owner_cannot_be_demoted`, which asserted the rule PRD-21 D8.1
    retired — and asserted it green, so it would have made the correct behaviour look
    like the regression.

    The old rule protected a proxy: that one specific person exists. What an org needs is
    that *somebody* can administer it, and `require_org_admin` has always accepted an
    admin as readily as an owner. So the owner seat is ordinary, and the floor invariant
    is what protects the org.
    """
    uid = _uid(client, org["owner"], org["org"]["id"], "alex@ascme-labs.com")
    r = client.patch(f"/api/orgs/{org['org']['id']}/members/{uid}",
                     json={"role": "member"}, headers=org["admin"])
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "member"


def test_the_founder_is_still_named_after_being_demoted(client, org):
    """PRD-21 D8.2. Who created the org is a durable fact; who administers it is a
    mutable role. Conflating them is what made the role immutable."""
    from app.db import SessionLocal
    from app.models import Organization, User

    uid = _uid(client, org["owner"], org["org"]["id"], "alex@ascme-labs.com")
    client.patch(f"/api/orgs/{org['org']['id']}/members/{uid}",
                 json={"role": "member"}, headers=org["admin"])

    s = SessionLocal()
    try:
        o = s.get(Organization, org["org"]["id"])
        assert o.created_by == uid, "the founder must survive their own demotion"
        assert s.get(User, o.created_by).email == "alex@ascme-labs.com"
    finally:
        s.close()


def test_only_an_owner_may_grant_the_owner_role(client, org):
    """Ownership is grantable now, but the rank ladder still governs: nobody grants a rank
    above their own, so an admin cannot mint an owner."""
    uid = _uid(client, org["owner"], org["org"]["id"], "ops@ascme-labs.com")
    assert client.patch(f"/api/orgs/{org['org']['id']}/members/{uid}",
                        json={"role": "owner"}, headers=org["admin"]).status_code == 403
    assert client.patch(f"/api/orgs/{org['org']['id']}/members/{uid}",
                        json={"role": "owner"}, headers=org["owner"]).status_code == 200


def test_nobody_promotes_themselves(client, org):
    """An admin who can grant themselves owner is not an admin."""
    uid = _uid(client, org["owner"], org["org"]["id"], "dana@ascme-labs.com")
    r = client.patch(f"/api/orgs/{org['org']['id']}/members/{uid}",
                     json={"role": "member"}, headers=org["admin"])
    assert r.status_code == 409
    assert "your own role" in r.json()["detail"]


def test_a_plain_member_cannot_change_anyone(client, org):
    uid = _uid(client, org["owner"], org["org"]["id"], "dana@ascme-labs.com")
    r = client.patch(f"/api/orgs/{org['org']['id']}/members/{uid}",
                     json={"role": "member"}, headers=org["member"])
    assert r.status_code == 403


def test_an_unknown_role_is_refused_by_name(client, org):
    uid = _uid(client, org["owner"], org["org"]["id"], "ops@ascme-labs.com")
    r = client.patch(f"/api/orgs/{org['org']['id']}/members/{uid}",
                     json={"role": "superuser"}, headers=org["owner"])
    assert r.status_code == 422
    assert "superuser" in r.json()["detail"]


# ---- removal --------------------------------------------------------------------
def test_removal_cascades_project_access_and_says_what_it_took(client, org):
    """Gone from the roster but still able to reach the work is the failure this
    prevents, so the response names what was revoked instead of reporting bare success."""
    uid = _uid(client, org["owner"], org["org"]["id"], "ops@ascme-labs.com")
    client.put(f"/api/projects/{org['project']['id']}/members/{uid}",
               json={"access": "write"}, headers=org["owner"])

    r = client.delete(f"/api/orgs/{org['org']['id']}/members/{uid}", headers=org["owner"])
    assert r.status_code == 200, r.text
    assert r.json()["removed_role"] == "member"
    assert r.json()["projects_revoked"] == [org["project"]["id"]]

    rows = client.get(f"/api/orgs/{org['org']['id']}/members", headers=org["owner"]).json()
    assert all(m["user"]["email"] != "ops@ascme-labs.com" for m in rows)
    members = client.get(f"/api/projects/{org['project']['id']}/members",
                         headers=org["owner"]).json()
    assert all(m["user"]["id"] != uid for m in members)


def test_an_owner_is_removable_while_another_administrator_remains(client, org):
    """The counterpart of demotion, and retired for the same reason."""
    uid = _uid(client, org["owner"], org["org"]["id"], "alex@ascme-labs.com")
    r = client.delete(f"/api/orgs/{org['org']['id']}/members/{uid}", headers=org["admin"])
    assert r.status_code == 200, r.text


def test_the_last_administrator_cannot_be_demoted_or_removed(client, org):
    """The floor invariant that replaced owner-immutability, from both directions.

    Demote the admin first so the owner is the only administrator left, then try to empty
    the seat. The refusal must name what would be lost rather than saying `409`.
    """
    org_id = org["org"]["id"]
    dana = _uid(client, org["owner"], org_id, "dana@ascme-labs.com")
    assert client.patch(f"/api/orgs/{org_id}/members/{dana}",
                        json={"role": "member"}, headers=org["owner"]).status_code == 200

    alex = _uid(client, org["owner"], org_id, "alex@ascme-labs.com")
    demote = client.patch(f"/api/orgs/{org_id}/members/{alex}",
                          json={"role": "member"}, headers=org["owner"])
    assert demote.status_code == 409
    assert "last owner or admin" in demote.json()["detail"]

    # `remove_member` refuses first on the floor, before the cannot-remove-yourself rule.
    remove = client.delete(f"/api/orgs/{org_id}/members/{alex}", headers=org["owner"])
    assert remove.status_code == 409
    assert "last owner or admin" in remove.json()["detail"]


def test_promoting_a_replacement_first_is_what_unblocks_it(client, org):
    """The refusal is a sequencing rule, not a wall — it tells you what to do and the
    instruction has to actually work."""
    org_id = org["org"]["id"]
    dana = _uid(client, org["owner"], org_id, "dana@ascme-labs.com")
    client.patch(f"/api/orgs/{org_id}/members/{dana}", json={"role": "member"},
                 headers=org["owner"])

    ops = _uid(client, org["owner"], org_id, "ops@ascme-labs.com")
    assert client.patch(f"/api/orgs/{org_id}/members/{ops}", json={"role": "admin"},
                        headers=org["owner"]).status_code == 200

    # The replacement does the demoting: `you cannot change your own role` is a separate
    # rule and still holds, so an owner never steps down unaided even with a floor to
    # spare. That is the sequence the refusal is actually asking for.
    alex = _uid(client, org["owner"], org_id, "alex@ascme-labs.com")
    assert client.patch(f"/api/orgs/{org_id}/members/{alex}", json={"role": "member"},
                        headers=org["member"]).status_code == 200


def test_you_cannot_remove_yourself(client, org):
    uid = _uid(client, org["owner"], org["org"]["id"], "dana@ascme-labs.com")
    r = client.delete(f"/api/orgs/{org['org']['id']}/members/{uid}", headers=org["admin"])
    assert r.status_code == 409


# ---- project access --------------------------------------------------------------
def test_access_can_be_granted_changed_and_explicitly_denied(client, org):
    """`none` is STORED, not deleted — an explicit "not this project" is a decision
    somebody made, and it must not read the same as never having been considered."""
    uid = _uid(client, org["owner"], org["org"]["id"], "ops@ascme-labs.com")
    pid = org["project"]["id"]

    assert client.put(f"/api/projects/{pid}/members/{uid}", json={"access": "write"},
                      headers=org["owner"]).json()["level"] == "write"
    assert client.put(f"/api/projects/{pid}/members/{uid}", json={"access": "read"},
                      headers=org["owner"]).json()["level"] == "read"
    assert client.put(f"/api/projects/{pid}/members/{uid}", json={"access": "none"},
                      headers=org["owner"]).json()["level"] == "none"

    members = client.get(f"/api/projects/{pid}/members", headers=org["owner"]).json()
    row = next(m for m in members if m["user"]["id"] == uid)
    assert row["access"] == "none"


def test_access_needs_a_seat_in_the_org_first(client, org, hosted):
    """Access to a project inside an org you do not belong to would be a path with no
    roster entry — invisible on every screen that lists who is here."""
    outsider = _login(client, "kate@ascme-labs.com")
    uid = client.get("/api/auth/me", headers=outsider).json()["id"]
    r = client.put(f"/api/projects/{org['project']['id']}/members/{uid}",
                   json={"access": "write"}, headers=org["owner"])
    assert r.status_code == 404


def test_a_plain_member_cannot_grant_access(client, org):
    uid = _uid(client, org["owner"], org["org"]["id"], "ops@ascme-labs.com")
    r = client.put(f"/api/projects/{org['project']['id']}/members/{uid}",
                   json={"access": "write"}, headers=org["member"])
    assert r.status_code == 403


# ---- the ledger -------------------------------------------------------------------
def test_every_mutation_is_audited(client, org):
    """Authority actions stay human-adjudicated AND audited — the rule
    `test_authority_gates.py` exists to hold."""
    from app.db import SessionLocal
    from app.models import Event
    from sqlalchemy import select

    uid = _uid(client, org["owner"], org["org"]["id"], "ops@ascme-labs.com")
    pid = org["project"]["id"]
    client.patch(f"/api/orgs/{org['org']['id']}/members/{uid}", json={"role": "admin"},
                 headers=org["owner"])
    client.put(f"/api/projects/{pid}/members/{uid}", json={"access": "read"},
               headers=org["owner"])
    client.delete(f"/api/orgs/{org['org']['id']}/members/{uid}", headers=org["owner"])

    db = SessionLocal()
    try:
        actions = set(db.scalars(select(Event.action)).all())
    finally:
        db.close()
    assert {"set_member_role", "set_project_access", "remove_member"} <= actions
