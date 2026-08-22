"""First-run provisioning (AL-283 / PRD-14 D3).

Issuing the first credential is the purest AUTHORITY gate there is, and it was the one
thing blocking a zero-browser install: signup and key minting are both JWT/UI-only, and
an agent cannot bootstrap itself into existing. PRD-14's answer is not to relax the gate
but to satisfy it out of band — an operator runs a script on the box they already own.

So the tests that matter are the REFUSALS. A bootstrap path that mints a credential
without authenticating anyone is only safe where "no users yet" really does imply "the
person running this owns the deployment", and these pin every case where it doesn't.
"""
import pytest
from sqlalchemy import select

from app import bootstrap
from app.models import ApiKey, Membership, Project, User
from app.services import projects as projects_svc


@pytest.fixture()
def db(client):
    """A migrated but EMPTY instance — the only fixture here that wants one, since
    provisioning is defined by "this database has no users".

    `client` seeds the prototype dataset, so every table is cleared in REVERSE
    dependency order. Deleting a hand-picked few (users, projects, keys) passes on
    SQLite and fails on Postgres, which enforces foreign keys immediately: seeded items
    still reference `projects.core`. Let the metadata supply the order rather than
    guessing it."""
    from app.db import Base, SessionLocal

    session = SessionLocal()
    for table in reversed(Base.metadata.sorted_tables):
        session.execute(table.delete())
    session.commit()
    try:
        yield session
    finally:
        session.close()


# ---- the happy path -----------------------------------------------------------------
def test_provisioning_yields_a_working_agent_credential(client, db, monkeypatch):
    monkeypatch.setattr(bootstrap.settings, "seed_on_start", False)
    out = bootstrap.provision(db, project_name="My Repo")
    assert out["provisioned"] is True

    # The credential is the deliverable: it must authenticate MCP with no further setup.
    r = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "get_context", "arguments": {}}},
        headers={"X-API-Key": out["api_key"]},
    )
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result.get("isError") is not True, result
    assert out["project_id"] in result["structuredContent"]["writable_projects"]


def test_the_operator_can_sign_in_with_what_was_printed(client, db, monkeypatch):
    """An account nobody can log into is a dead end — the human is the reviewer at the
    quality gates eventually, so the printed credential has to actually work.

    Uses the DEFAULT email deliberately. The first version of this test passed an explicit
    `me@example.com` and so proved nothing about the value real operators actually get:
    the default was `operator@localhost`, which the users table accepted (plain String)
    and the login endpoint rejected (EmailStr — no TLD). The account provisioned fine and
    could never sign in. Test the default, because the default is what ships."""
    monkeypatch.setattr(bootstrap.settings, "seed_on_start", False)
    out = bootstrap.provision(db, project_name="My Repo")
    assert out["email"] == bootstrap.DEFAULT_EMAIL
    r = client.post("/api/auth/login",
                    json={"email": out["email"], "password": out["password"]})
    assert r.status_code == 200, r.text


def test_a_custom_operator_email_also_signs_in(client, db, monkeypatch):
    monkeypatch.setattr(bootstrap.settings, "seed_on_start", False)
    out = bootstrap.provision(db, project_name="My Repo", email="me@example.com")
    r = client.post("/api/auth/login",
                    json={"email": "me@example.com", "password": out["password"]})
    assert r.status_code == 200, r.text


def test_the_operator_owns_the_project(client, db, monkeypatch):
    """Without the owner Membership the project would be invisible to its own creator."""
    monkeypatch.setattr(bootstrap.settings, "seed_on_start", False)
    out = bootstrap.provision(db, project_name="My Repo")
    m = db.scalars(select(Membership).where(Membership.project_id == out["project_id"])).all()
    assert [(x.role, x.access) for x in m] == [("owner", "write")]


def test_the_tag_is_derived_and_valid(client, db, monkeypatch):
    """PRD-13's grammar is satisfied for free — no operator has to invent a tag."""
    from app import tagging

    monkeypatch.setattr(bootstrap.settings, "seed_on_start", False)
    out = bootstrap.provision(db, project_name="Graphban Web")
    # `validate` RAISES on a bad tag and returns the normalized value, so compare against
    # it — an `is None or True` style assertion here would be a tautology that can't fail.
    assert tagging.validate(out["project_tag"]) == out["project_tag"] == "GW"


