"""Decider credential pointer (PRD-45 S2).

The decider is a third model type beside chat and embed. These tests mirror
`test_credentials_resolution.py` and `test_credential_selection.py` for the third pointer:

- A chat credential cannot be the decider default (422 names the type).
- A decider role pointing at a chat credential is refused.
- An unreachable decider default is still returned (asymmetry preserved from chat).
- `falling_back` lists a project whose decider pointer is unreachable.
- An unproven credential cannot become the decider.
- `resolve_decider` returns the project override, then the scope default, then None.
- `list_credentials` exposes `is_decider`, `serves`, and decider pointers in `used_by`/`falling_back`.
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
    p = Project(id="p1", name="P1", tag="p1")
    db.add(p)
    db.commit()
    return p


def _cred(db, cid, *, kind="anthropic", state="valid", org_id=None, model="claude-x"):
    c = Credential(id=cid, kind=kind, org_id=org_id, model=model, label=cid,
                   api_key=secrets.encrypt("sk-live"), state=state)
    db.add(c)
    db.commit()
    return c


# ---- the type gate -----------------------------------------------------------------------


def test_a_chat_credential_cannot_be_the_decider_default(db, project):
    """A chat provider answers in prose; a decider answers in probabilities over a declared
    answer space. Using a chat credential as the decider would parse JSON from prose, which
    is the defect the decider type exists to prevent."""
    _cred(db, "cred_chat", kind="anthropic")

    with pytest.raises(ValueError, match="does not serve decisions"):
        platform_svc.set_scope_defaults(db, "", decider_credential_id="cred_chat")


def test_a_chat_credential_cannot_be_the_project_decider(db, project):
    """Same gate at the project level. The error names the provider kind so the operator
    knows what went wrong."""
    _cred(db, "cred_chat", kind="openai")

    with pytest.raises(ValueError, match="does not serve decisions"):
        platform_svc.set_project_decider(db, "p1", decider_credential_id="cred_chat")


def test_a_decider_credential_can_be_the_default(db, project):
    """The happy path: a systemone credential is accepted as the decider."""
    _cred(db, "cred_decider", kind="systemone")

    row = platform_svc.set_scope_defaults(db, "", decider_credential_id="cred_decider")

    assert row.decider_credential_id == "cred_decider"


def test_a_typesafe_credential_can_be_the_project_decider(db, project):
    """The second decider kind is also accepted."""
    _cred(db, "cred_ts", kind="typesafe")

    proj = platform_svc.set_project_decider(db, "p1", decider_credential_id="cred_ts")

    assert proj.decider_credential_id == "cred_ts"


# ---- the UNPROVEN gate -------------------------------------------------------------------


def test_an_unproven_credential_cannot_be_the_decider(db, project):
    """Same rule as chat/embed: a credential nobody has proven cannot become a pointer."""
    _cred(db, "cred_new", kind="systemone", state="pending_validation")

    with pytest.raises(ValueError, match="never been validated"):
        platform_svc.set_scope_defaults(db, "", decider_credential_id="cred_new")


def test_an_unproven_credential_cannot_be_the_project_decider(db, project):
    """The project-level gate matches the scope-level gate."""
    _cred(db, "cred_new", kind="systemone", state="pending_validation")

    with pytest.raises(ValueError, match="never been validated"):
        platform_svc.set_project_decider(db, "p1", decider_credential_id="cred_new")


def test_an_unreachable_decider_may_be_chosen(db, project):
    """The asymmetry: `unreachable` was asked and did not answer — a fact about the world,
    not an absence of evidence. An operator pointing at it anyway has said something."""
    _cred(db, "cred_down", kind="systemone", state="unreachable")

    row = platform_svc.set_scope_defaults(db, "", decider_credential_id="cred_down")

    assert row.decider_credential_id == "cred_down"


# ---- resolution --------------------------------------------------------------------------


def test_resolve_decider_returns_the_project_override(db, project):
    """Project override wins over scope default, same ordering as chat."""
    _cred(db, "cred_scope", kind="systemone")
    _cred(db, "cred_proj", kind="systemone")
    platform_svc.set_scope_defaults(db, "", decider_credential_id="cred_scope")
    platform_svc.set_project_decider(db, "p1", decider_credential_id="cred_proj")

    resolved = platform_svc.resolve_decider(db, "p1")

    assert resolved is not None
    assert resolved.credential_id == "cred_proj"
    assert resolved.source == "project"


def test_resolve_decider_falls_back_to_scope_default(db, project):
    """No project override -> scope default."""
    _cred(db, "cred_scope", kind="systemone")
    platform_svc.set_scope_defaults(db, "", decider_credential_id="cred_scope")

    resolved = platform_svc.resolve_decider(db, "p1")

    assert resolved is not None
    assert resolved.credential_id == "cred_scope"
    assert resolved.source == "deployment"


def test_resolve_decider_returns_none_when_nothing_configured(db, project):
    """No decider anywhere -> None, so the caller degrades to similarity/chat judge."""
    resolved = platform_svc.resolve_decider(db, "p1")

    assert resolved is None


def test_resolve_decider_falls_back_when_project_pointer_is_unreachable(db, project):
    """An unreachable project decider is fallen past; the scope default answers."""
    _cred(db, "cred_proj", kind="systemone", state="unreachable")
    _cred(db, "cred_scope", kind="systemone")
    platform_svc.set_project_decider(db, "p1", decider_credential_id="cred_proj")
    platform_svc.set_scope_defaults(db, "", decider_credential_id="cred_scope")

    resolved = platform_svc.resolve_decider(db, "p1")

    assert resolved is not None
    assert resolved.credential_id == "cred_scope"
    assert resolved.source == "deployment"
    assert resolved.fell_back_from == "cred_proj"


# ---- list_credentials surface ------------------------------------------------------------


def test_list_credentials_includes_is_decider(db, project):
    """The operator sees which credential is the decider, same as is_default/is_embed."""
    _cred(db, "cred_decider", kind="systemone")
    platform_svc.set_scope_defaults(db, "", decider_credential_id="cred_decider")

    rows = platform_svc.list_credentials(db, "")
    by_id = {r["id"]: r for r in rows}

    assert by_id["cred_decider"]["is_decider"] is True
    assert by_id["cred_decider"]["serves"] == ["decide"]


def test_list_credentials_shows_serves_for_chat_providers(db, project):
    """Every credential row carries what model types its provider supports."""
    _cred(db, "cred_chat", kind="anthropic")

    rows = platform_svc.list_credentials(db, "")
    by_id = {r["id"]: r for r in rows}

    assert by_id["cred_chat"]["serves"] == ["chat"]


def test_list_credentials_includes_decider_in_used_by(db, project):
    """A credential pointed at as a decider shows the project in used_by."""
    _cred(db, "cred_decider", kind="systemone")
    platform_svc.set_project_decider(db, "p1", decider_credential_id="cred_decider")

    rows = platform_svc.list_credentials(db, "")
    by_id = {r["id"]: r for r in rows}

    assert "p1" in by_id["cred_decider"]["used_by"]


def test_list_credentials_shows_falling_back_for_unreachable_decider(db, project):
    """A project whose decider pointer is unreachable shows in falling_back."""
    _cred(db, "cred_down", kind="systemone", state="unreachable")
    platform_svc.set_project_decider(db, "p1", decider_credential_id="cred_down")

    rows = platform_svc.list_credentials(db, "")
    by_id = {r["id"]: r for r in rows}

    assert "p1" in by_id["cred_down"]["falling_back"]


def test_clearing_the_decider_default(db, project):
    """None clears, same as the other pointers."""
    _cred(db, "cred_decider", kind="systemone")
    platform_svc.set_scope_defaults(db, "", decider_credential_id="cred_decider")

    row = platform_svc.set_scope_defaults(db, "", decider_credential_id=None)

    assert row.decider_credential_id is None


def test_clearing_the_project_decider(db, project):
    """None clears the project override, so it inherits the scope default."""
    _cred(db, "cred_scope", kind="systemone")
    _cred(db, "cred_proj", kind="systemone")
    platform_svc.set_scope_defaults(db, "", decider_credential_id="cred_scope")
    platform_svc.set_project_decider(db, "p1", decider_credential_id="cred_proj")

    proj = platform_svc.set_project_decider(db, "p1", decider_credential_id=None)

    assert proj.decider_credential_id is None
    resolved = platform_svc.resolve_decider(db, "p1")
    assert resolved is not None
    assert resolved.credential_id == "cred_scope"
