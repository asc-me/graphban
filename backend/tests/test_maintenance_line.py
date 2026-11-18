"""The maintenance line (GRPH-1681): one per project, identity only, `line_id` on fixes."""
import pytest

from app.models import Item, Line, Project
from app.services import items as items_svc
from app.services import lines as lines_svc
from app.db import SessionLocal


def _rpc(client, key, tool, arguments):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments}}
    return client.post("/api/mcp", json=body, headers={"X-API-Key": key}).json()["result"]


def _project(client, auth, name):
    return client.post("/api/projects", json={"name": name}, headers=auth).json()["id"]


def _fleet_key(client, auth, proj, role):
    return client.post("/api/fleet/keys", json={"project_id": proj, "role": role, "wave": "w1"},
                       headers=auth).json()["plaintext"]


def test_create_line_twice_refuses_and_names_the_first(client, auth):
    proj = _project(client, auth, "LineTwice")
    planner = _fleet_key(client, auth, proj, "planner")
    ctx = _rpc(client, planner, "get_context", {})["structuredContent"]
    assert ctx["maintenance_line"] is None

    first = _rpc(client, planner, "create_line", {})
    assert not first.get("isError"), first
    line = first["structuredContent"]
    assert line["title"] == "Maintenance"

    ctx = _rpc(client, planner, "get_context", {})["structuredContent"]
    assert ctx["maintenance_line"] == line

    second = _rpc(client, planner, "create_line", {"title": "Other"})
    assert second["isError"]
    assert line["id"] in str(second["structuredContent"])
    # and a person through REST is refused the same way
    r = client.post(f"/api/projects/{proj}/line", json={}, headers=auth)
    assert r.status_code == 409 and line["id"] in r.text

    # the key is a line key, so get_prd cannot resolve it
    got = _rpc(client, planner, "get_prd", {"prd_id": line["id"]})
    assert got["isError"]


def test_a_worker_key_is_refused(client, auth):
    proj = _project(client, auth, "LineWorker")
    worker = _fleet_key(client, auth, proj, "worker")
    res = _rpc(client, worker, "create_line", {})
    assert res["isError"]
    with SessionLocal() as db:
        assert lines_svc.get_line(db, proj) is None


def test_a_person_creates_it_through_rest_with_a_title(client, auth):
    proj = _project(client, auth, "LineRest")
    assert client.get(f"/api/projects/{proj}/line", headers=auth).json() is None
    r = client.post(f"/api/projects/{proj}/line", json={"title": "Upkeep"}, headers=auth)
    assert r.status_code == 201
    assert r.json()["title"] == "Upkeep"
    assert client.get(f"/api/projects/{proj}/line", headers=auth).json() == r.json()


def test_line_id_rules(client, auth):
    proj = _project(client, auth, "LineRules")
    with SessionLocal() as db:
        # nothing creates the line implicitly
        with pytest.raises(ValueError, match="create_line"):
            items_svc.create_item(db, title="x", project_id=proj, kind="fix", line_id="NOPE-L1")
        line = lines_svc.create_line(db, proj)
        key = line.key
        fix = items_svc.create_item(db, title="bug", project_id=proj, kind="fix", line_id=key)
        assert fix.line_id == line.id and fix.line_key == key
        assert items_svc.item_dict(fix)["line_id"] == key
        for kind in ("feature", "chore", ""):
            with pytest.raises(ValueError, match="only a fix"):
                items_svc.create_item(db, title="x", project_id=proj, kind=kind, line_id=key)
        with pytest.raises(ValueError, match="both"):
            items_svc.create_item(db, title="x", project_id=proj, kind="fix", line_id=key,
                                  prd_id="ANY-P1")
        # updates are held to the same rules
        with pytest.raises(ValueError, match="only a fix"):
            items_svc.update_item(db, fix.id, kind="feature")
        plain = items_svc.create_item(db, title="feat", project_id=proj, kind="feature")
        with pytest.raises(ValueError, match="only a fix"):
            items_svc.update_item(db, plain.id, line_id=key)
        # clearing is allowed
        assert items_svc.update_item(db, fix.id, line_id="").line_id is None


def test_retag_keeps_the_stored_id_and_the_key_follows(client, auth):
    proj = _project(client, auth, "LineRetag")
    with SessionLocal() as db:
        line = lines_svc.create_line(db, proj)
        stored, old_key = line.id, line.key
        project = db.get(Project, proj)
        project.tag = "RTAG"
        db.commit()
        db.refresh(line)
        assert line.id == stored
        assert line.key == "RTAG-L1" != old_key
