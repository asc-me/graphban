"""PRD-21 D2 — the org dashboard, and the first cross-project aggregate.

§3.3 of the PRD established that no org-scoped aggregate existed anywhere in the codebase
and that one could not be had by relaxing a filter, because `require_readable` fails closed
on a null project *by design*. So the assertions that matter here are about scope: this
read must see every project the caller can read, no project from another org, and it must
not have loosened the guard it was written to sit beside.
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


@pytest.fixture()
def org(client, hosted):
    owner = _login(client, "alex@ascme-labs.com")
    o = client.post("/api/orgs", json={"name": "Acme"}, headers=owner).json()
    a = client.post("/api/projects", json={"name": "Rocket"}, headers=owner).json()
    b = client.post("/api/projects", json={"name": "Booster"}, headers=owner).json()
    return {"owner": owner, "org": o, "a": a, "b": b}


def test_the_overview_sees_every_project_at_once(client, org):
    """G1. Before this, an org with seven repos was seven visits to a single-project app."""
    r = client.get(f"/api/orgs/{org['org']['id']}/overview", headers=org["owner"])
    assert r.status_code == 200, r.text
    body = r.json()
    tags = {p["tag"] for p in body["projects"]}
    assert tags == {org["a"]["tag"], org["b"]["tag"]}
    assert body["totals"]["projects"] == 2


def test_a_project_that_never_synced_appears_rather_than_being_omitted(client, org):
    """The load-bearing case. Omitting it would shrink the org and hide precisely the
    projects that need attention — and `never` is not `stale`: a link set up and not
    finished is not a box that stopped."""
    client.post("/api/items", json={"title": "One", "project_id": org["a"]["id"]},
                headers=org["owner"])

    body = client.get(f"/api/orgs/{org['org']['id']}/overview", headers=org["owner"]).json()
    row = next(p for p in body["projects"] if p["tag"] == org["a"]["tag"])

    assert row["sync"] == "never"
    assert row["nodes"] == 0
    # Its ITEM counts are real even with no graph — the cloud is authoritative for items
    # when a box is linked, so a never-synced project is not an empty one.
    assert row["open_items"] == 1, "a never-synced project must not read as having no work"
    assert body["totals"]["never_synced"] == 2


def test_another_orgs_projects_never_appear(client, org, hosted):
    """AC 7's first half."""
    other = _login(client, "dana@ascme-labs.com")
    o2 = client.post("/api/orgs", json={"name": "Northbeam"}, headers=other).json()
    theirs = client.post("/api/projects", json={"name": "Secret"}, headers=other).json()

    body = client.get(f"/api/orgs/{org['org']['id']}/overview", headers=org["owner"]).json()
    assert theirs["tag"] not in {p["tag"] for p in body["projects"]}
    assert o2["id"] != org["org"]["id"]


def test_an_owner_of_two_orgs_sees_only_the_one_they_asked_for(client, org, hosted):
    """The case where the org filter is the ONLY thing protecting the boundary.

    `test_another_orgs_projects_never_appear` passes even with the org filter removed,
    because readability alone excludes a stranger's project — it proves the outcome
    without isolating the mechanism. Here both orgs are fully readable to one person, and
    nothing but the scoping keeps them apart.

    The second org is seated directly rather than through `POST /api/orgs`, which gates
    additional orgs behind an operator request. Going through the API made this test
    **skip**, and a skipped test asserting a tenant boundary is worse than no test: the
    run stays green and the boundary is unexamined.
    """
    from app.db import SessionLocal
    from app.models import Membership, OrgMembership, Organization, Project

    s = SessionLocal()
    try:
        me = s.scalar(
            OrgMembership.__table__.select().where(
                OrgMembership.org_id == org["org"]["id"]
            ).with_only_columns(OrgMembership.user_id)
        )
        s.add(Organization(id="org_second", name="Northbeam", plan="free"))
        s.flush()  # the rows below carry FKs to it
        s.add(OrgMembership(org_id="org_second", user_id=me, role="owner"))
        s.add(Project(id="prj_secret", name="Secret", tag="SEC", org_id="org_second"))
        s.flush()
        s.add(Membership(user_id=me, project_id="prj_secret", role="owner", access="write"))
        s.commit()
    finally:
        s.close()

    body = client.get(f"/api/orgs/{org['org']['id']}/overview", headers=org["owner"]).json()
    tags = {p["tag"] for p in body["projects"]}
    assert "SEC" not in tags, "an org the caller also owns is still a different org"

    # And the second org resolves on its own, so the exclusion above is scoping rather
    # than the project being invisible for some unrelated reason.
    other = client.get("/api/orgs/org_second/overview", headers=org["owner"])
    assert other.status_code == 200, other.text
    assert {p["tag"] for p in other.json()["projects"]} == {"SEC"}


def test_a_non_member_gets_404_not_403(client, org, hosted):
    """AC 7's second half. 403 would confirm the org exists, which is the probe the 404
    is there to defeat."""
    outsider = _login(client, "dana@ascme-labs.com")
    r = client.get(f"/api/orgs/{org['org']['id']}/overview", headers=outsider)
    assert r.status_code == 404, r.text


def test_the_fail_closed_guard_is_untouched(client, org):
    """D2 is a new endpoint precisely so this stays true. If a later change ever makes an
    unscoped read reachable, this is where it should be noticed."""
    from app.db import SessionLocal
    from app.security import authz

    s = SessionLocal()
    try:
        assert authz.can_read(s, "u1", None) is False
    finally:
        s.close()


def test_usage_is_reported_against_the_plan(client, org):
    body = client.get(f"/api/orgs/{org['org']['id']}/overview", headers=org["owner"]).json()
    assert body["usage"]["projects"] == 2
    assert body["limits"]["max_projects"] >= 2
