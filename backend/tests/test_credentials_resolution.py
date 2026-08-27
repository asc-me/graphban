"""The credential store and the transitional resolution order (PRD-25 S1, GRPH-507).

**The load-bearing test in this file is `test_a_project_holding_a_legacy_key_is_untouched`.**
S1 is advertised as additive — deploying it changes no resolution outcome by construction — and
that claim is false the moment step 0 stops coming first. Every project that has configured a
provider today holds it in `platform_config.providers`; the new pointers are unset for all of
them. Consult the pointers first and every one of those projects silently drops to a deployment
default nobody has set, which is the stub. The whole slice would read as a no-op and be an
outage.

Every other test here passes against a resolver that has that ordering backwards.
"""
from __future__ import annotations

import pytest

from app.models import Credential, DeploymentConfig, Organization, Project
from app.security import secrets
from app.services import platform as platform_svc


@pytest.fixture()
def db(client):
    """Depends on `client` because the app's lifespan seeds on startup — a session opened
    before it has no user to own a project. Same reason every other suite here does it."""
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


def _credential(db, cid="cred_1", *, kind="anthropic", org_id=None, model="claude-x", key="sk-live"):
    if org_id and db.get(Organization, org_id) is None:
        db.add(Organization(id=org_id, name=org_id))
        db.commit()
    c = Credential(id=cid, kind=kind, org_id=org_id, model=model,
                   api_key=secrets.encrypt(key), label=cid)
    db.add(c)
    db.commit()
    return c


def _legacy_blob(db, project_id="p1", provider="ollama", model="llama3.1:8b"):
    """Configure a project the OLD way — the blob S6 eventually migrates."""
    cfg = platform_svc.get_config(db, project_id)
    cfg.active_chat_provider = provider
    cfg.providers = {provider: {"base_url": "http://localhost:11434",
                                "chat_model": model, "api_key": ""}}
    db.commit()
    return cfg


# ---- the one that matters ----------------------------------------------------------------


def test_a_project_holding_a_legacy_key_is_untouched(db, project):
    """THE POINT OF THE SLICE. A project configured the old way must resolve exactly as it did
    before this table existed, even when a deployment default is sitting right there.

    The default is deliberately present and different. Without step 0 the resolver finds an
    unset pointer, falls to that default, and every project on the deployment quietly changes
    provider — an outage shipped as an additive slice.
    """
    _legacy_blob(db, "p1", provider="ollama", model="llama3.1:8b")
    cred = _credential(db, "cred_default", kind="anthropic", model="claude-x")
    db.add(DeploymentConfig(scope="", default_credential_id=cred.id))
    db.commit()

    resolved = platform_svc.resolve_chat(db, "p1")

    assert resolved.source == "legacy", (
        f"a project holding its own legacy key resolved via {resolved.source!r} — step 0 is not "
        "first, so every already-configured project on this deployment just changed provider"
    )
    assert resolved.provider_id == "ollama"
    assert resolved.model == "llama3.1:8b"
    assert resolved.credential_id == "", "a legacy resolution names no credential row"


# ---- the rest of the order ---------------------------------------------------------------


def test_no_config_anywhere_is_the_stub(db, project):
    """A fresh install resolves, rather than raising. `source` says why it is the stub."""
    resolved = platform_svc.resolve_chat(db, "p1")

    assert resolved.provider_id == "stub"
    assert resolved.source == "stub"


def test_the_project_pointer_wins_when_no_legacy_blob_exists(db, project):
    """Step 1. Nothing sets this pointer in S1 — it is written directly here so the branch
    ships exercised rather than waiting for S2 to discover it against live data."""
    cred = _credential(db, "cred_p", model="claude-project")
    project.credential_id = cred.id
    db.commit()

    resolved = platform_svc.resolve_chat(db, "p1")

    assert resolved.source == "project"
    assert resolved.credential_id == "cred_p"
    assert resolved.model == "claude-project"


def test_the_deployment_default_catches_a_project_with_no_pointer(db, project):
    """Step 2."""
    cred = _credential(db, "cred_d", model="claude-default")
    db.add(DeploymentConfig(scope="", default_credential_id=cred.id))
    db.commit()

    resolved = platform_svc.resolve_chat(db, "p1")

    assert resolved.source == "deployment"
    assert resolved.credential_id == "cred_d"


