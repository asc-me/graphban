"""A project key that fails falls back to the default — and says so (GRPH-525, PRD-25 §4).

§4 originally refused this fallback: *"silently substituting a different model for the one a
project chose is how the wrong thing runs for a week."* The author settled it by removing the
premise rather than overruling the objection — **the objection was to the SILENCE, not to the
fallback.** A fallback that announces itself is a project that keeps working and an operator who
knows why.

So there are two claims here and the second is the one that is easy to ship broken:

1. an unusable project credential is fallen past
2. **the substitution is REPORTED** — and `source` does not do that. `source="deployment"` looks
   identical whether the project asked for that credential or was quietly moved onto it, which
   is exactly the week-long silence §4 was written against.

`test_the_listing_shows_which_projects_are_falling_back` is the load-bearing one: the log line
and the `Resolved` field are both invisible to an operator who is not reading logs, and the
console is the only surface they see without going looking.
"""
from __future__ import annotations

import logging

import pytest

from app.models import Credential, DeploymentConfig, Organization, Project
from app.security import secrets
from app.services import platform as platform_svc


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def project(db):
    p = Project(id="p1", name="P1", tag="P1")
    db.add(p)
    db.commit()
    return p


def _cred(db, cid, *, state="valid", org_id=None, model="claude-x"):
    c = Credential(id=cid, kind="anthropic", org_id=org_id, model=model, label=cid,
                   api_key=secrets.encrypt("sk"), state=state)
    db.add(c)
    db.commit()
    return c


# ---- the load-bearing one -----------------------------------------------------------------


def test_the_listing_shows_which_projects_are_falling_back(db, project):
    """THE POINT. An operator does not read logs, and a field on a return value is not a
    warning. The credentials view is where a substitution has to be visible, next to the row
    that failed.

    A version that falls back correctly and reports nothing passes every other assertion in
    this file except the `fell_back_from` ones — and it is precisely the silent substitution
    §4 refused.
    """
    broken = _cred(db, "cred_broken", state="unreachable")
    good = _cred(db, "cred_default")
    db.add(DeploymentConfig(scope="", default_credential_id=good.id))
    project.credential_id = broken.id
    db.commit()

    rows = {r["id"]: r for r in platform_svc.list_credentials(db, "")}

    assert rows["cred_broken"]["used_by"] == ["p1"], "the pointer is still recorded"
    assert rows["cred_broken"]["falling_back"] == ["p1"], (
        "the console does not show that p1 is not actually getting this credential — the "
        "substitution is invisible to anyone not reading logs"
    )
    assert rows["cred_default"]["falling_back"] == [], "the default is not being fallen past"


# ---- claim 1: it falls back ----------------------------------------------------------------


def test_an_unreachable_project_credential_falls_back_to_the_default(db, project):
    broken = _cred(db, "cred_broken", state="unreachable")
    good = _cred(db, "cred_default", model="claude-default")
    db.add(DeploymentConfig(scope="", default_credential_id=good.id))
    project.credential_id = broken.id
    db.commit()

    resolved = platform_svc.resolve_chat(db, "p1")

    assert resolved.credential_id == "cred_default"
    assert resolved.source == "deployment"


def test_a_pending_project_credential_is_still_used(db, project):
    """Only `unreachable` is fallen past. `pending_validation` means nobody has ASKED yet —
    treating "unproven" as "broken" would drop a project onto the default the moment it was
    configured, before a single probe had run."""
    fresh = _cred(db, "cred_fresh", state="pending_validation", model="claude-fresh")
    _cred(db, "cred_default")
    db.add(DeploymentConfig(scope="", default_credential_id="cred_default"))
    project.credential_id = fresh.id
    db.commit()

    resolved = platform_svc.resolve_chat(db, "p1")

    assert resolved.credential_id == "cred_fresh"
    assert resolved.fell_back_from == ""


