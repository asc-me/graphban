"""Every existing key becomes a row, and step 0 dies with it (PRD-25 S6, GRPH-512).

The migration's whole claim is **dedupe on content**, and the two ways to get it wrong are
opposite: merging two credentials that only look alike, or splitting one that is genuinely
shared. Both produce a plausible-looking result, so both need their own test.

**`test_step_0_is_gone` is the one that makes this slice real.** Everything else could ship
with resolution still reading the blob, and nothing would fail — the migrated pointers would
simply never be consulted, and the deployment would keep running on configuration the whole PRD
was built to replace.

The grill amended two things this file pins against the ticket's original text: the legacy blob
is NOT deleted here, and a malformed entry is skipped rather than aborting the run.
"""
from __future__ import annotations

import pytest

from app.models import Credential, DeploymentConfig, PlatformConfig, Project
from app.security import secrets
from app.services import credential_migration as mig
from app.services import platform as platform_svc


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def _clean(db):
    """The seed ships projects with their own provider blobs; this suite states its own."""
    for cfg in db.query(PlatformConfig).all():
        cfg.providers = {}
        cfg.active_chat_provider = ""
    db.query(Credential).delete()
    db.query(DeploymentConfig).delete()
    for p in db.query(Project).all():
        p.credential_id = None
        p.model_override = ""
    db.commit()


def _project(db, pid, providers=None):
    """A project configured the OLD way — blob AND `active_chat_provider`.

    **Setting `active_chat_provider` is what makes these fixtures real.** `_chat_params` reads
    it first and falls through to `llm_mode` when it is empty, so a blob without it never
    resolved via step 0 in the first place. The initial version of this file omitted it, and
    `test_step_0_is_gone` consequently passed with step 0 fully restored — it was asserting a
    difference that could not exist. Found by that sabotage surviving.
    """
    if db.get(Project, pid) is None:
        db.add(Project(id=pid, name=pid.upper(), tag=pid.upper()[:6]))
        db.commit()
    cfg = platform_svc.get_config(db, pid)
    cfg.providers = providers or {}
    cfg.active_chat_provider = next(iter(providers), "") if providers else ""
    db.commit()
    return pid


def _blob(kind="anthropic", *, url="", key="sk-live", model="claude-x"):
    return {kind: {"base_url": url, "api_key": secrets.encrypt(key) if key else "",
                   "chat_model": model}}


# ---- the one that makes the slice real -----------------------------------------------------


def test_step_0_is_gone(db):
    """THE POINT. With a legacy blob present and NO credential pointer, resolution must not
    fall back to the blob.

    Every other test here passes with step 0 still in place — the migration would create rows
    nothing ever consults, and the deployment would keep running on the configuration this PRD
    exists to replace.
    """
    _project(db, "p1", _blob("ollama", url="http://legacy:11434", model="llama3.1:8b"))

    resolved = platform_svc.resolve_chat(db, "p1")

    assert resolved.source != "legacy", "resolution still reads the legacy blob"
    assert resolved.provider_id == "stub", (
        f"got {resolved.provider_id!r} from somewhere — the blob is still being consulted"
    )


# ---- dedupe on content ---------------------------------------------------------------------


def test_two_projects_with_the_same_key_collapse_to_one_credential(db):
    _project(db, "p1", _blob(key="sk-shared"))
    _project(db, "p2", _blob(key="sk-shared"))

    report = mig.migrate(db)

    assert report["credentials_created"] == 1
    assert db.get(Project, "p1").credential_id == db.get(Project, "p2").credential_id


def test_two_projects_with_DIFFERENT_keys_stay_two_credentials(db):
    """The opposite error, and just as plausible-looking: merging two credentials that only
    share a provider kind would hand one project another project's key."""
    _project(db, "p1", _blob(key="sk-one"))
    _project(db, "p2", _blob(key="sk-two"))

    report = mig.migrate(db)

    assert report["credentials_created"] == 2
    assert db.get(Project, "p1").credential_id != db.get(Project, "p2").credential_id


def test_endpoints_that_differ_only_cosmetically_are_the_same_credential(db):
    """Trailing slash, default port, host case. Three spellings of one endpoint must not
    become three credentials each holding the same key."""
    _project(db, "p1", _blob("ollama", url="https://Host.Example:443/v1/", key="k"))
    _project(db, "p2", _blob("ollama", url="https://host.example/v1", key="k"))

    assert mig.migrate(db)["credentials_created"] == 1


