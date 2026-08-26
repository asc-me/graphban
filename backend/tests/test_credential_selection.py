"""Choosing a credential, and refusing to strand one (PRD-25 S2a, GRPH-508).

Two claims carry this slice and both are about REFUSALS, which is what makes them easy to ship
broken: a guard that never fires looks identical to a guard that cannot fire.

- **A credential nobody has proven cannot become the default.** An unusable credential that can
  still be chosen is the same defect one layer along.
- **A credential something points at cannot be deleted**, and the refusal names who. "In use"
  that does not say by what leaves the operator opening projects by hand.

`unreachable` is deliberately selectable and `pending_validation` is not. That asymmetry is a
grill decision that POSTDATES this ticket's text, which forbids both — see the module note in
`services/platform.py` (UNPROVEN) for the reasoning.
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


def _cred(db, cid, *, state="valid", org_id=None, kind="anthropic", model="claude-x"):
    c = Credential(id=cid, kind=kind, org_id=org_id, model=model, label=cid,
                   api_key=secrets.encrypt("sk-live"), state=state)
    db.add(c)
    db.commit()
    return c


# ---- the state gate -----------------------------------------------------------------------


def test_an_unproven_credential_cannot_be_made_the_default(db, project):
    """`pending_validation` means nobody has ever established this works. Making it the thing
    every project falls back to would assert something no one has checked."""
    _cred(db, "cred_new", state="pending_validation")

    with pytest.raises(ValueError, match="never been validated"):
        platform_svc.set_scope_defaults(db, "", default_credential_id="cred_new")


def test_an_unproven_credential_cannot_be_the_fallback_either(db, project):
    """The fallback is what catches a primary failure. An unproven one turns one outage into
    two."""
    _cred(db, "cred_new", state="pending_validation")

    with pytest.raises(ValueError, match="never been validated"):
        platform_svc.set_scope_defaults(db, "", fallback_credential_id="cred_new")


def test_an_unreachable_credential_MAY_be_chosen(db, project):
    """The asymmetry, stated as a test because it contradicts this ticket's original text.

    `unreachable` was asked and did not answer — a fact about the world right now, not an
    absence of evidence. An operator pointing at it anyway has said something, and the system
    shows the state rather than overruling the choice. At runtime an unreachable fallback is
    skipped and the primary failure is terminal.
    """
    _cred(db, "cred_down", state="unreachable")

    row = platform_svc.set_scope_defaults(db, "", default_credential_id="cred_down")

    assert row.default_credential_id == "cred_down"


def test_clearing_a_default_is_distinct_from_leaving_it_alone(db, project):
    """`None` clears, an omitted field is untouched. If absence meant clear, setting only the
    fallback would silently drop the default."""
    a, b = _cred(db, "cred_a"), _cred(db, "cred_b")
    platform_svc.set_scope_defaults(db, "", default_credential_id=a.id)

    row = platform_svc.set_scope_defaults(db, "", fallback_credential_id=b.id)
    assert row.default_credential_id == "cred_a", "setting the fallback cleared the default"

    row = platform_svc.set_scope_defaults(db, "", default_credential_id=None)
    assert row.default_credential_id is None


# ---- delete integrity ---------------------------------------------------------------------


def test_deleting_a_credential_a_project_uses_is_refused_and_names_it(db, project):
    cred = _cred(db, "cred_1")
    other = Project(id="p2", name="P2", tag="P2", credential_id=cred.id)
    db.add(other)
    project.credential_id = cred.id
    db.commit()

    with pytest.raises(platform_svc.CredentialInUse) as e:
        platform_svc.delete_credential(db, "cred_1", "")

    assert e.value.projects == ["p1", "p2"]
    assert "p1" in str(e.value) and "p2" in str(e.value), (
        "the refusal did not name the projects — an operator now opens every project by hand"
    )


def test_deleting_the_default_is_refused_and_says_so(db, project):
    """A role is a pointer too. Deleting the default would leave resolution falling to the stub
    with nothing recording that it used to land somewhere."""
    cred = _cred(db, "cred_1")
    platform_svc.set_scope_defaults(db, "", default_credential_id=cred.id)

    with pytest.raises(platform_svc.CredentialInUse) as e:
        platform_svc.delete_credential(db, "cred_1", "")

    assert "the deployment default" in str(e.value)


def test_an_unreferenced_credential_deletes(db, project):
    """The refusal must be about references, not about deletion being hard."""
    _cred(db, "cred_1")

    platform_svc.delete_credential(db, "cred_1", "")

    assert db.get(Credential, "cred_1") is None


def test_deleting_across_scopes_is_a_miss_not_a_delete(db):
    """Another org's credential is not deletable from here, and the failure is `not found`
    rather than `forbidden` — the same reason authz uses 404: existence is not probeable."""
    db.add(Organization(id="org_other", name="other"))
    db.commit()
    _cred(db, "cred_theirs", org_id="org_other")

    with pytest.raises(LookupError):
        platform_svc.delete_credential(db, "cred_theirs", "")
    assert db.get(Credential, "cred_theirs") is not None


# ---- the model override -------------------------------------------------------------------


def test_the_override_reaches_the_adapter_not_just_the_report(db, project):
    """The failure this prevents: the UI shows the cheap model, the bill shows the expensive
    one, and nothing in between disagrees. So the assertion is on what `build_chat` RECEIVED,
    not on the `model` field the resolver reports.
    """
    seen = {}

    cred = _cred(db, "cred_1", model="claude-expensive")
    project.credential_id = cred.id
    project.model_override = "claude-cheap"
    db.commit()

    import app.services.platform as mod
    real = mod.providers.build_chat

    def spy(provider, **kw):
        seen.update(kw)
        return real(provider, **kw)

    mod.providers.build_chat = spy
    try:
        resolved = platform_svc.resolve_chat(db, "p1")
    finally:
        mod.providers.build_chat = real

    assert seen.get("model") == "claude-cheap", (
        f"the adapter was built with {seen.get('model')!r} while the override said "
        "'claude-cheap' — the override is cosmetic"
    )
    assert resolved.model == "claude-cheap"


def test_no_override_uses_the_credentials_own_model(db, project):
    cred = _cred(db, "cred_1", model="claude-expensive")
    project.credential_id = cred.id
    db.commit()

    assert platform_svc.resolve_chat(db, "p1").model == "claude-expensive"


def test_a_project_cannot_point_at_another_scopes_credential(db):
    """Checked on the way IN as well as at resolution. Resolution re-checks because a pointer
    outlives the row it names; this checks because an error at save is one the operator can act
    on, where a silent fallback is one they discover from a model answering in the wrong voice.
    """
    db.add(Organization(id="org_other", name="other"))
    mine = Project(id="mine", name="Mine", tag="MINE")
    db.add(mine)
    db.commit()
    _cred(db, "cred_theirs", org_id="org_other")

    with pytest.raises(LookupError):
        platform_svc.set_project_credential(db, "mine", credential_id="cred_theirs")


# ---- probe -> state, and the 422 that is NOT a state --------------------------------------


def test_a_provider_that_answers_and_lacks_the_model_is_still_refused(db, project, monkeypatch):
    """GRPH-485, unchanged. Retry is for *could not be asked*, never for *asked and told no* —
    a row that retried this would retry forever against a settled answer."""
    monkeypatch.setattr(platform_svc.probe, "known_models",
                        lambda *a, **k: frozenset({"claude-a", "claude-b"}))

    with pytest.raises(ValueError, match="does not have model"):
        platform_svc.create_credential(db, "", kind="anthropic", model="claude-missing")


def test_a_provider_that_cannot_be_asked_is_pending_not_refused(db, project, monkeypatch):
    """Refusing a save because a host was briefly down would break a correct edit for a reason
    that has nothing to do with the edit."""
    monkeypatch.setattr(platform_svc.probe, "known_models", lambda *a, **k: None)

    cred = platform_svc.create_credential(db, "", kind="ollama", model="llama3.1:8b")

    assert cred.state == "pending_validation"


def test_a_provider_that_answers_and_has_the_model_is_valid(db, project, monkeypatch):
    monkeypatch.setattr(platform_svc.probe, "known_models",
                        lambda *a, **k: frozenset({"llama3.1:8b"}))

    cred = platform_svc.create_credential(db, "", kind="ollama", model="llama3.1:8b")

    assert cred.state == "valid"


def test_a_resave_resets_the_retry_budget(db, project, monkeypatch):
    """A resave is new information: the thing that could not be asked may now be answerable.
    A row that stayed `unreachable` after being corrected would report the old failure."""
    cred = _cred(db, "cred_1", state="unreachable")
    cred.validation_attempts = 5
    cred.last_error = "connection refused"
    db.commit()
    monkeypatch.setattr(platform_svc.probe, "known_models",
                        lambda *a, **k: frozenset({"claude-x"}))

    updated = platform_svc.update_credential(db, "cred_1", "", base_url="http://fixed")

    assert updated.state == "valid"
    assert updated.validation_attempts == 0 and updated.last_error == ""


def test_editing_keeps_the_row_id_so_pointers_survive(db, project, monkeypatch):
    """Rotation is an edit. As create-and-repoint it would mean finding every project holding
    the old row — which is the migration this whole PRD exists to stop needing."""
    cred = _cred(db, "cred_1")
    project.credential_id = cred.id
    db.commit()
    monkeypatch.setattr(platform_svc.probe, "known_models",
                        lambda *a, **k: frozenset({"claude-x"}))

    updated = platform_svc.update_credential(db, "cred_1", "", api_key="sk-rotated")

    assert updated.id == "cred_1"
    assert platform_svc.resolve_chat(db, "p1").credential_id == "cred_1"
    assert secrets.decrypt(db.get(Credential, "cred_1").api_key) == "sk-rotated"