def test_a_generated_password_is_not_guessable(client, db, monkeypatch):
    """Decided by building it (the item was high-fidelity for this reason): a random
    password beats passwordless-local, because a box that later gets exposed shouldn't
    have an open door on it."""
    monkeypatch.setattr(bootstrap.settings, "seed_on_start", False)
    first = bootstrap.provision(db, project_name="A")["password"]
    assert len(first) >= 20
    assert first not in ("graphban", "password", "admin", "operator@localhost")


# ---- the refusals -------------------------------------------------------------------
def test_a_second_run_changes_nothing(client, db, monkeypatch):
    """Re-running must not mint a second operator or a second credential."""
    monkeypatch.setattr(bootstrap.settings, "seed_on_start", False)
    bootstrap.provision(db, project_name="My Repo")
    before = len(db.scalars(select(ApiKey)).all()), len(db.scalars(select(User)).all())

    again = bootstrap.provision(db, project_name="My Repo")
    assert again["provisioned"] is False
    assert "already has users" in again["reason"]
    assert (len(db.scalars(select(ApiKey)).all()), len(db.scalars(select(User)).all())) == before


def test_it_refuses_on_a_hosted_instance(client, db, monkeypatch):
    """The one that would be a security hole. On a multi-tenant deployment, "no users
    yet" says nothing about whether the caller owns it."""
    monkeypatch.setattr(bootstrap.settings, "hosted_mode", True)
    with pytest.raises(bootstrap.BootstrapRefused, match="HOSTED"):
        bootstrap.provision(db, project_name="My Repo")
    assert db.scalars(select(User)).all() == []


def test_it_refuses_when_seeding_is_on(client, db, monkeypatch):
    """Both key off zero users and seeding runs during lifespan startup, so seeding wins
    the race and the operator silently gets the prototype dataset instead of their own
    project. Refuse rather than pick one."""
    monkeypatch.setattr(bootstrap.settings, "hosted_mode", False)
    monkeypatch.setattr(bootstrap.settings, "seed_on_start", True)
    with pytest.raises(bootstrap.BootstrapRefused, match="SEED_ON_START"):
        bootstrap.provision(db, project_name="My Repo")
    assert db.scalars(select(User)).all() == []


def test_the_refusals_are_checked_before_anything_is_written(client, db, monkeypatch):
    """A refusal must leave NO partial state — half a provisioned instance is worse than
    none, because the zero-users guard would then consider it already set up."""
    monkeypatch.setattr(bootstrap.settings, "hosted_mode", True)
    with pytest.raises(bootstrap.BootstrapRefused):
        bootstrap.provision(db, project_name="My Repo")
    assert db.scalars(select(User)).all() == []
    assert db.scalars(select(Project)).all() == []
    assert db.scalars(select(ApiKey)).all() == []


# ---- what the AL-286 acceptance walk caught -----------------------------------------
def test_a_bootstrapped_project_can_read_back_its_own_memory(client, db, monkeypatch):
    """D1 and D3 did not compose. Each was right alone: `review` is the correct default
    for an existing project, and provisioning correctly created one. Together they meant
    a zero-browser install stopped at the first memory the agent wrote — it landed as a
    candidate and `search_memory` returned nothing, with no human around to publish it.

    Only a project created BY THIS SCRIPT is trusted. Running it is an explicit request
    for an agent-driven instance, the project is brand new so there is no corpus to
    poison, and every publish is labelled and undoable."""
    monkeypatch.setattr(bootstrap.settings, "seed_on_start", False)
    out = bootstrap.provision(db, project_name="Zero Browser")
    assert out["memory_write_mode"] == "trusted"

    def mcp(tool, args):
        r = client.post("/api/mcp",
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": tool, "arguments": args}},
                        headers={"X-API-Key": out["api_key"]})
        import json as _json
        return _json.loads(r.json()["result"]["content"][0]["text"])

    written = mcp("add_memory", {"text": "Migrations run on API startup."})
    assert written["status"] == "published", written
    found = mcp("search_memory", {"query": "migrations on startup"})
    assert found["returned"] == 1, found