def test_paths_are_case_sensitive_and_do_not_merge(db):
    """Path case is preserved on purpose — folding it would merge two genuinely different
    endpoints into one credential."""
    _project(db, "p1", _blob("ollama", url="https://h/v1/Alpha", key="k"))
    _project(db, "p2", _blob("ollama", url="https://h/v1/alpha", key="k"))

    assert mig.migrate(db)["credentials_created"] == 2


# ---- models are not a conflict --------------------------------------------------------------


def test_a_shared_key_with_different_models_becomes_one_credential_and_an_override(db):
    _project(db, "p1", _blob(key="sk-shared", model="claude-big"))
    _project(db, "p2", _blob(key="sk-shared", model="claude-big"))
    _project(db, "p3", _blob(key="sk-shared", model="claude-cheap"))

    report = mig.migrate(db)

    assert report["credentials_created"] == 1
    cred = db.query(Credential).one()
    # The most common model becomes the credential's own, so the FEWEST overrides are created.
    assert cred.model == "claude-big"
    assert db.get(Project, "p3").model_override == "claude-cheap"
    assert db.get(Project, "p1").model_override == ""
    assert report["overrides"] == 1


def test_the_model_tie_break_is_deterministic():
    """"Most common" alone is not a function. With two models at equal frequency the output
    would depend on row order, and a migration that is not reproducible cannot be reasoned
    about or re-run."""
    assert mig.choose_model(["b", "a"]) == "a"
    assert mig.choose_model(["a", "b"]) == "a"
    assert mig.choose_model(["b", "b", "a"]) == "b"


# ---- what the grill amended -----------------------------------------------------------------


def test_the_legacy_blob_survives_the_migration(db):
    """AMENDED FROM THE TICKET, which said blob deletion commits in the same transaction.
    Deleting the only copy of the old configuration in the same breath as writing the new one
    is what makes a bad migration unrecoverable. Nothing reads it after S6; a later migration
    removes it."""
    _project(db, "p1", _blob(key="sk-live"))

    mig.migrate(db)

    assert platform_svc.get_config(db, "p1").providers != {}, (
        "the legacy blob was deleted — there is now no copy of the old configuration"
    )


def test_a_malformed_entry_is_skipped_and_reported_not_aborted(db):
    """One bad blob must not block the upgrade, leaving the operator with no working
    deployment and no way in to fix the row that caused it."""
    _project(db, "p1", {"anthropic": "not-a-dict"})
    _project(db, "p2", _blob(key="sk-good"))

    report = mig.migrate(db)

    assert report["credentials_created"] == 1, "a good entry was lost with the bad one"
    assert any(s.get("why") for s in report["skipped"]), "the skip was not reported"
    assert db.get(Project, "p2").credential_id is not None


# ---- state, labels, default -----------------------------------------------------------------


def test_migrated_credentials_start_pending_not_valid(db):
    """A key that worked when it was saved may not work now, and this migration has no
    evidence either way."""
    _project(db, "p1", _blob(key="sk-live"))

    mig.migrate(db)

    assert db.query(Credential).one().state == "pending_validation"


def test_labels_disambiguate_only_when_they_have_to(db):
    """Same-kind-different-key is the only case needing one, since identical entries have
    already collapsed — so the common case keeps a clean name."""
    _project(db, "p1", _blob(key="sk-one"))
    _project(db, "p2", _blob(key="sk-two"))

    mig.migrate(db)

    labels = sorted(c.label for c in db.query(Credential).all())
    assert labels == ["anthropic", "anthropic (2)"]


def test_the_default_is_the_most_referenced_and_says_why(db):
    """A heuristic that picks wrong on some deployments — so it is reported with the count
    that decided it, rather than chosen silently."""
    _project(db, "p1", _blob(key="sk-shared"))
    _project(db, "p2", _blob(key="sk-shared"))
    _project(db, "p3", _blob(key="sk-lonely"))

    report = mig.migrate(db)

    shared = db.get(Project, "p1").credential_id
    assert report["default_credential_id"] == shared
    assert "2" in report["default_chosen_because"]
    assert db.get(DeploymentConfig, "").default_credential_id == shared


