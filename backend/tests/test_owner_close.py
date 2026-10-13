"""Owner-close: a signed-in project owner can close hand-merged work (GRPH-1030).

Not `fleet.sign_off`. The completion gate (GRPH-543) still refuses an agent that PATCHes
`done` without an attestation. Remainder attestation does not close a parent.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.services import items as items_svc
from app.services import links as links_svc

MERGE = "abcdef1234567890aaaabbbbccccddddeeeeffff"
CUT_FROM = "1111222233334444555566667777888899990000"
PR_URL = "https://github.com/asc-me/graphban/pull/933"
CLOSER = {"id": "u1", "handle": "ascme", "name": "Alex Cain"}


@pytest.fixture()
def db(client):
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _item(db, title="hand-merged", **kw):
    return items_svc.create_item(db, project_id="core", title=title, **kw)


def _mint(client, auth, scopes):
    return client.post("/api/api-keys", json={"name": "t", "scopes": scopes,
                                              "project_id": "core"},
                       headers=auth).json()["plaintext"]


def _rpc(client, key, name, arguments):
    return client.post("/api/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }, headers={"X-API-Key": key}).json()


def _error(out):
    result = out.get("result") or {}
    if not result.get("isError"):
        return None
    return (result.get("structuredContent") or {}).get("error") or {}


def _login(client, email):
    r = client.post("/api/auth/login", json={"email": email, "password": "graphban"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _attestation(**over):
    row = {"kind": "attestation", "adapter": items_svc.OWNER_CLOSE_ADAPTER,
           "commit": MERGE, "predicates": [
               {"name": "on_base", "passed": True, "detail": "x"},
               {"name": "owner_close", "passed": True, "detail": "y"}]}
    row.update(over)
    return row


# ---- the service -----------------------------------------------------------------

def test_owner_close_completes_with_a_distinct_adapter(db):
    it = _item(db, status="review")
    it.built_by = "builder-agent"
    db.commit()

    out = items_svc.owner_close(db, it.id, closer=CLOSER, commit=MERGE, pr_url=PR_URL)

    assert out.status == "done"
    assert out.reviewed_by is None, "owner close stamped a reviewer agent"
    assert out.reviewed_by_capabilities is None
    assert out.claimed_by is None
    assert out.github_url == PR_URL
    [att] = items_svc.valid_attestations(out.evidence, commit=MERGE)
    assert att["adapter"] == "owner.close"
    assert {p["name"] for p in att["predicates"]} == {"on_base", "owner_close"}
    assert "ascme" in att["detail"] or any("ascme" in p["detail"] for p in att["predicates"])
    assert not any(e.get("adapter") == "fleet.sign_off"
                   for e in items_svc.attestation_receipts(out.evidence))


def test_owner_close_does_not_impersonate_sign_off(db):
    """A reader grepping for fleet.sign_off on a closed item must not find one."""
    it = _item(db, status="blocked")
    out = items_svc.owner_close(db, it.id, closer=CLOSER, commit=MERGE)

    assert items_svc.has_valid_attestation(out.evidence, commit=MERGE)
    assert all(a.get("adapter") != "fleet.sign_off"
               for a in items_svc.attestation_receipts(out.evidence))


def test_owner_close_refuses_the_cut_from_commit(db):
    it = _item(db, status="review")
    it.evidence = items_svc.append_evidence(it.evidence, [{
        "kind": "note", "detail": f"{items_svc.CUT_FROM_MARKER}{CUT_FROM} (`gb/x`)"}])
    db.commit()

    with pytest.raises(ValueError, match="cut from"):
        items_svc.owner_close(db, it.id, closer=CLOSER, commit=CUT_FROM)
    db.refresh(it)
    assert it.status != "done"


def test_owner_close_refuses_a_missing_or_short_commit(db):
    it = _item(db)
    with pytest.raises(ValueError, match="merge commit"):
        items_svc.owner_close(db, it.id, closer=CLOSER, commit="")
    with pytest.raises(ValueError, match="merge commit"):
        items_svc.owner_close(db, it.id, closer=CLOSER, commit="abc")
    db.refresh(it)
    assert it.status != "done"


def test_owner_close_refuses_a_non_pr_url(db):
    it = _item(db)
    with pytest.raises(ValueError, match="not a pull"):
        items_svc.owner_close(db, it.id, closer=CLOSER, commit=MERGE,
                              pr_url="https://example.com/not-a-pr")


def test_owner_close_refuses_already_done(db):
    it = _item(db)
    it.status = "done"
    db.commit()
    with pytest.raises(ValueError, match="already done"):
        items_svc.owner_close(db, it.id, closer=CLOSER, commit=MERGE)


def test_a_remainder_close_does_not_close_its_parent(db):
    parent = _item(db, title="parent still open")
    parent.status = "review"
    remainder = _item(db, title="remainder that landed")
    remainder.status = "review"
    db.commit()
    links_svc.create_link(db, a=remainder.id, b=parent.id, type_="dependency",
                          project_id="core")

    items_svc.owner_close(db, remainder.id, closer=CLOSER, commit=MERGE)
    db.refresh(parent)
    db.refresh(remainder)

    assert remainder.status == "done"
    assert parent.status == "review", "closing a remainder silently closed its parent"


def test_owner_close_skips_pr_cooldown(db, monkeypatch):
    """THE CALL for the cooldown skip. Linking a PR and closing as merged in one
    owner act is the case the skip exists for — CI already ran, the owner merged."""
    monkeypatch.setattr(settings, "pr_cooldown_seconds", 60, raising=False)
    it = _item(db, status="review")
    out = items_svc.owner_close(db, it.id, closer=CLOSER, commit=MERGE, pr_url=PR_URL)
    assert out.status == "done"


def test_agent_update_item_done_is_still_refused(db):
    """GRPH-543 holds. Owner-close is not a weakening of the gate."""
    it = _item(db)
    with pytest.raises(items_svc.MissingAttestation):
        items_svc.update_item(db, it.id, status="done")
    db.refresh(it)
    assert it.status != "done"


# ---- REST -------------------------------------------------------------------

def test_rest_owner_close_as_project_owner(client, auth):
    item = client.post("/api/items", json={"title": "merged on main", "status": "review"},
                       headers=auth).json()
    r = client.post(f"/api/items/{item['id']}/owner-close",
                    json={"commit": MERGE, "pr_url": PR_URL}, headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "done"
    atts = [e for e in body["evidence"] if e.get("kind") == "attestation"]
    assert atts and atts[-1]["adapter"] == "owner.close"
    assert body.get("reviewed_by") in (None, "") or "reviewed_by" not in body


def test_rest_admin_with_write_cannot_owner_close(client, auth):
    item = client.post("/api/items", json={"title": "not yours to close"},
                       headers=auth).json()
    dana = _login(client, "dana@ascme-labs.com")
    r = client.post(f"/api/items/{item['id']}/owner-close",
                    json={"commit": MERGE}, headers=dana)
    assert r.status_code == 403, r.text
    got = client.get(f"/api/items/{item['id']}", headers=auth).json()
    assert got["status"] != "done"


def test_rest_owner_close_needs_a_jwt(client, auth):
    item = client.post("/api/items", json={"title": "no jwt"}, headers=auth).json()
    r = client.post(f"/api/items/{item['id']}/owner-close", json={"commit": MERGE})
    assert r.status_code == 401


def test_rest_owner_close_rejects_an_api_key(client, auth):
    """An agent key is not a signed-in owner, even with gate+write."""
    item = client.post("/api/items", json={"title": "key cannot close"}, headers=auth).json()
    key = _mint(client, auth, ["read", "write", "gate"])
    r = client.post(f"/api/items/{item['id']}/owner-close",
                    json={"commit": MERGE}, headers={"X-API-Key": key})
    assert r.status_code == 401, r.text
    r2 = client.post(f"/api/items/{item['id']}/owner-close",
                     json={"commit": MERGE},
                     headers={"Authorization": f"Bearer {key}"})
    assert r2.status_code == 401, r2.text


def test_rest_patch_done_without_attestation_still_409s(client, auth):
    item = client.post("/api/items", json={"title": "ui done"}, headers=auth).json()
    r = client.patch(f"/api/items/{item['id']}", json={"status": "done"}, headers=auth)
    assert r.status_code == 409, r.text
    assert "attestation" in r.text


def test_rest_patch_cannot_mint_owner_close(client, auth):
    """THE CALL. JWT PATCH used to be able to attach any attestation. This adapter
    has its own route so the owner check cannot be skipped."""
    item = client.post("/api/items", json={"title": "forged adapter"}, headers=auth).json()
    r = client.patch(f"/api/items/{item['id']}", json={
        "status": "done", "evidence": [_attestation()],
    }, headers=auth)
    assert r.status_code == 403, r.text
    assert "owner.close" in r.text
    got = client.get(f"/api/items/{item['id']}", headers=auth).json()
    assert got["status"] != "done"


def test_rest_patch_ci_attestation_still_completes(client, auth):
    """The control. Refusing every attestation on PATCH would strand CI."""
    item = client.post("/api/items", json={"title": "ci attested"}, headers=auth).json()
    r = client.patch(f"/api/items/{item['id']}", json={
        "status": "done",
        "evidence": [{"kind": "attestation", "adapter": "ci", "commit": MERGE,
                      "predicates": [{"name": "suite_green", "passed": True, "detail": "ok"}]}],
    }, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "done"


# ---- MCP --------------------------------------------------------------------

def test_mcp_has_no_owner_close_tool(client, auth):
    key = _mint(client, auth, ["read", "write", "gate"])
    tools = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                        headers={"X-API-Key": key}).json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert "owner_close" not in names
    assert "owner-close" not in names


def test_mcp_update_item_done_is_still_conflict(client, auth):
    item = client.post("/api/items", json={"title": "agent done"}, headers=auth).json()
    key = _mint(client, auth, ["read", "write"])
    err = _error(_rpc(client, key, "update_item", {"id": item["id"], "status": "done"}))
    assert err, "an agent completed without an attestation"
    assert err.get("code") == "conflict"
    assert "attestation" in (err.get("message") or "")


def test_mcp_cannot_write_the_owner_close_adapter(client, auth):
    """THE CALL. A gate-scoped key writing adapter=owner.close would impersonate the owner."""
    item = client.post("/api/items", json={"title": "forged owner.close"}, headers=auth).json()
    key = _mint(client, auth, ["read", "write", "gate"])
    err = _error(_rpc(client, key, "update_item", {
        "id": item["id"], "status": "done", "evidence": [_attestation()],
    }))
    assert err, "a gate key minted owner.close and completed"
    assert err.get("code") == "unauthorized"
    assert "owner.close" in (err.get("message") or "")
    got = client.get(f"/api/items/{item['id']}", headers=auth).json()
    assert got["status"] != "done"


def test_mcp_refusal_of_owner_close_is_on_the_call():
    """Sabotage the CALL: the adapter name is read on args, not only inside owner_close.

    `_call_tool` is wrapped in tests, so this reads the file rather than the live
    function — a wrapper's source would make the assertion hold no matter what the
    dispatcher does.
    """
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "app" / "mcp_server.py"
           ).read_text(encoding="utf-8")
    assert "OWNER_CLOSE_ADAPTER" in src
    # Bound to the dispatcher, not a comment in owner_close. Deleting the `if writes and any`
    # block leaves this green unless we pin the refuse next to `_call_tool`.
    assert "def _call_tool" in src
    call = src.split("def _call_tool", 1)[1].split("\ndef ", 1)[0]
    assert "OWNER_CLOSE_ADAPTER" in call, (
        "mcp_server._call_tool no longer names OWNER_CLOSE_ADAPTER — an agent can mint "
        "the owner receipt by sending it as evidence"
    )
    # `if False and any(... OWNER_CLOSE_ADAPTER)` still names the adapter and stays
    # green here unless the live condition is pinned. Sabotage: that swap let a
    # gate key complete (1 failed: test_mcp_cannot_write_the_owner_close_adapter).
    assert "if writes and any(" in call
    assert "owner.close is a signed-in" in call


def test_rest_patch_refusal_is_on_the_call():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "app" / "routers" / "items.py"
           ).read_text(encoding="utf-8")
    assert "OWNER_CLOSE_ADAPTER" in src
    assert "owner-close" in src
