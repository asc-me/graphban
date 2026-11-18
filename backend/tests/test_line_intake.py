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


def test_accepted_bug_is_unready_until_clause_and_touchpoint_then_claimable(client, auth):
    """Clause 2: the line item a bug accept creates has no clause and no touchpoint, so
    claim_cluster and claim_next skip it (GRPH-1685's predicate) and the drain reports it
    unready. Writing both makes it claimable. A feature accept is not held by that rule."""
    from app.services import fleet as fleet_svc
    from app.services import requests as req_svc
    proj = _project(client, auth, "IntakeUnready")
    key = client.post("/api/api-keys", json={"name": "k", "project_id": proj,
                                             "scopes": ["read", "write", "gate"]},
                      headers=auth).json()["plaintext"]
    with SessionLocal() as db:
        line = lines_svc.create_line(db, proj)
        req = _request(db, proj, "bug", "it breaks")
        _, item = req_svc.accept_request(db, req.id, status="next")
        bug_key, bug_id = item.key, item.id
        assert items_svc.unready_reasons(item) == ["acceptance", "touchpoints"]
        _row, code = fleet_svc.issue_enrolment(db, project_id=proj, role="worker",
                                               line_id=line.id)
    res = _rpc(client, key, "register_agent", {"label": "line", "enrolment_code": code})
    seat = res["structuredContent"]["agent_id"]
    with SessionLocal() as db:
        assert fleet_svc.claim_cluster(db, agent_id=seat, project_id=proj)["claimed"] is False
        assert items_svc.claim_next(db, agent_id=seat, project_id=proj) is None
        assert items_svc.unready_line_items(db, proj) == [
            {"id": bug_key, "missing": ["acceptance", "touchpoints"]}]
        items_svc.update_item(db, bug_id, description="## Acceptance\n- it no longer breaks\n",
                              touchpoints=["a/x.py"])
        assert items_svc.unready_line_items(db, proj) == []
        got = items_svc.claim_next(db, agent_id=seat, project_id=proj)
        assert got is not None and got.key == bug_key
        feat = req_svc.accept_request(db, _request(db, proj, "feature", "f").id)[1]
        assert items_svc.unready_reasons(feat) == []
