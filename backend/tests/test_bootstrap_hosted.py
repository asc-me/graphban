"""The first operator on a hosted instance (GRPH-219).

The hosted deployment had no way to reach its own first user, and it was not a theory:
verified live on 2026-08-11, `cloud.graphban.dev` had zero users, zero orgs and zero
invites, and no supported route to a first login.

    signup_mode=invite_only  → registration refused without a token (auth.py)
    issue a platform invite  → needs a platform-admin JWT
    a platform-admin JWT     → needs an account

`bootstrap.check_allowed` refuses hosted mode and advises "invite the first operator
instead", which is exactly the step the cycle makes impossible.

What makes this safe where `provision` is not: it mints **no API key**. That is the
property to defend — an unauthenticated key mint on a multi-tenant deployment is a hole,
and it is the reason hosted bootstrap was refused rather than merely unbuilt.
"""
import pytest

from app import bootstrap
from app.models import ApiKey, Organization, OrgMembership, Project, User


@pytest.fixture()
def db(_clean_database):
    """Deliberately NOT depending on `client`.

    A virgin instance is the entire precondition, and the `client` fixture runs the app's
    lifespan, which seeds the prototype dataset — so asking for it would make every test
    here run against an instance that already has users. Deleting them afterwards is not an
    option either: seeded projects and memberships reference them, so it fails on the
    foreign keys. The autouse reset already leaves every table empty; just open a session.
    """
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def hosted(monkeypatch):
    """A hosted, unseeded, virgin instance with an admin allowlist — the real shape."""
    from app.config import settings

    monkeypatch.setattr(settings, "hosted_mode", True)
    monkeypatch.setattr(settings, "seed_on_start", False)
    monkeypatch.setattr(settings, "platform_admin_emails", "boss@example.com")
    # Required for a hosted instance to boot at all: `check_security` refuses HOSTED_MODE
    # without it, so tenants' BYOK provider keys can never sit in plaintext. Without this
    # the fixture is not a hosted instance, it is a hosted instance that would refuse to
    # start — and the gap is invisible on SQLite, where check_security returns early.
    # Caught by the Postgres run, which is the only one that exercises real startup.
    monkeypatch.setattr(settings, "secret_encryption_key", "x" * 32)


def test_it_creates_the_operator_and_their_org(db, hosted):
    out = bootstrap.provision_hosted(
        db, email="boss@example.com", org_name="ascme-labs", name="Alex Cain")

    assert out["provisioned"] is True
    assert out["org_name"] == "ascme-labs"
    user = db.query(User).one()
    assert user.email == "boss@example.com" and user.initials == "AC"
    org = db.query(Organization).one()
    membership = db.query(OrgMembership).one()
    assert membership.org_id == org.id and membership.user_id == user.id
    assert membership.role == "owner"


def test_the_returned_password_actually_signs_in(db, hosted):
    """The AL-286 dead end, guarded: a provisioned account that cannot log in is worse
    than no account, because it looks done. That bug was an email the users table accepted
    and Pydantic's EmailStr on the login route then rejected — one layer down from where
    anyone was looking.

    The client is built AFTER provisioning rather than taken as a fixture: its lifespan
    seeds, and seeding first would mean this never ran against a virgin instance at all.
    """
    from fastapi.testclient import TestClient

    out = bootstrap.provision_hosted(db, email="boss@example.com", org_name="ascme-labs")

    from app.main import app

    with TestClient(app) as c:
        r = c.post("/api/auth/login",
                   json={"email": "boss@example.com", "password": out["password"]})

    assert r.status_code == 200, r.text
    assert r.json()["access_token"]


def test_it_mints_no_api_key(db, hosted):
    """THE security property, and the whole reason hosted bootstrap was refused rather
    than simply missing. An unauthenticated key mint on a multi-tenant deployment is a
    hole; creating a login the operator must then use is not."""
    bootstrap.provision_hosted(db, email="boss@example.com", org_name="ascme-labs")

    assert db.query(ApiKey).count() == 0


def test_it_creates_no_project(db, hosted):
    """Everything past the first login goes through the product — which is also what makes
    the onboarding path worth trusting once somebody has walked it."""
    before = db.query(Project).count()

    bootstrap.provision_hosted(db, email="boss@example.com", org_name="ascme-labs")

    assert db.query(Project).count() == before


def test_it_refuses_an_email_that_cannot_reach_the_operator_console(db, hosted):
    """The mistake that leaves you exactly where you started: an account that signs in
    fine and still cannot issue the first invite, because platform admin is an env
    allowlist rather than anything on the row."""
    with pytest.raises(bootstrap.BootstrapRefused) as e:
        bootstrap.provision_hosted(db, email="nobody@example.com", org_name="ascme-labs")

    assert "PLATFORM_ADMIN_EMAILS" in str(e.value)
    assert db.query(User).count() == 0, "and it creates nothing"


def test_it_refuses_on_a_self_host(db, monkeypatch):
    """Pointing at `graphban init` rather than doing something subtly different — that one
    also creates a project and a first key, which a self-host wants and hosted must not."""
    from app.config import settings

    monkeypatch.setattr(settings, "hosted_mode", False)

    with pytest.raises(bootstrap.BootstrapRefused) as e:
        bootstrap.provision_hosted(db, email="boss@example.com", org_name="x")

    assert "graphban init" in str(e.value)


def test_it_refuses_when_seeding_would_race_it(db, hosted, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "seed_on_start", True)

    with pytest.raises(bootstrap.BootstrapRefused):
        bootstrap.provision_hosted(db, email="boss@example.com", org_name="x")


def test_a_second_run_changes_nothing(db, hosted):
    """Idempotent by refusal, like `provision`. A second run must not create a rival
    operator — on a multi-tenant instance that is a second person with the console."""
    first = bootstrap.provision_hosted(db, email="boss@example.com", org_name="ascme-labs")

    second = bootstrap.provision_hosted(db, email="boss@example.com", org_name="other")

    assert first["provisioned"] is True
    assert second["provisioned"] is False and "already has users" in second["reason"]
    assert db.query(User).count() == 1
    assert db.query(Organization).count() == 1


def test_the_cli_wires_the_command(db, hosted, capsys):
    """The seam. Asserting on `provision_hosted` alone would pass just as well if the
    parser never reached it."""
    from app.cli import build_parser

    args = build_parser().parse_args(
        ["admin", "bootstrap-hosted", "--email", "boss@example.com",
         "--org-name", "ascme-labs", "--json"])
    args.func(args)

    import json as _json
    out = _json.loads(capsys.readouterr().out)
    assert out["provisioned"] is True and out["org_name"] == "ascme-labs"
