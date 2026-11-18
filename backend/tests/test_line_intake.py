"""Intake and the unscoped report (GRPH-1686)."""
from app.db import SessionLocal
from app.models import Item, Request
from app.services import items as items_svc
from app.services import lines as lines_svc


def _rpc(client, key, tool, arguments):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments}}
    return client.post("/api/mcp", json=body, headers={"X-API-Key": key}).json()["result"]


def _project(client, auth, name):
    return client.post("/api/projects", json={"name": name}, headers=auth).json()["id"]


def _request(db, proj, type_, title):
    from app.services import requests as req_svc
    return req_svc.create_request(db, project_id=proj, type_=type_, title=title, detail="no list")


def test_bug_accept_lands_on_the_line_as_a_fix(client, auth):
    from app.services import requests as req_svc
    proj = _project(client, auth, "IntakeBug")
    with SessionLocal() as db:
        line = lines_svc.create_line(db, proj)
        req = _request(db, proj, "bug", "it breaks")
        _, item = req_svc.accept_request(db, req.id)
        assert item.kind == "fix" and item.line_id == line.id and not item.prd_id


def test_bug_accept_refuses_prd_id_and_missing_line_leaving_request_new(client, auth):
    import pytest
    from app.services import requests as req_svc
    proj = _project(client, auth, "IntakeRefuse")
    with SessionLocal() as db:
        req = _request(db, proj, "bug", "no line yet")
        with pytest.raises(ValueError, match="create_line"):
            req_svc.accept_request(db, req.id)
        db.rollback()
        assert db.get(Request, req.id).status == "new"
        lines_svc.create_line(db, proj)
        with pytest.raises(ValueError, match="prd_id"):
            req_svc.accept_request(db, req.id, prd_id="GRPH-P1")
        db.rollback()
        assert db.get(Request, req.id).status == "new"


def test_feature_request_accept_is_unchanged(client, auth):
    from app.services import requests as req_svc
    proj = _project(client, auth, "IntakeFeat")
    with SessionLocal() as db:
        req = _request(db, proj, "feature", "a thing")
        _, item = req_svc.accept_request(db, req.id)
        assert not item.line_id and item.kind != "fix"


def test_unscoped_fixes_lists_only_scopeless_fixes_and_zero_is_present(client, auth):
    proj = _project(client, auth, "IntakeUnscoped")
    with SessionLocal() as db:
        assert lines_svc.unscoped_fixes(db, proj) == {"ids": [], "count": 0}
        line = lines_svc.create_line(db, proj)
        on_line = items_svc.create_item(db, title="on line", project_id=proj, kind="fix",
                                        line_id=line.id)
        loose = items_svc.create_item(db, title="loose", project_id=proj, tags=["bug"])
        feat = items_svc.create_item(db, title="feat", project_id=proj, kind="feature")
        out = lines_svc.unscoped_fixes(db, proj)
        assert out == {"ids": [loose.key], "count": 1}
        assert on_line.key not in out["ids"] and feat.key not in out["ids"]
    r = client.get(f"/api/projects/{proj}/line/unscoped_fixes", headers=auth)
    assert r.status_code == 200 and r.json()["count"] == 1
