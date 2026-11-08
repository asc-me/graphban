"""GRPH-1426: the `merge.done_predicates` rule — per-project predicates `done` requires."""
from __future__ import annotations

import pytest

from app.config import settings
from app.services import items as items_svc
from app.services import rule_toggles as T
from app.services import rules as R

RULE = T.DONE_PREDICATES_RULE
SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


def _att(*preds, commit=SHA):
    return {"kind": "attestation", "adapter": "github-actions", "commit": commit,
            "predicates": [{"name": n, "passed": p, "detail": ""} for n, p in preds]}


GREEN = _att(("suite_green", True))


def _login(client):
    r = client.post("/api/auth/login", json={"email": "alex@ascme-labs.com", "password": "graphban"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def library(monkeypatch):
    monkeypatch.setattr(T, "LIBRARY", {})
    T.register(T.LibraryRule(
        id=RULE, stage="merge", title="Predicates done requires",
        summary="Attested predicates an item needs before done.",
        enforcement="enforced", enforcer=R.Enforcer("graphban_source", "app/services/items.py"),
        default_on=True, default_params={"predicates": []}))
    return T.LIBRARY


@pytest.fixture()
def db(client):
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def deployment(monkeypatch):
    def _set(value: str):
        monkeypatch.setattr(settings, "required_predicates", value, raising=False)
    return _set


def _put(client, auth, predicates, reason="raise the bar"):
    return client.put(f"/api/rules/toggles/{RULE}", params={"project_id": "core"},
                      json={"params": {"predicates": predicates}, "reason": reason},
                      headers=auth)


def _item(db):
    return items_svc.create_item(db, project_id="core", title="Built it")


def _done(db, item):
    return items_svc.update_item(db, item.id, status="done").status


def test_unset_project_is_gated_as_today_and_follows_the_deployment(db, library, deployment):
    deployment("")
    item = _item(db)
    items_svc.update_item(db, item.id, evidence=[GREEN])
    assert _done(db, item) == "done"
    deployment("conformance")
    item2 = _item(db)
    items_svc.update_item(db, item2.id, evidence=[GREEN])
    with pytest.raises(items_svc.MissingAttestation) as exc:
        _done(db, item2)
    assert "conformance" in str(exc.value)


def test_a_project_adding_scans_clean_is_refused_without_it_and_passes_with_it(
        client, db, library, deployment):
    deployment("")
    auth = _login(client)
    assert _put(client, auth, ["suite_green", "scans_clean"]).status_code == 200
    item = _item(db)
    items_svc.update_item(db, item.id, evidence=[GREEN])
    with pytest.raises(items_svc.MissingAttestation) as exc:
        _done(db, item)
    assert "scans_clean" in str(exc.value)
    items_svc.update_item(db, item.id, evidence=[_att(("scans_clean", True))])
    assert _done(db, item) == "done"


def test_dropping_a_deployment_predicate_or_suite_green_is_refused_422(
        client, library, deployment):
    deployment("conformance")
    auth = _login(client)
    r = _put(client, auth, ["suite_green", "scans_clean"])
    assert r.status_code == 422 and "conformance" in r.text and "required_predicates" in r.text
    r = _put(client, auth, ["conformance", "scans_clean"])
    assert r.status_code == 422 and "suite_green" in r.text


def test_the_read_lists_each_effective_predicate_with_its_source(client, library, deployment):
    deployment("conformance")
    auth = _login(client)
    assert _put(client, auth, ["conformance", "suite_green", "scans_clean"]).status_code == 200
    r = client.get("/api/rules/toggles", params={"project_id": "core"}, headers=auth)
    t = next(t for t in r.json()["toggles"] if t["rule_id"] == RULE)
    src = {p["name"]: p["source"] for p in t["effective_predicates"]}
    assert src == {"conformance": "deployment", "suite_green": "deployment",
                   "scans_clean": "project"}
