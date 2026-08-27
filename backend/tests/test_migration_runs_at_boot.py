"""The credential migration runs at boot (GRPH-538).

**PRD-25 S6 removed resolution step 0 and nothing ran the migration that must precede it.**
`credential_migration.migrate()` was written, tested and sabotaged eight ways — and unreachable.
No call site anywhere in `app/`.

What that would have done on the reference deployment, measured at the moment S6 merged: three
projects still held a legacy `providers` blob, and step 0 was gone. Their `credential_id` was
never written, so they would have fallen past an unset deployment default to the STUB — every AI
feature silently degraded to the offline placeholder, nothing raising.

That is the downgrade step 0 existed to prevent, arriving in the slice that removed it.

**Why S6's own tests could not catch it.** They call `migrate(db)` directly, which proves the
function is correct and says nothing about whether anything runs it. Same gap as GRPH-496's
heartbeat (constructed by nothing) and S2b's `retry_now` (no endpoint) — third instance in one
arc, which is why this file asserts by BOOTING rather than by reading `main.py`.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.models import Credential, DeploymentConfig, PlatformConfig, Project
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


def _legacy_project(db, pid="p_legacy", *, key="sk-live", model="claude-x"):
    """A project configured the OLD way, exactly as one on a real deployment would be."""
    if db.get(Project, pid) is None:
        db.add(Project(id=pid, name=pid.upper(), tag="PLEG"))
        db.commit()
    cfg = platform_svc.get_config(db, pid)
    cfg.providers = {"anthropic": {"base_url": "", "api_key": secrets.encrypt(key),
                                   "chat_model": model}}
    cfg.active_chat_provider = "anthropic"
    db.commit()
    return pid


def _boot():
    """Run a real lifespan. This is the whole point of the file."""
    from app.main import app

    with TestClient(app) as c:
        c.get("/health")


# ---- the one that matters ------------------------------------------------------------------


def test_booting_migrates_a_project_configured_the_old_way(db):
    """THE POINT. Not "migrate() works" — S6 proved that. This asks whether anything RUNS it.

    A legacy-configured project is created, the app boots, and the project must come out with
    a credential pointer. With the call missing from `lifespan` this fails, and the project
    resolves to the stub — which is what would have shipped.
    """
    pid = _legacy_project(db)
    assert db.get(Project, pid).credential_id is None

    _boot()

    db.expire_all()
    assert db.get(Project, pid).credential_id is not None, (
        "booting did not migrate a legacy-configured project — step 0 is gone and this "
        "project now resolves to the stub"
    )


def test_and_it_then_resolves_through_the_new_pointer(db):
    """The consequence, stated separately: a pointer that exists but does not resolve would
    satisfy the test above while leaving the deployment just as broken."""
    pid = _legacy_project(db, model="claude-x")

    _boot()

    db.expire_all()
    resolved = platform_svc.resolve_chat(db, pid)
    assert resolved.source == "project"
    assert resolved.provider_id == "anthropic"
    assert resolved.model == "claude-x"


# ---- it must not be able to stop the app starting -------------------------------------------


def test_a_failing_migration_does_not_stop_the_app_booting(db, monkeypatch):
    """A deployment that cannot boot is worse than one whose migration needs another attempt —
    and because the migration is idempotent, the next boot simply tries again.

    Forced to fail on every call, so a version that gives up after one error also fails here.
    """
    from app.services import credential_migration

    monkeypatch.setattr(credential_migration, "migrate",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("migration broke")))

    _boot()  # must not raise

    from app.main import app
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200


def test_booting_twice_does_not_duplicate(db):
    """Idempotence is what makes running this on every boot acceptable. Without it, each
    restart would create another credential row for the same key."""
    _legacy_project(db)

    _boot()
    db.expire_all()
    after_first = db.query(Credential).count()
    _boot()
    db.expire_all()

    assert db.query(Credential).count() == after_first, "a second boot duplicated credentials"


def test_a_deployment_with_nothing_to_migrate_is_untouched(db):
    """The control. On an already-migrated deployment this must be a no-op, not a source of
    churn — otherwise "runs on every boot" is a liability rather than a safety net."""
    for cfg in db.query(PlatformConfig).all():
        cfg.providers = {}
        cfg.active_chat_provider = ""
    db.query(Credential).delete()
    db.query(DeploymentConfig).delete()
    db.commit()

    _boot()

    db.expire_all()
    assert db.query(Credential).count() == 0