def test_provisioning_does_not_change_an_existing_projects_write_mode(client, db, monkeypatch):
    """The trusted default is scoped to the project this script creates. Nothing else on
    the instance moves, and migration 0040 still defaults existing projects to review."""
    from app.models import Project

    monkeypatch.setattr(bootstrap.settings, "seed_on_start", False)
    out = bootstrap.provision(db, project_name="First")
    other = projects_svc.create_project(db, name="Made In The UI",
                                        owner_user_id=db.scalars(select(User)).first().id)
    assert other.memory_write_mode == "review"
    assert db.get(Project, out["project_id"]).memory_write_mode == "trusted"


# ---- what the PRD-22 acceptance walk caught (GRPH-461) -------------------------------
#
# The comment above `DEFAULT_EMAIL` already knew this: `operator@localhost` provisioned
# happily and could not sign in, found by the AL-286 walk. The fix was to change the
# DEFAULT — which repaired the one value the author controlled and left the dead end
# reachable for every value a user supplies. `--email not-an-email` still reported
# `provisioned: true` and handed back a password for an account the login route refuses.
#
# `test_the_operator_can_sign_in_with_what_was_printed` says "test the default, because
# the default is what ships". True, and incomplete: `--email` is on the command line, so
# what a user passes ships too.

@pytest.mark.parametrize(
    "email",
    [
        "not-an-email",       # no @ at all
        "operator@localhost", # the exact address AL-286 found, still accepted until now
        "me@example.invalid", # looks like an address; RFC 2606 reserved, refused by login
        "",                   # the empty string, which `handle` would have made "operator"
    ],
)
def test_an_address_nobody_could_sign_in_with_is_refused(client, db, monkeypatch, email):
    monkeypatch.setattr(bootstrap.settings, "seed_on_start", False)
    with pytest.raises(bootstrap.BootstrapRefused) as exc:
        bootstrap.provision(db, project_name="My Repo", email=email)
    assert "sign in" in str(exc.value)


@pytest.mark.parametrize("email", ["not-an-email", "operator@localhost"])
def test_a_refused_address_leaves_the_instance_untouched(client, db, monkeypatch, email):
    """Refused BEFORE anything is written, for the reason the existing refusal test
    gives: a half-provisioned instance is worse than none, because the second run then
    reports "this instance already has users; nothing was changed" and the human is left
    with no account and no way forward."""
    monkeypatch.setattr(bootstrap.settings, "seed_on_start", False)
    with pytest.raises(bootstrap.BootstrapRefused):
        bootstrap.provision(db, project_name="My Repo", email=email)
    assert db.scalars(select(User)).all() == []
    assert db.scalars(select(Project)).all() == []
    assert db.scalars(select(ApiKey)).all() == []


@pytest.mark.parametrize(
    "email",
    ["not-an-email", "operator@localhost", "me@example.invalid", "me@example.com",
     bootstrap.DEFAULT_EMAIL],
)
def test_provisioning_and_login_refuse_exactly_the_same_addresses(
    client, db, monkeypatch, email
):
    """The assertion that makes this non-vacuous.

    A test that only checked "init refuses a bad address" would pass for a hand-rolled
    check that agrees today and drifts tomorrow — and drift is the whole defect: the
    users table takes a plain String, the login route takes `EmailStr`, and the gap
    between them is where an unusable account lives. So this asserts they refuse the
    SAME set, including the addresses that must still be ACCEPTED.

    Driven through the real login endpoint rather than against the adapter, because the
    adapter agreeing with itself proves nothing.
    """
    monkeypatch.setattr(bootstrap.settings, "seed_on_start", False)

    try:
        bootstrap.check_email(email)
        init_refused = False
    except bootstrap.BootstrapRefused:
        init_refused = True

    # 401 means the address parsed and the credentials were wrong; 422 means the address
    # itself was refused. Only the second is the comparison being made here.
    response = client.post("/api/auth/login", json={"email": email, "password": "x" * 12})
    login_refused = response.status_code == 422

    assert init_refused == login_refused, (
        f"{email!r}: provisioning {'refused' if init_refused else 'accepted'} it and "
        f"login {'refused' if login_refused else 'accepted'} it — the gap between those "
        "two is exactly where an account nobody can sign into lives"
    )
