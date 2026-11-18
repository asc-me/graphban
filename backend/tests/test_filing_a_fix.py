"""Filing a fix (GRPH-1683): scope required, blocking-fix shape, severity, acceptance and
touchpoints on an agent create."""
import pytest

from app.db import SessionLocal
from app.models import Item, Link
from app.services import fleet as fleet_svc
from app.services import items as items_svc

ACC = "Does it.\n\n## Acceptance\n- the fix works\n"


def _rpc(client, key, tool, arguments):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments}}
    return client.post("/api/mcp", json=body, headers={"X-API-Key": key}).json()["result"]


@pytest.fixture
def world(client, auth):
    proj = client.post("/api/projects", json={"name": "FixFiling"}, headers=auth).json()["id"]
    key = client.post("/api/fleet/keys", json={"project_id": proj, "role": "planner", "wave": "w1"},
                      headers=auth).json()["plaintext"]
    prd = _rpc(client, key, "create_prd", {"title": "P"})["structuredContent"]["id"]
    on_prd = _rpc(client, key, "create_item", {"title": "feat", "kind": "feature",
                                               "prd_id": prd})["structuredContent"]["id"]
    return {"key": key, "proj": proj, "prd": prd, "feat": on_prd}


def _create(client, w, **args):
    return _rpc(client, w["key"], "create_item",
                {"title": "bug", "kind": "fix", "description": ACC,
                 "touchpoints": ["backend/app/x.py"], **args})


def _line(client, w):
    return _rpc(client, w["key"], "create_line", {})["structuredContent"]["id"]


def test_fix_with_no_scope_is_refused_naming_the_exits(client, world):
    res = _create(client, world)
    assert res["isError"]
    text = str(res)
    assert "prd_id" in text and "line_id" in text and "fix_in_build" in text


def test_feature_with_line_and_security_on_chore_and_empty_kind(client, world):
    line = _line(client, world)
    assert _rpc(client, world["key"], "create_item",
                {"title": "f", "kind": "feature", "line_id": line})["isError"]
    assert _rpc(client, world["key"], "create_item",
                {"title": "f", "line_id": line})["isError"]
    assert _rpc(client, world["key"], "create_item",
                {"title": "c", "kind": "chore", "severity": "security"})["isError"]


def test_blocking_fix_shape(client, world):
    w = world
    bad_section = _create(client, w, prd_id=w["prd"], prd_section="S", blocks=w["feat"])
    assert bad_section["isError"] and "prd_section" in str(bad_section)
    no_dep = _create(client, w, prd_id=w["prd"])
    assert no_dep["isError"] and "blocks" in str(no_dep)
    ok = _create(client, w, prd_id=w["prd"], blocks=w["feat"], severity="security")
    assert not ok.get("isError"), ok
    fix = ok["structuredContent"]["id"]
    with SessionLocal() as db:
        item = items_svc.get_item(db, fix)
        assert item.severity == "security"
        assert items_svc.has_blocking_dependency(db, item)
        assert items_svc.is_filed_fix(db, item)
        link = db.query(Link).filter(Link.b == item.id).one()
        assert items_svc.get_item(db, w["feat"]).id == link.a


def test_line_fix_rules(client, world):
    w = world
    no_line = _create(client, w, line_id="X-L1")
    assert no_line["isError"] and "create_line" in str(no_line)
    line = _line(client, w)
    assert _create(client, w, line_id=line, prd_id=w["prd"])["isError"]
    assert not _create(client, w, line_id=line).get("isError")


def test_agent_create_needs_acceptance_and_touchpoint(client, world):
    line = _line(client, world)
    no_acc = _create(client, world, line_id=line, description="nothing")
    assert no_acc["isError"] and "acceptance" in str(no_acc) and "touchpoint" not in str(no_acc)
    no_tp = _create(client, world, line_id=line, touchpoints=[])
    assert no_tp["isError"] and "touchpoint" in str(no_tp) and "acceptance clause" not in str(no_tp)


