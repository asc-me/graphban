"""Filing a fix (GRPH-1695): duplicates refused unless distinct_from, found_during provenance."""
import pytest

from app.db import SessionLocal
from app.models import Link
from app.services import items as items_svc
from app.services import prioritization

ACC = "Does it.\n\n## Acceptance\n- the fix works\n"


def _rpc(client, key, tool, arguments):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments}}
    return client.post("/api/mcp", json=body, headers={"X-API-Key": key}).json()["result"]


@pytest.fixture
def world(client, auth):
    proj = client.post("/api/projects", json={"name": "FixProv"}, headers=auth).json()["id"]
    key = client.post("/api/fleet/keys", json={"project_id": proj, "role": "planner", "wave": "w1"},
                      headers=auth).json()["plaintext"]
    line = _rpc(client, key, "create_line", {})["structuredContent"]["id"]
    return {"key": key, "line": line}


def _fix(client, w, tps=("backend/app/x.py",), **args):
    return _rpc(client, w["key"], "create_item",
                {"title": "bug", "kind": "fix", "description": ACC, "line_id": w["line"],
                 "touchpoints": list(tps), **args})


def _links(item_key, type_):
    with SessionLocal() as db:
        it = items_svc.get_item(db, item_key)
        return [(db.get(items_svc.Item, l.b).key, l.reason)
                for l in db.query(Link).filter(Link.a == it.id, Link.type == type_)]


def test_overlap_refused_then_accepted_with_distinct_from(client, world):
    first = _fix(client, world)["structuredContent"]["id"]
    refused = _fix(client, world)
    assert refused["isError"] and first in str(refused)
    bad = _fix(client, world, distinct_from=[{"id": first, "reason": "  "}])
    assert bad["isError"]
    ok = _fix(client, world, distinct_from=[{"id": first, "reason": "other module"}])
    assert not ok.get("isError"), ok
    assert _links(ok["structuredContent"]["id"], "semantic") == [(first, "other module")]


def test_naming_only_some_overlaps_lists_the_rest(client, world):
    a = _fix(client, world, tps=("backend/app/a.py",))["structuredContent"]["id"]
    b = _fix(client, world, tps=("backend/app/b.py",))["structuredContent"]["id"]
    res = _fix(client, world, tps=("backend/app/a.py", "backend/app/b.py"),
               distinct_from=[{"id": a, "reason": "r"}])
    assert res["isError"] and b in str(res) and "possible duplicate" in str(res)


def test_line_item_with_no_held_item_reads_found_during_null(client, world):
    out = _fix(client, world)["structuredContent"]
    assert out["found_during"] is None


def test_explicit_found_during_links_and_does_not_block(client, world):
    host = _rpc(client, world["key"], "create_item",
                {"title": "host", "kind": "chore"})["structuredContent"]["id"]
    out = _fix(client, world, found_during=host)["structuredContent"]
    assert out["found_during"] == host
    assert _links(out["id"], "found_during")[0][0] == host
    with SessionLocal() as db:
        ctx = prioritization.context(db, items_svc.get_item(db, host).project_id)
        for k in (host, out["id"]):
            it = items_svc.get_item(db, k)
            assert prioritization.ready(ctx, it), k
            assert prioritization.blocked_by(ctx, it) == []


def test_blocking_fix_is_not_a_duplicate_of_the_item_it_blocks(client):
    from app.services import prds as prd_svc
    with SessionLocal() as db:
        prd = prd_svc.create_prd(db, title="Spec", project_id="core",
                                 body="# S\n\n## Build\n\nBuild it.\n")
        feat = items_svc.create_item(db, title="feat", project_id="core", kind="feature",
                                     prd_id=prd.id, prd_section="Build",
                                     touchpoints=["backend/app/x.py"])
        fix = items_svc.create_item(db, title="bug", project_id="core", kind="fix",
                                    prd_id=prd.id, description=ACC, agent_create=True,
                                    touchpoints=["backend/app/x.py"], blocks=feat.key)
        assert fix.kind == "fix"
        other = items_svc.create_item(db, title="feat2", project_id="core", kind="feature",
                                      prd_id=prd.id, prd_section="Build",
                                      touchpoints=["backend/app/x.py"])
        # A second fix blocking `feat` still overlaps `fix` and `other` — those stay refused.
        with pytest.raises(ValueError) as e:
            items_svc.create_item(db, title="bug2", project_id="core", kind="fix",
                                  prd_id=prd.id, description=ACC, agent_create=True,
                                  touchpoints=["backend/app/x.py"], blocks=feat.key)
        msg = str(e.value)
        assert fix.key in msg and other.key in msg and feat.key not in msg


def test_found_during_item_is_not_a_duplicate(client, world):
    host = _fix(client, world)["structuredContent"]["id"]
    out = _fix(client, world, found_during=host)
    assert not out.get("isError"), out
    assert out["structuredContent"]["found_during"] == host


def test_held_item_defaults_found_during_and_two_held_refused(client, world):
    w = world
    reg = _rpc(client, w["key"], "register_agent", {})["structuredContent"]
    agent = reg["agent_id"]
    one = _rpc(client, w["key"], "create_item", {"title": "one", "kind": "chore"})
    two = _rpc(client, w["key"], "create_item", {"title": "two", "kind": "chore"})
    k1, k2 = one["structuredContent"]["id"], two["structuredContent"]["id"]
    with SessionLocal() as db:
        a = items_svc.get_item(db, k1)
        a.status, a.claimed_by = "in_progress", agent
        db.commit()
    out = _fix(client, w, agent_id=agent)["structuredContent"]
    assert out["found_during"] == k1
    with SessionLocal() as db:
        b = items_svc.get_item(db, k2)
        b.status, b.claimed_by = "in_progress", agent
        db.commit()
    res = _fix(client, w, tps=("backend/app/y.py",), agent_id=agent)
    assert res["isError"] and k1 in str(res) and k2 in str(res)