def test_the_default_is_NOT_fallen_past_when_unreachable(db, project):
    """The deliberate asymmetry. There is nothing below the default but the stub, so routing
    around it would hide a broken default forever — the deployment would run permanently on
    its fallback while the console showed health."""
    broken = _cred(db, "cred_broken", state="unreachable")
    db.add(DeploymentConfig(scope="", default_credential_id=broken.id))
    db.commit()

    resolved = platform_svc.resolve_chat(db, "p1")

    assert resolved.credential_id == "cred_broken", (
        "an unreachable DEFAULT was routed around — a broken default is now undiscoverable"
    )
    assert resolved.source == "deployment"


# ---- claim 2: it says so --------------------------------------------------------------------


def test_the_resolution_carries_what_the_project_asked_for(db, project):
    """`source` cannot answer this. `source="deployment"` is the same value whether the project
    pointed at that credential or was moved onto it."""
    broken = _cred(db, "cred_broken", state="unreachable")
    _cred(db, "cred_default")
    db.add(DeploymentConfig(scope="", default_credential_id="cred_default"))
    project.credential_id = broken.id
    db.commit()

    resolved = platform_svc.resolve_chat(db, "p1")

    assert resolved.fell_back_from == "cred_broken"
    assert resolved.substituted is True


def test_an_ordinary_resolution_reports_no_substitution(db, project):
    """The counterpart. If `substituted` were always true it would carry no information."""
    good = _cred(db, "cred_p")
    project.credential_id = good.id
    db.commit()

    resolved = platform_svc.resolve_chat(db, "p1")

    assert resolved.fell_back_from == "" and resolved.substituted is False


def test_falling_all_the_way_to_the_stub_still_reports_it(db, project):
    """No default configured. The project gets the stub, and still has to be told it asked for
    something else — this is the case §4's original wording was most worried about."""
    broken = _cred(db, "cred_broken", state="unreachable")
    project.credential_id = broken.id
    db.commit()

    resolved = platform_svc.resolve_chat(db, "p1")

    assert resolved.provider_id == "stub"
    assert resolved.fell_back_from == "cred_broken"


def test_a_cross_scope_pointer_also_reports_the_substitution(db):
    """"Unusable" is not only `unreachable` — a pointer at another org's row fails to produce
    a credential too, and the operator needs the same warning."""
    db.add(Organization(id="org_other", name="other"))
    mine = Project(id="mine", name="Mine", tag="MINE")
    db.add(mine)
    db.commit()
    _cred(db, "cred_theirs", org_id="org_other")
    _cred(db, "cred_default")
    db.add(DeploymentConfig(scope="", default_credential_id="cred_default"))
    mine.credential_id = "cred_theirs"
    db.commit()

    resolved = platform_svc.resolve_chat(db, "mine")

    assert resolved.credential_id == "cred_default"
    assert resolved.fell_back_from == "cred_theirs"


def test_the_fallback_is_logged_with_both_names(db, project):
    """A log line naming neither the project nor the credential sends the operator hunting.

    **Captured with its own handler rather than `caplog`, and that is not a style preference.**
    `observability.configure_logging()` does `root.handlers[:] = [handler]` — it REPLACES the
    root handlers, including the one pytest's caplog installs — and on Postgres Alembic's
    `fileConfig` resets logging a second time during `run_migrations()`. So this test passed on
    SQLite and failed on Postgres for reasons that had nothing to do with the code under test:
    the warning was emitted both times and only one run could see it.

    Attaching a handler to the logger itself, after the fixtures have finished starting the
    app, is immune to both.
    """
    broken = _cred(db, "cred_broken", state="unreachable")
    _cred(db, "cred_default")
    db.add(DeploymentConfig(scope="", default_credential_id="cred_default"))
    project.credential_id = broken.id
    db.commit()

    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger("graphban.platform")
    handler = Capture(level=logging.WARNING)
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.WARNING)
    try:
        platform_svc.resolve_chat(db, "p1")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    # getMessage(), not .message — the latter only exists once a formatter has run.
    msgs = [r.getMessage() for r in records]
    assert any("p1" in m and "cred_broken" in m for m in msgs), msgs
