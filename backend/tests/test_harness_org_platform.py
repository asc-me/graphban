"""PRD-38 PR 4 — org scope, the opt-in, and the platform overlay (criteria 14, 15).

The overlay's floor is the part worth testing hardest: every one of its three conditions has
a case here, and so does the org that opts out, because "opt-in that keeps your numbers after
you leave is not opt-in" is a claim a test has to hold up.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import (HarnessRollup, Organization, OrgMembership, PlatformRollup, Project,
                        User)
from app.services import harness as hsvc


@pytest.fixture()
def db(_clean_database):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def hosted(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "hosted_mode", True)
    return True


@pytest.fixture()
def operator(client, auth, monkeypatch):
    """Hosted plus the platform-admin allowlist — the console is gated twice."""
    from app.config import settings

    monkeypatch.setattr(settings, "hosted_mode", True)
    monkeypatch.setattr(settings, "platform_admin_emails", "alex@ascme-labs.com")
    return auth


def _owned_project(client, auth, name: str) -> str:
    """A project the session can actually read. Rows written straight to the table are
    invisible to `require_readable`, which is the guard working."""
    return client.post("/api/projects", json={"name": name}, headers=auth).json()["id"]


def _user(db, email: str) -> str:
    row = db.scalar(select(User).where(User.email == email))
    assert row is not None, email
    return row.id


def _org(db, org_id: str, *, share: bool = False, admin: str | None = None) -> str:
    db.add(Organization(id=org_id, name=org_id, telemetry_share=share))
    # Committed BEFORE the membership: the unit of work has no reason to order two independent
    # inserts, and SQLite's foreign keys are on.
    db.commit()
    if admin:
        db.add(OrgMembership(org_id=org_id, user_id=admin, role="admin"))
        db.commit()
    return org_id


def _project(db, project_id: str, org_id: str | None, tag: str) -> str:
    db.add(Project(id=project_id, name=project_id, tag=tag, org_id=org_id))
    db.commit()
    return project_id


def _roll(db, project_id: str, *, week="2026-W36", vendor="gbagent", model="qwen3.6",
          finished=10, signed_off=8, lane="backend", version="1.0.0"):
    db.add(HarnessRollup(project_id=project_id, week=week, vendor=vendor, model=model,
                         binary_version=version, capability="other", lane=lane, tier="cheap",
                         task_class="general", size_band="M", finished=finished,
                         signed_off=signed_off, bounced=finished - signed_off,
                         median_seconds=100, tokens_reported=0, signed_off_reported=0,
                         first_choice=finished, fallback=0, explicit=0, unknown=0))
    db.commit()


# ---- 14: org scope -----------------------------------------------------------------------------

def test_org_scope_aggregates_the_orgs_projects_and_breaks_them_down(client, auth, db):
    """14. Sabotage: aggregate with a different summation from the project view and the two
    numbers stop agreeing, which is the whole reason `_shape` is shared."""
    alex = _user(db, "alex@ascme-labs.com")
    _org(db, "org_a", admin=alex)
    _project(db, "p_one", "org_a", "PONE")
    _project(db, "p_two", "org_a", "PTWO")
    _roll(db, "p_one", finished=6, signed_off=6)
    _roll(db, "p_two", finished=4, signed_off=1)

    r = client.get("/api/harness?org_id=org_a", headers=auth)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["scope"] == "org" and sorted(out["projects"]) == ["p_one", "p_two"]
    assert len(out["cells"]) == 1
    cell = out["cells"][0]
    assert cell["finished"] == 10 and cell["signed_off"] == 7
    assert cell["by_project"] == [
        {"project_id": "p_one", "finished": 6, "signed_off": 6},
        {"project_id": "p_two", "finished": 4, "signed_off": 1},
    ]


def test_org_scope_refuses_a_member_who_is_not_an_admin(client, auth, db):
    """14. A project member sees project scope; the org view is the admin's."""
    kate = _user(db, "kate@ascme-labs.com")
    alex = _user(db, "alex@ascme-labs.com")
    _org(db, "org_b", admin=alex)
    db.add(OrgMembership(org_id="org_b", user_id=kate, role="member"))
    db.commit()
    token = client.post("/api/auth/login", json={"email": "kate@ascme-labs.com",
                                                 "password": "graphban"}).json()["access_token"]
    r = client.get("/api/harness?org_id=org_b", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (403, 404), r.text
    # And the admin can.
    assert client.get("/api/harness?org_id=org_b", headers=auth).status_code == 200


def test_naming_neither_scope_is_refused_rather_than_listing_everything(client, auth):
    """14. `require_readable` fails closed on a null project by design; this must not be the
    door around it."""
    assert client.get("/api/harness", headers=auth).status_code == 422


# ---- 15: the platform overlay ------------------------------------------------------------------

def _three_orgs(db, *, share=(True, True, True), finished=(10, 10, 10), signed=(8, 8, 8)):
    for i, (s, f, g) in enumerate(zip(share, finished, signed), start=1):
        _org(db, f"org_{i}", share=s)
        _project(db, f"proj_{i}", f"org_{i}", f"PRJ{i}")
        _roll(db, f"proj_{i}", finished=f, signed_off=g)


def test_a_cell_with_two_contributing_orgs_is_not_served(client, auth, db, hosted):
    """15. Sabotage: drop the org count and two orgs' cells become an "average"."""
    _three_orgs(db, share=(True, True, False))
    assert hsvc.platform_roll(db) == 1
    db.commit()
    row = db.scalars(select(PlatformRollup)).all()[0]
    assert row.orgs_contributing == 2
    assert hsvc._platform_cell({"orgs": 2, "finished": 40, "signed_off": 30,
                                "top_share": 0.3})["rate"] is None


def test_three_orgs_and_enough_attempts_are_served_as_a_band_never_a_count(
        client, auth, db, hosted):
    """15. The banded `n` is the defence against a contributor subtracting itself out."""
    _three_orgs(db, finished=(10, 10, 10), signed=(8, 8, 8))
    hsvc.platform_roll(db)
    db.commit()
    row = db.scalars(select(PlatformRollup)).all()[0]
    assert row.orgs_contributing == 3 and row.finished == 30
    served = hsvc._platform_cell({"orgs": 3, "finished": 30, "signed_off": 24,
                                  "top_share": 0.34})
    assert served["rate"] == 0.8
    assert served["n"] == "20–49"
    assert "finished" not in served and isinstance(served["n"], str)


def test_a_cell_one_org_dominates_is_not_served(client, auth, db, hosted):
    """15. Three orgs where one holds 70% is one org with two witnesses."""
    _three_orgs(db, finished=(70, 15, 15), signed=(50, 10, 10))
    hsvc.platform_roll(db)
    db.commit()
    row = db.scalars(select(PlatformRollup)).all()[0]
    assert row.top_org_share is not None and row.top_org_share > hsvc.PLATFORM_MAX_ORG_SHARE
    assert hsvc._platform_cell({"orgs": 3, "finished": row.finished,
                                "signed_off": row.signed_off,
                                "top_share": row.top_org_share})["rate"] is None


def test_an_org_with_too_few_of_its_own_does_not_count_as_a_contributor(
        client, auth, db, hosted):
    """15. Sabotage: count every org and a dominant pair is laundered by a third with one."""
    _three_orgs(db, finished=(20, 20, 2), signed=(15, 15, 1))
    hsvc.platform_roll(db)
    db.commit()
    row = db.scalars(select(PlatformRollup)).all()[0]
    assert row.orgs_contributing == 2, "the third org has fewer than five in this cell"
    assert row.finished == 42, "its attempts still sum — dropping them would bias the average"


def test_an_org_that_has_not_opted_in_contributes_nothing(client, auth, db, hosted):
    """15."""
    _three_orgs(db, share=(True, True, True))
    _org(db, "org_out", share=False)
    _project(db, "proj_out", "org_out", "POUT")
    _roll(db, "proj_out", finished=100, signed_off=100)
    hsvc.platform_roll(db)
    db.commit()
    row = db.scalars(select(PlatformRollup)).all()[0]
    assert row.finished == 30, "the opted-out org's hundred attempts are not in the average"


def test_opting_out_recomputes_at_once_and_leaves_nothing_behind(client, auth, db, hosted):
    """15 / D13. Opt-in that keeps your numbers after you leave is not opt-in."""
    alex = _user(db, "alex@ascme-labs.com")
    _three_orgs(db)
    db.add(OrgMembership(org_id="org_1", user_id=alex, role="admin"))
    db.commit()
    hsvc.platform_roll(db)
    db.commit()
    assert db.scalars(select(PlatformRollup)).all()[0].finished == 30

    r = client.put("/api/harness/platform/share", headers=auth,
                   json={"org_id": "org_1", "telemetry_share": False})
    assert r.status_code == 200, r.text
    db.expire_all()
    rows = db.scalars(select(PlatformRollup)).all()
    assert rows[0].finished == 20 and rows[0].orgs_contributing == 2


def test_the_served_payload_carries_no_org_or_project_id(client, auth, db, hosted):
    """15. What crosses the boundary is a count per cell per week, and nothing else."""
    alex = _user(db, "alex@ascme-labs.com")
    _three_orgs(db)
    db.add(OrgMembership(org_id="org_1", user_id=alex, role="admin"))
    db.commit()
    hsvc.platform_roll(db)
    db.commit()

    mine = _owned_project(client, auth, "Mine")
    db.expire_all()
    project = db.get(Project, mine)
    project.org_id = "org_1"
    db.commit()
    _roll(db, mine)
    out = client.get(f"/api/harness?project_id={mine}", headers=auth).json()
    body = str(out["cells"][0]["platform"])
    for leak in ("org_", "proj_", "2026-W"):
        assert leak not in body, body


def test_a_self_hosted_instance_says_why_there_is_no_overlay(client, auth, db):
    """15. `platform: null` with a reason, never a null a reader can mistake for "not yet"."""
    solo = _owned_project(client, auth, "Solo")
    _roll(db, solo)
    out = client.get(f"/api/harness?project_id={solo}", headers=auth).json()
    assert out["platform"] is None
    assert "self-hosted" in out["platform_reason"]


def test_the_share_toggle_and_the_roll_are_hosted_only(client, auth, db):
    """15. Self-hosted has no overlay and no way to turn one on."""
    alex = _user(db, "alex@ascme-labs.com")
    _org(db, "org_z", admin=alex)
    r = client.put("/api/harness/platform/share", headers=auth,
                   json={"org_id": "org_z", "telemetry_share": True})
    assert r.status_code == 404, r.text


def test_the_operator_sees_the_platform_view_whole_with_its_skew(client, auth, db, operator):
    """15 / D13. The floor protects orgs from each other, not the operator from the instance
    they run — but the operator must see how lopsided an average is before quoting it."""
    _three_orgs(db, finished=(70, 15, 15), signed=(50, 10, 10))
    hsvc.platform_roll(db)
    db.commit()
    r = client.get("/api/admin/harness/platform", headers=operator)
    assert r.status_code == 200, r.text
    cell = r.json()["cells"][0]
    assert cell["orgs_contributing"] == 3
    assert cell["top_org_share"] > 0.6 and cell["served"] is False