def test_a_project_pointer_beats_the_deployment_default(db, project):
    """The override is the whole reason a project pointer exists."""
    default = _credential(db, "cred_d", model="claude-default")
    own = _credential(db, "cred_p", model="claude-own")
    db.add(DeploymentConfig(scope="", default_credential_id=default.id))
    project.credential_id = own.id
    db.commit()

    resolved = platform_svc.resolve_chat(db, "p1")

    assert resolved.credential_id == "cred_p" and resolved.model == "claude-own"


# ---- dangling: the distinction `source` exists for ----------------------------------------


def test_a_pointer_to_a_row_that_never_existed_is_refused_by_the_database(db, project):
    """Written to assert `dangling`, and it could not: the foreign key rejects the pointer.

    That is worth keeping rather than deleting. It pins WHY the interesting dangling case is
    the scope one below — a pointer at an id that was never a credential cannot be stored at
    all, so the only reachable form of "set but does not resolve" is a row that exists and is
    not readable from here.
    """
    from sqlalchemy.exc import IntegrityError

    project.credential_id = "cred_never_existed"
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_a_dangling_pointer_still_falls_through_to_the_default(db, project):
    """Dangling is a description of the OUTCOME, not a halt. If the default catches it the
    project keeps working, and `source` reports where it actually landed."""
    cred = _credential(db, "cred_d")
    theirs = _credential(db, "cred_theirs", org_id="org_other")
    db.add(DeploymentConfig(scope="", default_credential_id=cred.id))
    project.credential_id = theirs.id
    db.commit()

    resolved = platform_svc.resolve_chat(db, "p1")

    assert resolved.source == "deployment" and resolved.credential_id == "cred_d"


# ---- scope: the hosted-mode hole the PRD did not name -------------------------------------


def test_a_credential_from_another_org_is_not_reachable(db):
    """PRD-25 says these belong to "the deployment", which is right for a self-hosted install
    and a cross-tenant leak on the hosted service. A pointer outlives the row it names, so the
    scope is re-checked at RESOLUTION rather than trusted because a write path once vetted it.
    """
    mine = Project(id="mine", name="Mine", tag="MINE", org_id=None)
    db.add(mine)
    theirs = _credential(db, "cred_theirs", org_id="org_other", model="claude-theirs")
    mine.credential_id = theirs.id
    db.commit()

    resolved = platform_svc.resolve_chat(db, "mine")

    assert resolved.credential_id != "cred_theirs", (
        "a project resolved a credential belonging to a different org"
    )
    assert resolved.source == "dangling"


def test_the_deployment_default_does_not_leak_across_scopes(db):
    """The same check on step 2: a default row is only read for the scope that owns it."""
    mine = Project(id="mine", name="Mine", tag="MINE", org_id=None)
    db.add(mine)
    theirs = _credential(db, "cred_theirs", org_id="org_other")
    db.add(DeploymentConfig(scope="", default_credential_id=theirs.id))
    db.commit()

    resolved = platform_svc.resolve_chat(db, "mine")

    assert resolved.source == "stub", "another org's credential was served as this scope's default"


# ---- the registry read --------------------------------------------------------------------


def test_the_listing_never_returns_the_key(db, project):
    """`key_set` and nothing else, exactly as the legacy blob's `provider_config` does it."""
    _credential(db, "cred_1", key="sk-super-secret")

    rows = platform_svc.list_credentials(db, "")

    assert len(rows) == 1
    assert rows[0]["key_set"] is True
    blob = repr(rows[0])
    assert "sk-super-secret" not in blob and "api_key" not in rows[0]


def test_used_by_is_derived_from_the_pointers(db, project):
    """Not stored. A stored count disagrees with reality the first time a project is removed
    by a path that forgets to decrement it."""
    cred = _credential(db, "cred_1")
    other = Project(id="p2", name="P2", tag="P2")
    db.add(other)
    project.credential_id = cred.id
    other.credential_id = cred.id
    db.commit()

    rows = platform_svc.list_credentials(db, "")

    assert rows[0]["used_by"] == ["p1", "p2"]


