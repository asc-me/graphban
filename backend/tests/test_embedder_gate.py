"""One embedder per deployment, and the dimension gate (PRD-25 S4a, GRPH-510).

**`test_a_probe_that_cannot_be_asked_is_REFUSED` is the load-bearing one, and it is the test
most likely to be "fixed" by someone who knows GRPH-485.** That decision says an unreachable
provider is *unchecked, not invalid*, because refusing a save over a brief blip breaks a correct
edit for a reason unrelated to the edit. This slice deliberately says the opposite for
EMBEDDERS, because the blast radius is different in kind: a chat provider that turns out to be
wrong produces a bad answer somebody reads, while an embedder that turns out to be wrong writes
vectors of one width into a column of another.

Both rules are correct for their own case. The test says so, so that the next person to notice
the inconsistency finds the argument instead of the bug they think they see.

The other easy-to-break claim: **the boot path no longer picks the embedder by project name.**
`lifespan` used to configure it from whichever project sorted first alphabetically, so a rename
could silently change which model produced every vector written afterwards.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.models import CodeNode, Credential, DeploymentConfig, MemoryShard, Project
from app.security import secrets
from app.services import embedder as emb


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _cred(db, cid="cred_e", *, kind="stub", model="m", org_id=None):
    c = Credential(id=cid, kind=kind, model=model, label=cid, org_id=org_id,
                   api_key=secrets.encrypt("sk"), state="valid")
    db.add(c)
    db.commit()
    return c


def _no_vectors(db):
    """Clear every embedding, so "empty deployment" means what it says.

    The suite boots with `SEED_ON_START=true`, so the seeded corpus is already embedded — a
    test that skipped this would be asserting about a deployment that has vectors while calling
    it empty. Worth stating because the consequence is real and not a test artifact: on any
    deployment that already has vectors, adopting a deployment embedder for the FIRST time also
    requires an explicit re-index. Vectors written by the environment's embedder are just as
    much "a different space" as vectors written by a previous credential.
    """
    for model in (MemoryShard, CodeNode):
        for row in db.query(model).filter(model.embedding.isnot(None)).all():
            row.embedding = None
    db.commit()


def _a_vector(db, project_id="core"):
    """One embedded shard, so `vectors_exist` has something true to find."""
    db.add(MemoryShard(id="ms_x", project_id=project_id, text="t",
                       embedding=[0.0] * settings.embed_dim))
    db.commit()


class _Embedder:
    def __init__(self, dim=None, raises=None):
        self._dim, self._raises = dim, raises

    def embed(self, text):
        if self._raises is not None:
            raise self._raises
        return [0.1] * self._dim


def _returns(monkeypatch, dim=None, raises=None):
    monkeypatch.setattr(emb.providers, "build_embedder",
                        lambda *a, **k: _Embedder(dim=dim, raises=raises))


# ---- the one that matters ------------------------------------------------------------------


def test_a_probe_that_cannot_be_asked_is_REFUSED(db, monkeypatch):
    """UNKNOWN IS NOT PERMISSION — and this contradicts GRPH-485 on purpose.

    There, `known_models` returning None means "unchecked, not invalid", and a save proceeds.
    Here the worst case is an unverified embedder writing into a fixed-width column, so a
    provider that did not answer has demonstrated nothing and is refused.

    If you are here because the two rules look inconsistent: they are, deliberately, and the
    module docstring argues it.
    """
    _returns(monkeypatch, raises=ConnectionError("host is down"))
    _cred(db, "cred_e")

    with pytest.raises(emb.EmbedderRefused) as caught:
        emb.set_embed_credential(db, "", "cred_e")

    assert caught.value.reason == "unreachable"
    assert db.get(DeploymentConfig, "") is None, "a refused credential was stored anyway"


def test_an_empty_vector_is_also_unreachable(db, monkeypatch):
    """A provider that answers with nothing has not told us its dimension either. Treating an
    empty list as "dimension 0" would compare 0 against EMBED_DIM and report the wrong reason."""
    _returns(monkeypatch, dim=0)
    _cred(db, "cred_e")

    with pytest.raises(emb.EmbedderRefused) as caught:
        emb.set_embed_credential(db, "", "cred_e")

    assert caught.value.reason == "unreachable"


# ---- the dimension gate ---------------------------------------------------------------------


def test_a_different_dimension_is_refused_and_names_EMBED_DIM(db, monkeypatch):
    """A settings dialog cannot resize a live column — `EmbeddingType(settings.embed_dim)` is
    read at model-definition time. An interface that appeared to offer it would be lying."""
    _returns(monkeypatch, dim=settings.embed_dim + 128)
    _cred(db, "cred_e")

    with pytest.raises(emb.EmbedderRefused) as caught:
        emb.set_embed_credential(db, "", "cred_e")

    assert caught.value.reason == "wrong_dimension"
    assert "EMBED_DIM" in str(caught.value), "the refusal does not name what must change"


def test_the_probe_is_the_check_not_the_model_name(db, monkeypatch):
    """Nothing is inferred from a model name or a lookup table, so there is no "probe passed
    but the model was incompatible" case. A credential whose NAME suggests a familiar model is
    still judged by the vector it returns."""
    _returns(monkeypatch, dim=settings.embed_dim + 1)
    _cred(db, "cred_e", model="bge-m3")  # a model whose real dimension is well known

    with pytest.raises(emb.EmbedderRefused) as caught:
        emb.set_embed_credential(db, "", "cred_e")

    assert caught.value.reason == "wrong_dimension"


def test_a_matching_dimension_is_accepted_on_an_empty_deployment(db, monkeypatch):
    _no_vectors(db)
    _returns(monkeypatch, dim=settings.embed_dim)
    _cred(db, "cred_e")

    row = emb.set_embed_credential(db, "", "cred_e")

    assert row.embed_credential_id == "cred_e"


# ---- vectors already exist ------------------------------------------------------------------


def test_changing_the_embedder_while_vectors_exist_is_refused(db, monkeypatch):
    """Same width is not the same space. Neighbours computed under the old model are
    meaningless under the new one, and search would return them without saying so."""
    _no_vectors(db)
    _returns(monkeypatch, dim=settings.embed_dim)
    _cred(db, "cred_old")
    _cred(db, "cred_new")
    emb.set_embed_credential(db, "", "cred_old")
    _a_vector(db)

    with pytest.raises(emb.EmbedderRefused) as caught:
        emb.set_embed_credential(db, "", "cred_new")

    assert caught.value.reason == "vectors_exist"
    assert db.get(DeploymentConfig, "").embed_credential_id == "cred_old", "it changed anyway"


def test_an_explicit_reindex_is_how_that_becomes_possible(db, monkeypatch):
    """Refused as a side effect of saving a form; allowed as a deliberate action."""
    _no_vectors(db)
    _returns(monkeypatch, dim=settings.embed_dim)
    _cred(db, "cred_old")
    _cred(db, "cred_new")
    emb.set_embed_credential(db, "", "cred_old")
    _a_vector(db)

    row = emb.set_embed_credential(db, "", "cred_new", allow_reindex=True)

    assert row.embed_credential_id == "cred_new"


def test_re_selecting_the_SAME_credential_is_not_a_change(db, monkeypatch):
    """Saving a form without touching the embedder must not demand a re-index."""
    _no_vectors(db)
    _returns(monkeypatch, dim=settings.embed_dim)
    _cred(db, "cred_e")
    emb.set_embed_credential(db, "", "cred_e")
    _a_vector(db)

    row = emb.set_embed_credential(db, "", "cred_e")

    assert row.embed_credential_id == "cred_e"


def test_vectors_are_looked_for_in_BOTH_tables(db, monkeypatch):
    """Checking one would answer "are there vectors" with "are there vectors in the table I
    happened to check" — a deployment with an empty memory and a populated code graph would
    sail past the guard."""
    _no_vectors(db)
    _returns(monkeypatch, dim=settings.embed_dim)
    _cred(db, "cred_old")
    _cred(db, "cred_new")
    emb.set_embed_credential(db, "", "cred_old")
    db.add(CodeNode(id="cn_x", project_id="core", path="a.py", kind="file",
                    embedding=[0.0] * settings.embed_dim))
    db.commit()

    with pytest.raises(emb.EmbedderRefused) as caught:
        emb.set_embed_credential(db, "", "cred_new")

    assert caught.value.reason == "vectors_exist"


# ---- scope and boot ---------------------------------------------------------------------------


def test_another_scopes_credential_cannot_be_selected(db, monkeypatch):
    from app.models import Organization

    _returns(monkeypatch, dim=settings.embed_dim)
    db.add(Organization(id="org_other", name="other"))
    db.commit()
    _cred(db, "cred_theirs", org_id="org_other")

    with pytest.raises(LookupError):
        emb.set_embed_credential(db, "", "cred_theirs")


def test_boot_no_longer_picks_the_embedder_by_project_name(db, monkeypatch):
    """THE WART THIS REMOVES. `lifespan` configured the embedder from whichever project sorted
    first alphabetically, so renaming a project could silently change which model produced
    every vector written afterwards — with nothing recording that it had.

    Two projects are created whose names sort either side of the credential's; the answer must
    not depend on them at all.
    """
    _no_vectors(db)
    _returns(monkeypatch, dim=settings.embed_dim)
    db.add(Project(id="aaa", name="AAA", tag="AAA"))
    db.add(Project(id="zzz", name="ZZZ", tag="ZZZ"))
    db.commit()
    _cred(db, "cred_e")
    emb.set_embed_credential(db, "", "cred_e")

    assert emb.apply_embedder(db) == "credential:cred_e"


def test_with_no_credential_configured_boot_keeps_the_environment(db):
    """An offline or freshly-installed deployment must behave exactly as it does today."""
    assert emb.apply_embedder(db) == "environment"


def test_a_dangling_embed_pointer_falls_back_rather_than_failing_boot(db):
    """A pointer at a credential that is not readable must not take startup down — the whole
    deployment would fail to boot over a configuration row."""
    db.add(DeploymentConfig(scope="", embed_credential_id=None))
    db.commit()
    row = db.get(DeploymentConfig, "")
    row.embed_credential_id = None
    db.commit()

    assert emb.apply_embedder(db) == "environment"


# ---- the CALL: PUT /credentials/defaults must hit the gate -------------------------------


def test_the_defaults_endpoint_refuses_an_embedder_while_vectors_exist(
    client, auth, db, monkeypatch,
):
    """THE BOUNCE. `set_embed_credential` refused while vectors exist; PUT defaults
    wrote `embed_credential_id` through `set_scope_defaults` and returned 200.

    Seeded tests already have vectors, so this is the real deployment case, not a
    fixture we had to invent.
    """
    _returns(monkeypatch, dim=settings.embed_dim)
    _cred(db, "cred_e")

    r = client.put(
        "/api/platform/credentials/defaults",
        json={"embed_credential_id": "cred_e"},
        headers=auth,
    )

    assert r.status_code == 409, r.text
    assert "vector" in r.json()["detail"].lower()
    row = db.get(DeploymentConfig, "")
    assert row is None or row.embed_credential_id != "cred_e"


def test_set_scope_defaults_routes_embed_through_the_gate(db, monkeypatch):
    """Same hole at the service layer the router calls. setattr on embed_credential_id
    is the mutation that must fail this.
    """
    from app.services import platform as platform_svc

    _returns(monkeypatch, dim=settings.embed_dim)
    _cred(db, "cred_e")
    _a_vector(db)

    with pytest.raises(emb.EmbedderRefused, match="vectors"):
        platform_svc.set_scope_defaults(db, "", embed_credential_id="cred_e")


def test_a_legal_embedder_change_is_live_without_restart(db, monkeypatch):
    """Saving a form that is allowed must not wait for process restart to take effect.
    get_embedder() is cached from boot; apply_embedder is what clears it.
    """
    from app import providers

    _no_vectors(db)
    _returns(monkeypatch, dim=settings.embed_dim)
    _cred(db, "cred_e", model="live-now")
    emb.set_embed_credential(db, "", "cred_e")

    assert providers._active_embed.get("model") == "live-now", (
        "the row was saved but get_embedder() is still the boot embedder"
    )
