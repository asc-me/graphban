"""Blocking fixes hold the PRD (GRPH-1684): listed in completeness, out of coverage and
scope drift, and close is refused while one is open."""
import pytest

from app.services import items as items_svc
from app.services import prds as prd_svc
from tests import attest

BODY = ("# Spec\n\n## Problem\n\nSomething.\n\n## Build\n\nBuild it.\n\n"
        "## Extra\n\nMore.\n")
ACC = "Does it.\n\n## Acceptance\n- works\n"


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _approve(db, prd):
    prior = prd_svc.grill_history(db, prd.id, since=prd_svc.grill_window(db, prd.id))
    prd_svc.record_grill_turns(db, prd.id, prior + [{"role": "user", "text": "Answer."}])
    for name in prd_svc.DIMENSIONS:
        prd_svc.set_dimension(db, prd.id, name, "resolved")
    prd_svc.sync_status(db, prd)
    return prd


@pytest.fixture()
def prd(db):
    return _approve(db, prd_svc.create_prd(db, title="Spec", project_id="core", body=BODY))


def _fix(db, prd):
    feat = items_svc.create_item(db, title="feat", project_id="core", kind="feature",
                                 prd_id=prd.id, prd_section="Build")
    fix = items_svc.create_item(db, title="bug", project_id="core", kind="fix",
                                prd_id=prd.id, description=ACC,
                                touchpoints=["backend/app/x.py"], blocks=feat.key)
    return feat, fix


def test_blocking_fix_is_listed_and_changes_no_section(db, prd):
    before = prd_svc.completeness(db, prd)
    assert before["blocking_fixes"] == []
    feat, fix = _fix(db, prd)
    out = prd_svc.completeness(db, prd)
    assert out["blocking_fixes"] == [{"id": fix.key, "status": fix.status, "blocks": [feat.key]}]
    assert out["outside_baseline"] == []
    build = [s for s in out["sections"] if s["section"] == "Build"][0]
    assert build["planned"] == 1 and fix.key not in [i["id"] for i in build["items"]]


def test_excluded_from_coverage_and_scope_drift(db, prd):
    feat, fix = _fix(db, prd)
    cov = prd_svc.coverage(db, prd)
    assert fix.key not in [k for s in cov["sections"] for k in s["item_ids"]]
    assert fix.key not in [o["id"] for o in cov["orphaned"]]
    assert cov["total_items"] == 1
    assert fix.key not in [r["id"] for r in prd_svc.scope_drift(db, prd)["scope_added"]]


def test_close_refused_while_open_then_allowed_when_done(db, prd):
    feat, fix = _fix(db, prd)
    ready = prd_svc.close_readiness(db, prd)
    assert ready["can_close"] is False and fix.key in ready["blocked_on"]
    dispositions = [{"section": s, "disposition": "deferred", "reason": "later"}
                    for s in prd_svc.dropped_intent(db, prd)]
    with pytest.raises(prd_svc.CloseRefused) as exc:
        prd_svc.close_prd(db, prd, dispositions=dispositions, closed_by="user:t")
    assert fix.key in str(exc.value)

    attest.complete(db, fix.id)
    db.refresh(fix)
    assert prd_svc.close_readiness(db, prd)["can_close"] is True
    assert [f["status"] for f in prd_svc.completeness(db, prd)["blocking_fixes"]] == ["done"]
    prd_svc.close_prd(db, prd, dispositions=dispositions, closed_by="user:t")
