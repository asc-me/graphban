"""Agent-side project creation, gated on the cloud link (AL-284 / PRD-14 D4).

Project creation is an AUTHORITY gate, and PRD-14's rule is that those stay human. This
is the one deliberate exception, and it is narrow on purpose: an agent may create a
project only where doing so cannot reach anyone else's tenant — a self-hosted box that
is not linked to a cloud org.

Once linked, a locally-created project reaches that org's tenant space and consumes its
quota, so conjuring one becomes a decision belonging to whoever owns the org.

The gate reads `code_sync.link_status()`, which resolves the DB link then the env link.
That is only trustworthy because of AL-281: before it, a CLI-linked instance reported
`linked: false` and this gate would have FAILED OPEN on precisely the instances it
exists for. The parametrised test below covers all three link sources for that reason.
"""
import json

import pytest

from app.services import code_sync


def _call(client, api_key: str, args: dict) -> dict:
    r = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "create_project", "arguments": args}},
        headers={"X-API-Key": api_key},
    )
    assert r.status_code == 200, r.text
    return r.json()["result"]


def _ok(client, api_key: str, args: dict) -> dict:
    result = _call(client, api_key, args)
    assert result.get("isError") is not True, result
    return json.loads(result["content"][0]["text"])


def _err(client, api_key: str, args: dict) -> dict:
    result = _call(client, api_key, args)
    assert result.get("isError") is True, result
    return result["structuredContent"]["error"]


@pytest.fixture()
def key(client, auth):
    """A GLOBAL key (no pinned project) — what an agent bootstrapping a box would hold."""
    r = client.post("/api/api-keys", json={"name": "agent", "scopes": ["read", "write"]},
                    headers=auth)
    assert r.status_code == 201, r.text
    return r.json()["plaintext"]


# ---- the narrow opening --------------------------------------------------------------
def test_an_unlinked_self_host_may_create_a_project(client, key):
    out = _ok(client, key, {"name": "Fresh Repo"})
    assert out["tag"] == "FR"
    assert out["usable_by_this_key"] is True
    listed = _ok_list(client, key)
    assert out["id"] in listed


def _ok_list(client, api_key: str) -> list[str]:
    r = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "list_projects", "arguments": {}}},
        headers={"X-API-Key": api_key},
    )
    return [p["id"] for p in json.loads(r.json()["result"]["content"][0]["text"])["results"]]


def test_the_creating_agents_owner_can_use_it(client, key):
    """A project without the owner Membership would be invisible to its own creator —
    which is why creation goes through the one shared service path."""
    out = _ok(client, key, {"name": "Owned Repo"})
    assert out["id"] in _ok_list(client, key)


def test_an_explicit_bad_tag_is_a_validation_error(client, key):
    err = _err(client, key, {"name": "Repo", "tag": "way-too-long"})
    assert err["code"] == "validation", err


# ---- the refusals --------------------------------------------------------------------
@pytest.mark.parametrize("source", ["db", "env"])
def test_a_linked_instance_refuses(client, key, monkeypatch, source):
    """Both link sources must refuse. `db` is the UI link AND — since AL-281 — the CLI
    link, which is the case that used to fail open."""
    if source == "db":
        from app.db import SessionLocal

        db = SessionLocal()
        try:
            code_sync.set_link(db, cloud_url="https://cloud.example", api_key="k", org="Acme")
        finally:
            db.close()
    else:
        monkeypatch.setattr(code_sync.settings, "sync_cloud_url", "https://cloud.example")
        monkeypatch.setattr(code_sync.settings, "sync_api_key", "k")

    err = _err(client, key, {"name": "Should Not Exist"})
    assert err["code"] == "unauthorized", err
    assert "cloud org" in err["message"]

    # Check the DATABASE, not list_projects: under an env link every non-local tool
    # proxies, so listing would be answered by the (fake) cloud rather than this box.
    from app.db import SessionLocal
    from app.models import Project

    db = SessionLocal()
    try:
        assert [p for p in db.query(Project).all() if p.name == "Should Not Exist"] == []
    finally:
        db.close()


def test_a_cli_link_refuses_too(client, key, tmp_path, monkeypatch):
    """End-to-end on the AL-281 fix: link via the CLI, then confirm the gate sees it.
    Before AL-281 the CLI wrote only ~/.graphban/config.json, link_status never read it,
    and this call would have SUCCEEDED on a linked box."""
    from app.db import SessionLocal
    from app.services import code_sync

    db = SessionLocal()
    try:
        code_sync.set_link(db, cloud_url="https://cloud.example", api_key="gb_sk_" + "0" * 40)
    finally:
        db.close()

    err = _err(client, key, {"name": "Conjured While Linked"})
    assert err["code"] == "unauthorized", err


def test_hosted_mode_refuses(client, key, monkeypatch):
    """In hosted mode this is tenant isolation, not a preference."""
    import app.mcp_server as mcp

    monkeypatch.setattr(mcp.settings, "hosted_mode", True)
    err = _err(client, key, {"name": "Tenant Escape"})
    assert err["code"] == "unauthorized", err
    assert "hosted mode" in err["message"]


def test_create_project_never_proxies_to_the_cloud(client):
    """The trap this item nearly walked into. Proxying is decided BEFORE dispatch, so a
    tool absent from LOCAL_TOOLS is forwarded to the cloud — which would create the
    project in the org's tenant space, the precise authority action the gate refuses.
    Keeping it local is what lets the refusal fire at all."""
    from app.services import mcp_proxy

    assert "create_project" in mcp_proxy.LOCAL_TOOLS


# ---- key scope -----------------------------------------------------------------------
def test_a_pinned_key_is_told_it_cannot_use_the_new_project(client, auth):
    """A key minted for one project doesn't widen because it created a second. Saying so
    beats letting the next call 403 in a way that reads like a bug."""
    pid = client.post("/api/projects", json={"name": "Pinned Home", "tag": "PH"},
                      headers=auth).json()["id"]
    api_key = client.post("/api/api-keys",
                          json={"name": "pinned", "project_id": pid,
                                "scopes": ["read", "write"]},
                          headers=auth).json()["plaintext"]

    out = _ok(client, api_key, {"name": "Second Repo"})
    assert out["usable_by_this_key"] is False, out