def test_running_it_twice_does_not_duplicate(db):
    """Idempotent by construction: a project that already has a pointer is not migrated again,
    so a re-run after a partial failure reconciles rather than doubling."""
    _project(db, "p1", _blob(key="sk-live"))

    first = mig.migrate(db)
    second = mig.migrate(db)

    assert first["credentials_created"] == 1
    assert second["credentials_created"] == 0
    assert db.query(Credential).count() == 1


def test_a_blank_entry_is_not_a_credential(db):
    """The old table let you save a row with neither key nor endpoint. That was never usable
    and must not become a credential."""
    _project(db, "p1", {"anthropic": {"base_url": "", "api_key": "", "chat_model": "x"}})

    assert mig.migrate(db)["credentials_created"] == 0


# ---- the migrated deployment actually resolves ----------------------------------------------


def test_after_migrating_a_project_resolves_through_its_new_pointer(db):
    """The end-to-end claim: configuration that used to resolve via step 0 still resolves,
    now through the pointer. Without this the migration could create perfect rows that
    resolution ignores."""
    _project(db, "p1", _blob(key="sk-live", model="claude-x"))

    mig.migrate(db)
    resolved = platform_svc.resolve_chat(db, "p1")

    assert resolved.source == "project"
    assert resolved.credential_id == db.get(Project, "p1").credential_id
    assert resolved.model == "claude-x"


# ---- a blob with more than one entry (GRPH-539) ---------------------------------------------


def test_the_pointer_follows_active_chat_provider_not_iteration_order(db):
    """FOUND IN PRODUCTION, not by a test. This is the case none of the fifteen above covered.

    A project's blob can hold several configured providers while `active_chat_provider` names
    the one in use. The first version assigned `credential_id` for every group a project
    appeared in, so it was written once per entry and whichever came LAST won.

    On the reference deployment that pointed an ollama-configured project at an xai credential
    — with an ollama model override — so its calls asked x.ai for a qwen model. Nothing failed
    at migration time; it failed at the first chat request.
    """
    _project(db, "p1", {
        # Deliberately ordered so the WRONG answer is the last one written.
        "ollama": {"base_url": "http://ollama:11434", "api_key": "", "chat_model": "qwen"},
        "xai": {"base_url": "", "api_key": secrets.encrypt("sk-xai"), "chat_model": "grok"},
    })
    cfg = platform_svc.get_config(db, "p1")
    cfg.active_chat_provider = "ollama"
    db.commit()

    mig.migrate(db)

    pointed = db.get(Credential, db.get(Project, "p1").credential_id)
    assert pointed.kind == "ollama", (
        f"the project was using ollama and now points at a {pointed.kind} credential"
    )


def test_an_inactive_entry_still_becomes_a_credential(db):
    """It is a real key somebody saved — it simply is not what this project was using. Losing
    it would mean the migration discarded configuration, which is the one thing it must not do.
    """
    _project(db, "p1", {
        "ollama": {"base_url": "http://ollama:11434", "api_key": "", "chat_model": "qwen"},
        "xai": {"base_url": "", "api_key": secrets.encrypt("sk-xai"), "chat_model": "grok"},
    })
    cfg = platform_svc.get_config(db, "p1")
    cfg.active_chat_provider = "ollama"
    db.commit()

    mig.migrate(db)

    kinds = sorted(c.kind for c in db.query(Credential).all())
    assert kinds == ["ollama", "xai"], f"an entry was discarded: {kinds}"


def test_an_inactive_entry_does_not_create_a_spurious_override(db):
    """The credential's model is chosen from the projects USING it. An inactive entry's model
    says what a project would use if it switched, and letting it win would create an override
    for every project that is actually using the thing."""
    _project(db, "p1", {
        "ollama": {"base_url": "http://o:11434", "api_key": "", "chat_model": "qwen"},
    })
    _project(db, "p2", {
        "ollama": {"base_url": "http://o:11434", "api_key": "", "chat_model": "qwen"},
        "xai": {"base_url": "", "api_key": secrets.encrypt("k"), "chat_model": "grok"},
    })
    for pid in ("p1", "p2"):
        platform_svc.get_config(db, pid).active_chat_provider = "ollama"
    db.commit()

    mig.migrate(db)

    assert db.get(Project, "p1").model_override == ""
    assert db.get(Project, "p2").model_override == ""
