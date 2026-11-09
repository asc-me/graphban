"""`done` is refused for a salvaged branch that has no PR (GRPH-1457).

The PR gates apply only to an item with a linked PR, so a missing PR read as "no PR needed":
a branch the supervisor pushed but did not propose went `done` with no CI attestation and its
code on no PR.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.services import items as items_svc

CUT_FROM = "1111222233334444555566667777888899990000"
PR = {"kind": "url", "url": "https://github.com/asc-me/graphban/pull/999", "detail": "PR #999"}
SALVAGE = {"kind": "note",
           "detail": "gbfleet: salvaged onto `gb/p73-118` `01ea7d0dfc2c`; not proposed as a PR"}


@pytest.fixture()
def db(client):
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def cooldown(monkeypatch):
    monkeypatch.setattr(settings, "pr_cooldown_seconds", 60, raising=False)


def _salvaged(db, *, branch="gb/p73-118"):
    it = items_svc.create_item(db, project_id="core", title="Salvaged")
    it.branch = branch
    it.evidence = items_svc.append_evidence(it.evidence, [
        {"kind": "note", "detail": f"{items_svc.CUT_FROM_MARKER}{CUT_FROM} (`{branch}`)"},
        SALVAGE])
    db.commit()
    db.refresh(it)
    return it


def test_done_is_refused_for_a_salvaged_branch_with_no_pr_naming_the_branch(db, cooldown):
    item = _salvaged(db)
    with pytest.raises(items_svc.NoPRForBranch) as exc:
        items_svc.refuse_if_pr_cooling_down(db, item, [])
    assert "gb/p73-118" in str(exc.value) and "no PR" in str(exc.value)


def test_a_linked_pr_lifts_the_no_pr_refusal(db, cooldown):
    """The control: the refusal is about the missing PR, not the salvage note."""
    item = _salvaged(db)
    with pytest.raises(items_svc.PRCooldown) as exc:
        items_svc.refuse_if_pr_cooling_down(db, item, [PR])
    assert not isinstance(exc.value, items_svc.NoPRForBranch)


def test_an_ordinary_branch_without_a_pr_is_not_refused_here(db, cooldown):
    item = items_svc.create_item(db, project_id="core", title="Plain")
    item.branch = "gb/plain"
    db.commit()
    assert items_svc.refuse_if_pr_cooling_down(db, item, []) == []
