"""GRPH-1368 — specs and designs align to a linked PRD instead of being accepted."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.mcp_server import TOOLS
from app.models import DesignSet, Project
from app.services import design_sets as ds_svc
from app.services import documents as docs
from app.services import prds as prd_svc
from app.services import spec_alignment as sa
from tests.prd_approve import approve

BODY = "## Tokens\nUse the teal accent everywhere.\n\n## Spacing\nEight point grid.\n"
KEY = "key:alex"


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def pid(db):
    return db.scalars(select(Project.id)).first()


def _set(db, pid, prd, constraints="Teal only."):
    sid = ds_svc.open_design(db, pid, prd.id)["id"]
    ds_svc.put_set_constraints(db, pid, sid, constraints, screens=[{"screen": "home"}])
    return sid


def test_no_linked_prd_is_never_approved(db):
    out = sa.assess(db, None, ["## Tokens\n## Spacing"])
    assert out == {"linked_prd": None, "prd_passed": False, "gaps": [],
                   "functionally_complete": False, "approved_to_build": False}


def test_a_preamble_with_no_prd_is_stored_and_not_approved(db, pid):
    out = docs.put_document(db, project_id=pid, kind="preamble", body="Be calm.", prd_id=None,
                            created_by=KEY)
    assert out["prd_id"] is None
    got = docs.get_document(db, pid, out["id"])["alignment"]
    assert got["linked_prd"] is None and got["approved_to_build"] is False


def test_a_prd_with_no_baseline_is_not_approved(db, pid):
    prd = prd_svc.create_prd(db, title="Draft", project_id=pid, body=BODY)
    out = sa.assess(db, prd.id, [BODY])
    assert out["linked_prd"] == prd.key and out["prd_passed"] is False
    assert out["gaps"] == [] and out["functionally_complete"] is True
    assert out["approved_to_build"] is False


def test_one_missing_heading_is_a_gap(db, pid):
    prd = prd_svc.create_prd(db, title="P", project_id=pid, body=BODY)
    approve(db, prd)
    out = sa.assess(db, prd.id, ["## tokens\n\nonly this"])
    assert out["gaps"] == ["Spacing"]
    assert out["functionally_complete"] is False and out["approved_to_build"] is False
    assert out["prd_passed"] is True


def test_a_full_match_is_approved_to_build_case_and_space_folded(db, pid):
    prd = prd_svc.create_prd(db, title="P", project_id=pid, body=BODY)
    approve(db, prd)
    out = sa.assess(db, prd.id, ["TOKENS here", "the   SPACING\nrules"])
    assert out["gaps"] == [] and out["approved_to_build"] is True


def test_design_set_aligns_on_constraints_and_compiles_with_no_prior_accept(db, pid):
    prd = prd_svc.create_prd(db, title="P", project_id=pid, body=BODY)
    approve(db, prd)
    sid = _set(db, pid, prd, constraints="Tokens: teal.")
    ds = db.get(DesignSet, sid)
    assert sa.for_design_set(db, ds)["gaps"] == ["Spacing"]
    assert ds_svc.get_design_set(db, pid, sid)["alignment"]["approved_to_build"] is False
    # nothing accepted, no tier confirmed: compile still runs, and the prompt carries the PRD
    ds_svc.compile_prompts(db, pid, sid)
    out = ds_svc.get_design_set(db, pid, sid)["alignment"]
    assert out["gaps"] == [] and out["approved_to_build"] is True


def test_a_design_set_on_an_unbaselined_prd_is_not_approved(db, pid):
    prd = prd_svc.create_prd(db, title="P", project_id=pid, body=BODY)
    sid = _set(db, pid, prd, constraints="Tokens and Spacing.")
    out = ds_svc.get_design_set(db, pid, sid)["alignment"]
    assert out["prd_passed"] is False and out["approved_to_build"] is False


def test_the_accept_tools_are_gone_and_accept_gated_is_empty():
    names = {t["name"] for t in TOOLS}
    assert not names & {"accept_set_constraints", "accept_prompt", "accept_prompts",
                        "accept_design"}
    assert docs.ACCEPT_GATED == ()
