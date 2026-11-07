"""Build handoff (GRPH-1281): decompose_prd on a design-gated section."""
from __future__ import annotations

from sqlalchemy import select

from app.models import DesignSet, Item
from app.services import design_sets as ds_svc
from app.services import prds as prd_svc
from tests.test_design_review import (  # noqa: F401
    COMPILER, be, db, pid, world, _ready,
)


def _gate(db, pid, sid, sections):
    ds_svc.put_set_constraints(db, pid, sid, "Teal only.", sections=sections)
    row = db.get(DesignSet, sid)
    row.constraints_accepted = True
    db.commit()


def _items(db, prd):
    return {i.prd_section: i for i in db.scalars(select(Item).where(Item.prd_id == prd.id))}


def test_unaccepted_gated_section_files_blocked_and_others_normal(db, pid, world):
    prd, sid = world
    _gate(db, pid, sid, ["Tokens"])
    prd_svc.decompose(db, prd, create=True)
    items = _items(db, prd)
    assert items["Tokens"].status == "blocked"
    assert "not approved to build" in items["Tokens"].description
    assert "`open`" in items["Tokens"].description
    assert "Eight point grid" not in items["Tokens"].description
    assert "Use the teal accent" in items["Tokens"].description
    assert items["Spacing"].status == "backlog" and "Design revision" not in items["Spacing"].description


def test_accepted_gate_cites_revision_and_goes_stale(db, pid, world, be):
    prd, sid = world
    _gate(db, pid, sid, ["Tokens", "Spacing"])
    _ready(db, pid, sid, be)
    ds_svc.accept_design(db, pid, sid, "agent:GRPH-A2", "dana")
    prd_svc.decompose(db, prd, create=True)
    items = _items(db, prd)
    tok = items["Tokens"]
    assert tok.status == "backlog"
    assert f"{ds_svc.REVISION_MARK}" in tok.description and "Review record" in tok.description
    assert db.get(DesignSet, sid).prd_hash in tok.description
    # removing a section from an accepted set marks it stale and blocks citing items
    ds_svc.put_set_constraints(db, pid, sid, "Teal only.", sections=["Tokens"])
    db.expire_all()
    assert db.get(DesignSet, sid).status == "stale"
    assert db.get(DesignSet, sid).stale_reason == "sections"
    items = _items(db, prd)
    assert items["Tokens"].status == "blocked" and "stale" in items["Tokens"].blocker
    assert "Design stale" in items["Tokens"].description
    # the removed section is released
    assert items["Spacing"].status == "backlog", (items["Spacing"].blocker, ds_svc.get_design_set(db, pid, sid)["sections"])


def test_an_aligned_set_on_a_baselined_prd_hands_off_with_no_accept(db, pid, world):
    """GRPH-1368: nobody accepts the set; its constraints carry every PRD heading, and the
    PRD is baselined, so the gated section files unblocked and pins the hashes."""
    prd, sid = world
    ds_svc.put_set_constraints(db, pid, sid, "Tokens: teal. Spacing: 8pt.", sections=["Tokens"])
    row = db.get(DesignSet, sid)
    row.constraints_accepted = False
    db.commit()
    ds_svc.compile_prompts(db, pid, sid)
    assert ds_svc.get_design_set(db, pid, sid)["alignment"]["approved_to_build"] is True
    prd_svc.decompose(db, prd, create=True)
    tok = _items(db, prd)["Tokens"]
    assert tok.status == "backlog", tok.blocker
    assert db.get(DesignSet, sid).prd_hash in tok.description
    assert "aligned to" in tok.description
