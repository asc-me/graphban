"""Decider credential resolution and selection (PRD-45 S2, GRPH-896).

Mirrors `test_credentials_resolution.py` and `test_credential_selection.py` for the third
pointer. The decider is a System One model that returns calibrated probabilities; used for
memory adjudication when configured. No fallback: a decider that does not answer degrades
to similarity.
"""
from __future__ import annotations

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


def _credential(db, cid="cred_1", *, kind="systemone", org_id=None, model="laya", key="sk-live", state="valid"):
    """Create a credential. Default kind is 'systemone' (decider)."""
    if org_id and db.get(Organization, org_id) is None:
        db.add(Organization(id=org_id, name=org_id))
        db.commit()
    c = Credential(id=cid, kind=kind, org_id=org_id, model=model,
                   api_key=secrets.encrypt(key), label=cid, state=state)
    db.add(c)
    db.commit()
    return c


# ---- Resolution order --------------------------------------------------------------------


def test_no_decider_configured_returns_none_source(db, project):
    """A project with no decider pointer and no scope default gets source='none'."""
    resolved = platform_svc.resolve_decider(db, "p1")
    assert resolved.provider_id == ""
    assert resolved.source == "none"
    assert resolved.chat is None


def test_project_pointer_wins(db, project):
    """A project's decider_credential_id beats the scope default."""
    cred_project = _credential(db, "cred_project", kind="systemone")
    cred_default = _credential(db, "cred_default", kind="systemone")
    project.decider_credential_id = cred_project.id
    db.add(DeploymentConfig(scope="", decider_credential_id=cred_default.id))
    db.commit()

    resolved = platform_svc.resolve_decider(db, "p1")
    assert resolved.source == "project"
    assert resolved.credential_id == cred_project.id


def test_scope_default_catches_project_with_no_pointer(db, project):
    """A project with no decider pointer falls back to the scope default."""
    cred_default = _credential(db, "cred_default", kind="systemone")
    db.add(DeploymentConfig(scope="", decider_credential_id=cred_default.id))
    db.commit()

    resolved = platform_svc.resolve_decider(db, "p1")
    assert resolved.source == "deployment"
    assert resolved.credential_id == cred_default.id


def test_dangling_project_pointer_falls_to_default(db, project):
    """A project pointer to an unreachable credential falls to the default (asymmetric with chat)."""
    cred_bad = _credential(db, "cred_bad", kind="systemone", state="unreachable")
    cred_default = _credential(db, "cred_default", kind="systemone")
    project.decider_credential_id = cred_bad.id
    db.add(DeploymentConfig(scope="", decider_credential_id=cred_default.id))
    db.commit()

    resolved = platform_svc.resolve_decider(db, "p1")
    # Unlike chat, an unreachable project decider does NOT fall back — there's nothing below
    # the default but None. But if the project pointer is dangling (does not resolve in scope),
    # it falls to the default.
    assert resolved.source == "deployment"
    assert resolved.credential_id == cred_default.id


def test_unreachable_default_is_still_returned(db, project):
    """An unreachable scope default is still returned (asymmetry preserved from chat)."""
    cred_bad = _credential(db, "cred_bad", kind="systemone", state="unreachable")
    db.add(DeploymentConfig(scope="", decider_credential_id=cred_bad.id))
    db.commit()

    resolved = platform_svc.resolve_decider(db, "p1")
    assert resolved.source == "deployment"
    assert resolved.credential_id == cred_bad.id


def test_cross_scope_credential_is_not_reachable(db, project):
    """A credential from another org is not reachable."""
    cred_theirs = _credential(db, "cred_theirs", kind="systemone", org_id="other-org")
    project.decider_credential_id = cred_theirs.id
    db.commit()

    resolved = platform_svc.resolve_decider(db, "p1")
    # Source is 'dangling' because the pointer is set but does not resolve in this scope
    assert resolved.source == "dangling"
    assert resolved.fell_back_from == cred_theirs.id


# ---- set_project_decider -----------------------------------------------------------------


def test_set_project_decider_accepts_valid_decider(db, project):
    """set_project_decider accepts a valid decider credential."""
    cred = _credential(db, "cred_decider", kind="systemone", state="valid")
    result = platform_svc.set_project_decider(db, "p1", decider_credential_id=cred.id)
    assert result.decider_credential_id == cred.id


