"""P23 A / GRPH-654: create=true files tasks only after the grill earns approved.

Dry-run still proposes on a draft. Filing an ungrilled body is the write a running
fleet will pick up, so the refusal has to be at the service — REST, MCP, and the
in-app assistant all call decompose().
"""
import pytest

from app.db import SessionLocal
from app.providers.toolcall import ToolCall
from app.services import assistant_tools as at
from app.services import items as items_svc
from app.services import prds as prd_svc
from tests.prd_approve import approve, approve_id

BODY = "# Spec\n\n## Ingest\nread the feed\n\n## Transform\nnormalize\n"


def _prd(client, auth, body=BODY, title="Spec"):
    return client.post("/api/prds", json={"title": title, "body": body, "project_id": "core"},
                       headers=auth).json()


def _mcp(client, key, name, args):
    r = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": name, "arguments": args}}, headers={"X-API-Key": key})
    return r.json()["result"]


def _key(client, auth):
    return client.post("/api/api-keys", json={"name": "spec-agent", "project_id": "core"},
                       headers=auth).json()["plaintext"]


def test_dry_run_on_a_draft_still_proposes(client, auth):
    prd = _prd(client, auth)
    assert prd["status"] == "draft"
    dry = client.post(f"/api/prds/{prd['id']}/decompose", headers=auth)
    assert dry.status_code == 200
    body = dry.json()
    assert [p["section"] for p in body["proposals"]] == ["Ingest", "Transform"]
    assert body["created"] == []


def test_create_on_a_draft_is_refused_and_creates_nothing(client, auth):
    """THE CALL. Service, not the router: MCP and the assistant share this."""
    prd = _prd(client, auth)
    r = client.post(f"/api/prds/{prd['id']}/decompose?create=true", headers=auth)
    assert r.status_code == 409, r.text
    assert "approved" in r.json()["detail"]
    assert "grill" in r.json()["detail"]
    assert "draft" in r.json()["detail"]
    items = client.get("/api/items?project_id=core", headers=auth).json()
    assert [i for i in items if i.get("prd_id") == prd["id"]] == []


def test_create_on_review_is_refused(client, auth):
    prd = _prd(client, auth)
    s = SessionLocal()
    try:
        row = prd_svc.get_prd(s, prd["id"])
        prd_svc.record_grill_turns(s, row.id, [{"role": "user", "text": "started"}])
        prd_svc.sync_status(s, row)
        s.refresh(row)
        assert row.status == "review"
    finally:
        s.close()
    r = client.post(f"/api/prds/{prd['id']}/decompose?create=true", headers=auth)
    assert r.status_code == 409
    assert "review" in r.json()["detail"]


def test_create_on_approved_still_files(client, auth):
    prd = _prd(client, auth)
    approve_id(prd["id"])
    r = client.post(f"/api/prds/{prd['id']}/decompose?create=true", headers=auth)
    assert r.status_code == 200, r.text
    assert len(r.json()["created"]) == 2


def test_mcp_create_on_a_draft_is_conflict_not_validation(client, auth):
    """validation would send an agent editing a payload that is already correct."""
    prd = _prd(client, auth)
    key = _key(client, auth)
    result = _mcp(client, key, "decompose_prd", {"prd_id": prd["id"], "create": True})
    assert result.get("isError") is True
    err = result["structuredContent"]["error"]
    assert err["code"] == "conflict", err
    assert "approved" in err["message"]
    assert "grill" in err.get("hint", "") + err["message"]


def test_mcp_create_on_approved_still_files(client, auth):
    prd = _prd(client, auth)
    approve_id(prd["id"])
    key = _key(client, auth)
    result = _mcp(client, key, "decompose_prd", {"prd_id": prd["id"], "create": True})
    assert result.get("isError") is not True
    assert len(result["structuredContent"]["created"]) == 2


def test_assistant_create_on_a_draft_is_not_fully_tracked(client):
    """A refused draft must not read as 'already fully tracked' (absence as clean)."""
    s = SessionLocal()
    try:
        prd = prd_svc.create_prd(s, title="Chat Spec", project_id="core", body=BODY)
        ctx = at.ToolContext(db=s, user_id="u1", project_id="core",
                             entity_type="prd", entity_id=prd.id)
        r = at.dispatch(ctx, ToolCall(id="c1", name="decompose_prd", input={}))
        assert r.is_error
        assert "approved" in r.content
        assert "fully tracked" not in r.content
        assert items_svc.search_items(s, query="Ingest", project_id="core") == []
    finally:
        s.close()


def test_assistant_create_on_approved_still_files(client):
    s = SessionLocal()
    try:
        prd = prd_svc.create_prd(s, title="Chat Spec", project_id="core", body=BODY)
        approve(s, prd)
        ctx = at.ToolContext(db=s, user_id="u1", project_id="core",
                             entity_type="prd", entity_id=prd.id)
        r = at.dispatch(ctx, ToolCall(id="c1", name="decompose_prd", input={}))
        assert not r.is_error
        assert "created" in r.content
        assert any(i.prd_id == prd.id for i in items_svc.search_items(s, query="Ingest",
                                                                     project_id="core"))
    finally:
        s.close()


def test_mcp_description_names_the_gate():
    from app.mcp_server import TOOLS
    desc = next(t["description"] for t in TOOLS if t["name"] == "decompose_prd")
    assert "approved" in desc
    assert "grill" in desc


def test_decompose_consults_status_before_create_item():
    """THE CALL pin. A gate after the loop is a gate nobody hits on the first write."""
    import ast
    import inspect
    src = inspect.getsource(prd_svc.decompose)
    tree = ast.parse(src)
    fn = tree.body[0]
    text = ast.dump(fn)
    assert "DecomposeRequiresApproval" in text
    assert "approved" in text
    # The raise is inside `if create`, and create_item is also there — the raise must
    # appear first so a draft never reaches the write.
    raise_at = src.find("DecomposeRequiresApproval")
    write_at = src.find("create_item")
    assert 0 < raise_at < write_at


def test_service_raises_and_writes_nothing(client):
    s = SessionLocal()
    try:
        prd = prd_svc.create_prd(s, title="Svc", project_id="core", body=BODY)
        with pytest.raises(prd_svc.DecomposeRequiresApproval, match="draft"):
            prd_svc.decompose(s, prd, create=True)
        assert items_svc.search_items(s, query="Ingest", project_id="core") == []
        out = prd_svc.decompose(s, prd, create=False)
        assert len(out["proposals"]) == 2 and out["created"] == []
    finally:
        s.close()
