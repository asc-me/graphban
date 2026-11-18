"""Filing a blocking fix releases the holder to blocked (GRPH-1696); the brief states the exits."""
import pytest

from app.db import SessionLocal
from app.services import delegation as D
from app.services import items as items_svc
from app.services import prds as prd_svc

ACC = "Does it.\n\n## Acceptance\n- the fix works\n"


def _setup(db, holder):
    prd = prd_svc.create_prd(db, title="Spec", project_id="core",
                             body="# S\n\n## Build\n\nBuild it.\n")
    feat = items_svc.create_item(db, title="feat", project_id="core", kind="feature",
                                 prd_id=prd.id, prd_section="Build")
    feat.status, feat.claimed_by = "in_progress", holder
    db.commit()
    return prd, feat


def _file(db, prd, feat, held):
    return items_svc.create_item(db, title="bug", project_id="core", kind="fix", prd_id=prd.id,
                                 description=ACC, agent_create=True, touchpoints=["a/x.py"],
                                 blocks=feat.key, held=held)


def test_holder_released_to_blocked_naming_the_fix(client):
    with SessionLocal() as db:
        prd, feat = _setup(db, "A-1")
        fix = _file(db, prd, feat, [feat])
        db.refresh(feat)
        assert feat.status == "blocked" and feat.claimed_by is None
        assert fix.key in feat.blocker


def test_other_agents_lease_is_left_alone(client):
    with SessionLocal() as db:
        prd, feat = _setup(db, "A-2")
        _file(db, prd, feat, [])
        db.refresh(feat)
        assert feat.status == "in_progress" and feat.claimed_by == "A-2"


def test_brief_text_states_exits_and_no_line_key():
    t = D.FIX_EXITS_LINE
    assert "blocking fix" in t and "line_id" in t and "get_context.maintenance_line" in t
    assert "scope is required" in t.lower()