def test_the_listing_marks_default_fallback_and_embed(db, project):
    a = _credential(db, "cred_a")
    b = _credential(db, "cred_b")
    c = _credential(db, "cred_c")
    db.add(DeploymentConfig(scope="", default_credential_id=a.id,
                            fallback_credential_id=b.id, embed_credential_id=c.id))
    db.commit()

    rows = {r["id"]: r for r in platform_svc.list_credentials(db, "")}

    assert rows["cred_a"]["is_default"] and not rows["cred_a"]["is_fallback"]
    assert rows["cred_b"]["is_fallback"]
    assert rows["cred_c"]["is_embed"]


def test_the_listing_is_scoped(db, project):
    """A listing that showed every org's credentials would leak their labels and models even
    without the keys."""
    _credential(db, "cred_mine", org_id=None)
    _credential(db, "cred_theirs", org_id="org_other")

    rows = platform_svc.list_credentials(db, "")

    assert [r["id"] for r in rows] == ["cred_mine"]


# ---- the SHAPE of the registry read, not just its output (GRPH-526) ------------------------


def _count_selects(db, fn):
    """The SELECTs one call issues. Same idiom as `test_fleet_holding_phase`, kept local
    because a test module importing another test module breaks whenever either is run alone.

    `expire_all` first: `db.get(DeploymentConfig, ...)` is served from the identity map when
    the row is already loaded, so a second measurement in the same session would emit one
    fewer query than the first for a reason that has nothing to do with the derivation —
    and one query of drift is exactly the size of the signal being measured.
    """
    from sqlalchemy import event

    db.expire_all()
    seen: list[str] = []
    engine = db.get_bind()
    hook = lambda conn, cur, stmt, *a: seen.append(stmt)  # noqa: E731
    event.listen(engine, "before_cursor_execute", hook)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", hook)
    return [s for s in seen if s.lstrip().upper().startswith("SELECT")]


def test_the_listing_costs_the_same_queries_for_thirty_credentials_as_for_three(db, project):
    """GRPH-507 named this property — "the query count does not scale with the number of
    credentials" — and nothing asserted it. Replacing the grouped `used_by` query with one
    lookup per credential produces byte-identical output and 2288 passed.

    The docstring on `list_credentials` already answers "who cares at this size": S2 refuses
    to delete a referenced credential and names every referencing project in the 409, so this
    is the query behind an operator-facing error message. That reasoning was prose, and prose
    does not fail.

    Asserted as two sizes rather than one constant, deliberately. A `<= 3` bound passes the
    N+1 version outright whenever the fixture holds three or fewer credentials — which is what
    a hand-written fixture holds — so the obvious form of this test re-creates the defect it
    guards. Thirty is far enough above three that the two shapes cannot be confused for noise.
    """
    for i in range(3):
        _credential(db, f"cred_{i:03d}")
    three = _count_selects(db, lambda: platform_svc.list_credentials(db, ""))
    assert len(platform_svc.list_credentials(db, "")) == 3, \
        "fixture did not seed three credentials; the comparison below has no small side"

    for i in range(3, 30):
        _credential(db, f"cred_{i:03d}")
    thirty = _count_selects(db, lambda: platform_svc.list_credentials(db, ""))
    assert len(platform_svc.list_credentials(db, "")) == 30, \
        "fixture did not grow to thirty credentials; both measurements are the same call"

    assert three, "no SELECTs were observed at all — the event hook is not attached"
    assert len(thirty) == len(three), (
        f"{len(three)} selects for 3 credentials, {len(thirty)} for 30 — the listing queries "
        f"per credential, and the count grows with the registry"
    )


def test_used_by_stays_correct_at_thirty_credentials(db, project):
    """The control on the test above. A count that does not grow is trivially satisfiable by
    a derivation that stops deriving — dropping the `used_by` query entirely holds the query
    count constant at every size, and would sail through a test that only counts.

    So the shape assertion needs a partner asserting the answer is still right at the size
    where the shape is observable.
    """
    for i in range(30):
        _credential(db, f"cred_{i:03d}")
    other = Project(id="p2", name="P2", tag="P2")
    db.add(other)
    project.credential_id = "cred_007"
    other.credential_id = "cred_007"
    db.commit()

    rows = {r["id"]: r for r in platform_svc.list_credentials(db, "")}

    assert rows["cred_007"]["used_by"] == ["p1", "p2"]
    assert rows["cred_008"]["used_by"] == [], \
        "a credential nothing points at reports users — used_by is not keyed per credential"