def test_update_onto_the_line_and_signoff_shape(client, world):
    w = world
    line = _line(client, w)
    old = _rpc(client, w["key"], "create_item", {"title": "old", "kind": "chore"})["structuredContent"]["id"]
    # same row, neither kind nor scope changes: fine
    assert not _rpc(client, w["key"], "update_item", {"id": old, "title": "old2"}).get("isError")
    fixrow = _rpc(client, w["key"], "create_item", {"title": "x", "kind": "fix", "line_id": line,
                  "description": ACC, "touchpoints": ["a.py"]})["structuredContent"]["id"]
    with SessionLocal() as db:
        it = items_svc.get_item(db, fixrow)
        it.description = "nothing"
        db.commit()
        with pytest.raises(fleet_svc.FiledFixShape):
            _sign(db, it)
        legacy = items_svc.create_item(db, title="legacy", tags=["bug"], description="no clauses")
        assert legacy.kind == "fix"
        assert not items_svc.is_filed_fix(db, legacy)
    # setting line_id on a row with no clause is refused
    with SessionLocal() as db:
        with pytest.raises(ValueError, match="acceptance"):
            items_svc.update_item(db, old, kind="fix", line_id=line)


def _sign(db, item):
    """Reach the shape gate in sign_off: the gates before it are not what is under test."""
    from unittest import mock
    item.status = "review"
    item.built_by = "builder"
    db.commit()
    with mock.patch.object(fleet_svc, "_independent_of_author", return_value=True), \
         mock.patch.object(fleet_svc, "needs_adversarial_evidence", return_value=False), \
         mock.patch.object(fleet_svc, "require_reviewer_sabotage"), \
         mock.patch.object(fleet_svc, "require_gap_followups"), \
         mock.patch.object(fleet_svc, "require_reviewer_property"), \
         mock.patch.object(fleet_svc, "require_no_human_clause"):
        fleet_svc.sign_off(db, item_id=item.id, agent_id="reviewer")


def test_historical_prd_fix_is_not_a_filed_fix(client, world):
    """A pre-rule fix on a PRD section, or with nothing depending on it, keeps today's rule."""
    with SessionLocal() as db:
        feat = items_svc.get_item(db, world["feat"])
        # A bug-tagged default `fix` predates the scope rules, as historical rows do.
        on_section = items_svc.create_item(db, title="old", tags=["bug"], project_id=feat.project_id,
                                           prd_id=feat.prd_id, prd_section="S")
        no_dep = items_svc.create_item(db, title="old2", tags=["bug"], project_id=feat.project_id,
                                       prd_id=feat.prd_id)
        assert on_section.kind == no_dep.kind == "fix"
        assert not items_svc.is_filed_fix(db, on_section)
        assert not items_svc.is_filed_fix(db, no_dep)


def test_agent_bug_tag_with_no_kind_or_scope_is_refused(client, world):
    """A bug tag defaults kind to fix; over MCP that default is a filed fix and needs a scope."""
    res = _rpc(client, world["key"], "create_item",
               {"title": "bug", "tags": ["bug"], "description": "x"})
    assert res["isError"]
    text = str(res)
    assert "prd_id" in text and "line_id" in text and "fix_in_build" in text
    line = _line(client, world)
    unshaped = _rpc(client, world["key"], "create_item",
                    {"title": "bug", "tags": ["bug"], "description": "x", "line_id": line})
    assert unshaped["isError"] and "acceptance" in str(unshaped) and "touchpoint" in str(unshaped)
    ok = _rpc(client, world["key"], "create_item",
              {"title": "bug", "tags": ["bug"], "description": ACC, "line_id": line,
               "touchpoints": ["a.py"]})
    assert not ok.get("isError"), ok
    with SessionLocal() as db:
        assert items_svc.get_item(db, ok["structuredContent"]["id"]).kind == "fix"


def test_historical_fix_signs_off_with_no_clauses(client, world):
    """A historical `fix` (no line, not a blocking fix) still signs off on today's rule."""
    with SessionLocal() as db:
        feat = items_svc.get_item(db, world["feat"])
        legacy = items_svc.create_item(db, title="legacy", tags=["bug"], project_id=feat.project_id,
                                       description="no clauses")
        assert legacy.kind == "fix" and not items_svc.is_filed_fix(db, legacy)
        _sign(db, legacy)
        assert items_svc.get_item(db, legacy.id).status == "done"
