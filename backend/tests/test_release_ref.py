"""GRPH-1109 / PRD-P57 S2 — the release ref is a project setting on the box, not gitops.

Pinned from the caller's side: the REST PATCH a person edits it through, the roster the
supervisor reads it from, and the gitops surface it must leave alone.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from app.schemas import GitopsPatch, GitopsView

# Literal, not projects_svc.RELEASE_REF_LITERAL: a test coupled to the callee's constant
# stays green when the message changes (GRPH-617 bounce).
LITERAL_MESSAGE = "release_ref stores one literal ref, not a pattern"
LINKED_403_DETAIL = "gitops on a linked instance is owned by the org admin"


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "Releases"}, headers=auth).json()["id"]


def _patch(client, auth, proj, body):
    return client.patch(f"/api/projects/{proj}", json=body, headers=auth)


def _get(client, auth, proj):
    rows = client.get("/api/projects", headers=auth).json()
    return next(p for p in rows if p["id"] == proj)


def _supervisor_status(client, auth, proj, view="lean"):
    key = client.post("/api/api-keys", json={"name": "sup", "project_id": proj,
                                             "scopes": ["read", "write", "gate"]},
                      headers=auth).json()["plaintext"]
    r = client.post("/api/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "fleet_status", "arguments": {"view": view}}},
        headers={"X-API-Key": key})
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    assert not res.get("isError"), res
    return res["structuredContent"]


# ---- set / clear ----------------------------------------------------------------------------


def test_a_project_starts_unset_and_set_then_clear_round_trips(client, auth, proj):
    assert _get(client, auth, proj)["release_ref"] is None

    r = _patch(client, auth, proj, {"release_ref": "main"})
    assert r.status_code == 200, r.text
    assert r.json()["release_ref"] == "main"
    assert _get(client, auth, proj)["release_ref"] == "main"

    r = _patch(client, auth, proj, {"release_ref": None})
    assert r.status_code == 200, r.text
    assert r.json()["release_ref"] is None
    assert _get(client, auth, proj)["release_ref"] is None


def test_omitting_the_key_leaves_it_alone(client, auth, proj):
    assert _patch(client, auth, proj, {"release_ref": "stable"}).status_code == 200
    r = _patch(client, auth, proj, {"description": "unrelated edit"})
    assert r.status_code == 200
    assert r.json()["release_ref"] == "stable"


def test_blank_clears_rather_than_storing_an_empty_ref(client, auth, proj):
    assert _patch(client, auth, proj, {"release_ref": "main"}).status_code == 200
    assert _patch(client, auth, proj, {"release_ref": "   "}).json()["release_ref"] is None


@pytest.mark.parametrize("bad", ["release/*", "release/?", "rel[12]", "release/{version}",
                                 "two words"])
def test_a_pattern_is_refused_and_nothing_is_stored(client, auth, proj, bad):
    assert _patch(client, auth, proj, {"release_ref": "main"}).status_code == 200
    r = _patch(client, auth, proj, {"release_ref": bad})
    assert r.status_code == 422
    assert LITERAL_MESSAGE in r.json()["detail"]
    assert _get(client, auth, proj)["release_ref"] == "main"


def test_a_slashed_literal_is_a_ref_not_a_pattern(client, auth, proj):
    r = _patch(client, auth, proj, {"release_ref": "release/2026.10"})
    assert r.status_code == 200, r.text
    assert r.json()["release_ref"] == "release/2026.10"


# ---- not gitops -----------------------------------------------------------------------------


def test_nothing_infers_the_ref_from_gitops_base_branch(client, auth, proj):
    assert client.patch(f"/api/projects/{proj}/gitops", json={"base_branch": "main"},
                        headers=auth).status_code == 200
    assert _get(client, auth, proj)["release_ref"] is None
    assert _supervisor_status(client, auth, proj)["release_ref"] == {
        "ref": None, "source": "unmeasured"}


def test_a_linked_box_can_still_set_it(client, auth, proj, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "sync_cloud_url", "https://cloud.example")
    monkeypatch.setattr(settings, "sync_api_key", "gb_sk_link")

    g = client.patch(f"/api/projects/{proj}/gitops", json={"base_branch": "x"}, headers=auth)
    assert g.status_code == 403 and g.json() == {"detail": LINKED_403_DETAIL}, \
        "precondition: this box must be linked, or the test proves nothing"

    r = _patch(client, auth, proj, {"release_ref": "main"})
    assert r.status_code == 200, r.text
    assert _get(client, auth, proj)["release_ref"] == "main"


# Captured from the base commit (362341cd) before this slice. A digest rather than a literal
# so the pin is the whole schema — titles, defaults, nesting — not a field list that would
# miss a changed type. If this fails, the gitops wire changed: that is not this slice's job.
_GITOPS_PATCH_SHA = "5ad8d74436a2a088a22f2609c29bbecf546ba8cb16bd58ffe17fc8eef1cfd610"
_GITOPS_VIEW_SHA = "1392fe0a668cfd2b10a8b952eff434dbe49692dbec71a1e4dfbfd16bc1653673"


def _digest(model) -> str:
    return hashlib.sha256(
        json.dumps(model.model_json_schema(), sort_keys=True).encode()).hexdigest()


def test_the_gitops_patch_and_view_schemas_are_unchanged():
    assert "release_ref" not in GitopsPatch.model_fields
    assert "release_ref" not in json.dumps(GitopsView.model_json_schema())
    assert _digest(GitopsPatch) == _GITOPS_PATCH_SHA
    assert _digest(GitopsView) == _GITOPS_VIEW_SHA


def test_org_and_project_gitops_routes_still_use_those_schemas(client):
    spec = client.get("/openapi.json").json()
    for path in ("/api/projects/{project_id}/gitops", "/api/orgs/{org_id}/gitops"):
        patch = spec["paths"][path]["patch"]
        body = patch["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        out = patch["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        assert body.endswith("/GitopsPatch") and out.endswith("/GitopsView"), path


def test_gitops_patch_does_not_write_the_ref(client, auth, proj):
    """Extra keys are ignored by GitopsPatch; the point is that the ref is not written."""
    client.patch(f"/api/projects/{proj}/gitops", json={"release_ref": "main"}, headers=auth)
    assert _get(client, auth, proj)["release_ref"] is None


# ---- stack health head is the running API, not the ref --------------------------------------


def test_stack_health_head_is_not_the_release_ref(client, auth, proj, monkeypatch):
    from app.services import scans as scans_svc

    seen = []
    real = scans_svc.component_health

    def spy(db, project_id, *, head_sha):
        seen.append(head_sha)
        return real(db, project_id, head_sha=head_sha)

    monkeypatch.setattr(scans_svc, "component_health", spy)
    assert client.get(f"/api/platform/stack/health?project_id={proj}",
                      headers=auth).status_code == 200
    before = seen[-1]
    assert _patch(client, auth, proj, {"release_ref": "release-branch"}).status_code == 200
    assert client.get(f"/api/platform/stack/health?project_id={proj}",
                      headers=auth).status_code == 200
    assert seen[-1] == before
    assert seen[-1] != "release-branch"


# ---- the supervisor reads it ----------------------------------------------------------------


def test_the_supervisor_credential_reads_the_ref_on_fleet_status(client, auth, proj):
    assert _supervisor_status(client, auth, proj)["release_ref"] == {
        "ref": None, "source": "unmeasured"}
    assert _patch(client, auth, proj, {"release_ref": "main"}).status_code == 200
    assert _supervisor_status(client, auth, proj)["release_ref"] == {
        "ref": "main", "source": "project"}


@pytest.mark.parametrize("view", ["lean", "full", "live"])
def test_every_roster_view_carries_the_ref(client, auth, proj, view):
    assert _patch(client, auth, proj, {"release_ref": "trunk"}).status_code == 200
    assert _supervisor_status(client, auth, proj, view)["release_ref"]["ref"] == "trunk"


def test_unset_never_reads_as_main(client, auth, proj):
    """The sabotage target: treat an unset ref as `main` in the read and this fails."""
    status = _supervisor_status(client, auth, proj)
    assert status["release_ref"]["ref"] is None
    assert status["release_ref"]["source"] == "unmeasured"
    assert _get(client, auth, proj)["release_ref"] is None