def test_set_project_decider_refuses_chat_credential(db, project):
    """A chat credential cannot be the decider (422 names the type)."""
    cred = _credential(db, "cred_chat", kind="anthropic", state="valid")
    with pytest.raises(ValueError, match="does not serve the decider role"):
        platform_svc.set_project_decider(db, "p1", decider_credential_id=cred.id)


def test_set_project_decider_refuses_unproven(db, project):
    """An UNPROVEN credential cannot be the decider."""
    cred = _credential(db, "cred_unproven", kind="systemone", state="pending_validation")
    with pytest.raises(ValueError, match="has never been validated"):
        platform_svc.set_project_decider(db, "p1", decider_credential_id=cred.id)


def test_set_project_decider_clears_with_none(db, project):
    """Passing None clears the decider pointer."""
    cred = _credential(db, "cred_decider", kind="systemone", state="valid")
    project.decider_credential_id = cred.id
    db.commit()

    result = platform_svc.set_project_decider(db, "p1", decider_credential_id=None)
    assert result.decider_credential_id is None


# ---- set_scope_defaults ------------------------------------------------------------------


def test_set_scope_defaults_accepts_decider(db):
    """set_scope_defaults accepts decider_credential_id."""
    cred = _credential(db, "cred_decider", kind="systemone", state="valid")
    result = platform_svc.set_scope_defaults(db, "", decider_credential_id=cred.id)
    assert result.decider_credential_id == cred.id


def test_set_scope_defaults_refuses_chat_credential(db):
    """A chat credential cannot be the scope decider default."""
    cred = _credential(db, "cred_chat", kind="anthropic", state="valid")
    with pytest.raises(ValueError, match="does not serve the decider role"):
        platform_svc.set_scope_defaults(db, "", decider_credential_id=cred.id)


def test_set_scope_defaults_refuses_unproven(db):
    """An UNPROVEN credential cannot be the scope decider default."""
    cred = _credential(db, "cred_unproven", kind="systemone", state="pending_validation")
    with pytest.raises(ValueError, match="has never been validated"):
        platform_svc.set_scope_defaults(db, "", decider_credential_id=cred.id)


def test_set_scope_defaults_clears_decider_with_none(db):
    """Passing None clears the scope decider default."""
    cred = _credential(db, "cred_decider", kind="systemone", state="valid")
    db.add(DeploymentConfig(scope="", decider_credential_id=cred.id))
    db.commit()

    result = platform_svc.set_scope_defaults(db, "", decider_credential_id=None)
    assert result.decider_credential_id is None


# ---- list_credentials --------------------------------------------------------------------


def test_listing_includes_serves_field(db, project):
    """GET /credentials rows carry the serves field."""
    cred_chat = _credential(db, "cred_chat", kind="anthropic")
    cred_decider = _credential(db, "cred_decider", kind="systemone")
    listing = platform_svc.list_credentials(db, "")
    by_id = {c["id"]: c for c in listing}
    assert "chat" in by_id[cred_chat.id]["serves"]
    assert "decide" in by_id[cred_decider.id]["serves"]


def test_listing_marks_is_decider(db, project):
    """GET /credentials rows carry is_decider for the scope default."""
    cred = _credential(db, "cred_decider", kind="systemone")
    db.add(DeploymentConfig(scope="", decider_credential_id=cred.id))
    db.commit()

    listing = platform_svc.list_credentials(db, "")
    by_id = {c["id"]: c for c in listing}
    assert by_id[cred.id]["is_decider"] is True


def test_listing_used_by_includes_decider_pointers(db, project):
    """used_by includes projects pointing via decider_credential_id."""
    cred = _credential(db, "cred_decider", kind="systemone")
    project.decider_credential_id = cred.id
    db.commit()

    listing = platform_svc.list_credentials(db, "")
    by_id = {c["id"]: c for c in listing}
    assert "p1" in by_id[cred.id]["used_by"]


def test_listing_falling_back_includes_decider(db, project):
    """falling_back includes projects whose decider pointer is unreachable."""
    cred = _credential(db, "cred_decider", kind="systemone", state="unreachable")
    project.decider_credential_id = cred.id
    db.commit()

    listing = platform_svc.list_credentials(db, "")
    by_id = {c["id"]: c for c in listing}
    assert "p1" in by_id[cred.id]["falling_back"]
