"""GRPH-186: tracker linking and authority — link record, authority flag, per-link
field mapping, and write-back comment opt-out (PRD-10).

Org-scoped: every route sits behind an org-admin gate. The link record is the first
v1 layer; the sync engine, adapter, and write-back paths are separate items.
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


@pytest.fixture(autouse=True)
def _clear_outbox():
    from app.services.email import outbox
    outbox.clear()
    yield
    outbox.clear()


def _make_org(client, headers, name="Acme"):
    r = client.post("/api/orgs", json={"name": name}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _make_project(client, headers, name="Rocket", org_id=None):
    body = {"name": name}
    if org_id:
        body["org_id"] = org_id
    r = client.post("/api/projects", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


# ---- CRUD ---------------------------------------------------------------------

def test_create_and_list_link(client, hosted):
    auth = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, auth)
    proj = _make_project(client, auth, org_id=org["id"])

    r = client.post(
        f"/api/orgs/{org['id']}/tracker-links",
        json={
            "project_id": proj["id"],
            "tracker_kind": "linear",
            "tracker_team_id": "team-uuid-1",
            "tracker_team_name": "Platform Eng",
        },
        headers=auth,
    )
    assert r.status_code == 201, r.text
    link = r.json()
    assert link["org_id"] == org["id"]
    assert link["project_id"] == proj["id"]
    assert link["tracker_kind"] == "linear"
    assert link["tracker_team_id"] == "team-uuid-1"
    assert link["tracker_team_name"] == "Platform Eng"
    assert link["authority"] is True
    assert link["write_back_comment"] is True
    assert link["field_mapping"] == {}

    listed = client.get(f"/api/orgs/{org['id']}/tracker-links", headers=auth).json()
    assert len(listed) == 1
    assert listed[0]["id"] == link["id"]


def test_get_single_link(client, hosted):
    auth = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, auth)
    proj = _make_project(client, auth, org_id=org["id"])

    r = client.post(
        f"/api/orgs/{org['id']}/tracker-links",
        json={"project_id": proj["id"], "tracker_team_id": "team-1"},
        headers=auth,
    )
    link_id = r.json()["id"]

    r = client.get(f"/api/orgs/{org['id']}/tracker-links/{link_id}", headers=auth)
    assert r.status_code == 200
    assert r.json()["id"] == link_id


def test_update_link(client, hosted):
    auth = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, auth)
    proj = _make_project(client, auth, org_id=org["id"])

    r = client.post(
        f"/api/orgs/{org['id']}/tracker-links",
        json={"project_id": proj["id"], "tracker_team_id": "team-1"},
        headers=auth,
    )
    link_id = r.json()["id"]

    r = client.patch(
        f"/api/orgs/{org['id']}/tracker-links/{link_id}",
        json={
            "authority": False,
            "write_back_comment": False,
            "field_mapping": {"status": "state", "assignee": "owner"},
            "tracker_team_name": "Renamed Team",
        },
        headers=auth,
    )
    assert r.status_code == 200, r.text
    updated = r.json()
    assert updated["authority"] is False
    assert updated["write_back_comment"] is False
    assert updated["field_mapping"] == {"status": "state", "assignee": "owner"}
    assert updated["tracker_team_name"] == "Renamed Team"


def test_delete_link(client, hosted):
    auth = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, auth)
    proj = _make_project(client, auth, org_id=org["id"])

    r = client.post(
        f"/api/orgs/{org['id']}/tracker-links",
        json={"project_id": proj["id"], "tracker_team_id": "team-1"},
        headers=auth,
    )
    link_id = r.json()["id"]

    r = client.delete(f"/api/orgs/{org['id']}/tracker-links/{link_id}", headers=auth)
    assert r.status_code == 204

    listed = client.get(f"/api/orgs/{org['id']}/tracker-links", headers=auth).json()
    assert len(listed) == 0


# ---- constraints --------------------------------------------------------------

def test_duplicate_team_rejected(client, hosted):
    auth = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, auth)
    proj = _make_project(client, auth, org_id=org["id"])

    body = {"project_id": proj["id"], "tracker_team_id": "team-dup"}
    r = client.post(f"/api/orgs/{org['id']}/tracker-links", json=body, headers=auth)
    assert r.status_code == 201

    r = client.post(f"/api/orgs/{org['id']}/tracker-links", json=body, headers=auth)
    assert r.status_code == 409


def test_unsupported_tracker_kind(client, hosted):
    auth = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, auth)
    proj = _make_project(client, auth, org_id=org["id"])

    r = client.post(
        f"/api/orgs/{org['id']}/tracker-links",
        json={"project_id": proj["id"], "tracker_kind": "jira", "tracker_team_id": "p-1"},
        headers=auth,
    )
    assert r.status_code == 422


def test_empty_team_id_rejected(client, hosted):
    auth = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, auth)
    proj = _make_project(client, auth, org_id=org["id"])

    r = client.post(
        f"/api/orgs/{org['id']}/tracker-links",
        json={"project_id": proj["id"], "tracker_team_id": "  "},
        headers=auth,
    )
    assert r.status_code == 422


# ---- authz --------------------------------------------------------------------

def test_non_member_cannot_list(client, hosted):
    auth = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, auth)

    dana_auth = _login(client, "dana@ascme-labs.com")
    r = client.get(f"/api/orgs/{org['id']}/tracker-links", headers=dana_auth)
    assert r.status_code == 404


def test_member_cannot_create(client, hosted):
    """A plain member (not admin/owner) cannot create a tracker link."""
    auth = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, auth)

    dana_auth = _login(client, "dana@ascme-labs.com")
    # Invite dana as a plain member
    r = client.post(
        f"/api/orgs/{org['id']}/invites",
        json={"email": "dana@ascme-labs.com", "role": "member"},
        headers=auth,
    )
    assert r.status_code == 201, r.text
    invite = r.json()
    token = invite["accept_url"].rsplit("/", 1)[-1]

    # Dana accepts the invite
    r = client.post(
        "/api/invites/accept",
        json={"token": token},
        headers=dana_auth,
    )
    assert r.status_code == 200, r.text

    proj = _make_project(client, auth, org_id=org["id"])
    r = client.post(
        f"/api/orgs/{org['id']}/tracker-links",
        json={"project_id": proj["id"], "tracker_team_id": "team-x"},
        headers=dana_auth,
    )
    assert r.status_code == 403


def test_cross_org_link_not_visible(client, hosted, monkeypatch):
    """A link in org A is not visible through org B's URL."""
    from app.services import quotas
    monkeypatch.setitem(
        quotas.PLANS, "free",
        quotas.Plan(**{**quotas.PLANS["free"].__dict__, "may_found_additional_orgs": True}),
    )
    auth = _login(client, "alex@ascme-labs.com")
    org_a = _make_org(client, auth, "Alpha")
    org_b = _make_org(client, auth, "Beta")
    proj = _make_project(client, auth, org_id=org_a["id"])

    r = client.post(
        f"/api/orgs/{org_a['id']}/tracker-links",
        json={"project_id": proj["id"], "tracker_team_id": "team-a"},
        headers=auth,
    )
    link_id = r.json()["id"]

    # Visible in org A
    r = client.get(f"/api/orgs/{org_a['id']}/tracker-links/{link_id}", headers=auth)
    assert r.status_code == 200

    # Not visible in org B
    r = client.get(f"/api/orgs/{org_b['id']}/tracker-links/{link_id}", headers=auth)
    assert r.status_code == 404


def test_requires_auth(client, hosted):
    assert client.get("/api/orgs/fake-org/tracker-links").status_code == 401
