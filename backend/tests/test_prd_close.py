"""The terminal state, and what it costs to get there (GRPH-244 / PRD-12).

`approved` is set before work starts and never changes, so there was no event to hang an
acceptance check on. `closed` is that event.

The open question this section carried from v1.0 — *"if a PRD cannot leave the terminal
state the verdict is decorative; if it can, terminal is not terminal"* — was answered in
the v1.2 rebaseline by dissolving it. The PRD does not leave; the **work** does.

So close gates on **disposition**, never on delivery. Every baselined section with nothing
delivered must first be promoted or explicitly deferred. That is the grill's completion
standard one level up, and `deferred` completes rather than blocks for the same reason it
does there: the failure being caught is an implicit non-answer, not a conscious decision
to leave something open.
"""
import pytest

from app.services import items as items_svc
from app.services import prds as prd_svc
from app.services.platform import Resolved

BODY = (
    "# Spec\n\n"
    "## Problem\n\nNothing checks delivery.\n\n"
    "## Baseline\n\nFreeze the spec at approval.\n\n"
    "## Judging\n\nClassify each completed item against the goal.\n\n"
    "## Close report\n\nDelivered vs original intent.\n"
)


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _approve(db, prd):
    window = prd_svc.grill_window(db, prd.id)
    prior = prd_svc.grill_history(db, prd.id, since=window)
    prd_svc.record_grill_turns(db, prd.id, prior + [
        {"role": "user", "text": f"Answer, round {len(prd_svc.baseline_chain(db, prd.id))}."}])
    for name in prd_svc.DIMENSIONS:
        prd_svc.set_dimension(db, prd.id, name, "resolved")
    prd_svc.sync_status(db, prd)
    return prd


@pytest.fixture()
def approved(db):
    """One section delivered, three with nothing against them."""
    prd = _approve(db, prd_svc.create_prd(db, title="Spec", project_id="core", body=BODY))
    item = items_svc.create_item(db, title="Built it", project_id="core",
                                 prd_id=prd.id, prd_section="Baseline")
    items_svc.update_item(db, item.id, status="done")
    return prd


def _defer_all(db, prd, reason="Not needed for v1."):
    return [{"section": s, "disposition": "deferred", "reason": reason}
            for s in prd_svc.dropped_intent(db, prd)]


# ---- the gate --------------------------------------------------------------------------
def test_closing_with_intent_nobody_decided_about_is_refused(db, approved):
    """THE rule. A PRD may always close; what it may not do is close while pretending
    nothing was missed."""
    with pytest.raises(prd_svc.CloseRefused, match="nothing decided about them"):
        prd_svc.close_prd(db, approved, dispositions=[], closed_by="user:1")

    assert approved.status == "approved" and approved.close_record is None


def test_a_deferral_completes_rather_than_blocks(db, approved):
    """Exactly as in the grill: a conscious decision to drop something is an ANSWER. If
    deferring blocked, the only way to close would be to build everything, and the state
    would be unreachable in practice."""
    rec = prd_svc.close_prd(db, approved, dispositions=_defer_all(db, approved),
                            closed_by="user:1")

    assert approved.status == "closed"
    assert {d["disposition"] for d in rec["dispositions"]} == {"deferred"}


def test_a_deferral_needs_a_stated_reason(db, approved):
    """A deferral with no reason is indistinguishable from an oversight, which is the
    precise failure the grill's deferred/unanswered split exists to catch."""
    bad = [{"section": s, "disposition": "deferred", "reason": " "}
           for s in prd_svc.dropped_intent(db, approved)]

    with pytest.raises(prd_svc.CloseRefused, match="needs a stated reason"):
        prd_svc.close_prd(db, approved, dispositions=bad, closed_by="user:1")


def test_promotion_dispositions_create_the_work_and_record_where_it_went(db, approved):
    dispositions = [{"section": "Judging", "disposition": "promoted", "promote_to": "item"},
                    {"section": "Close report", "disposition": "promoted", "promote_to": "prd"},
                    {"section": "Decisions", "disposition": "deferred", "reason": "n/a"}]
    dispositions = [d for d in dispositions
                    if d["section"] in prd_svc.dropped_intent(db, approved)]

    rec = prd_svc.close_prd(db, approved, dispositions=dispositions, closed_by="user:1")
    by_section = {d["section"]: d for d in rec["dispositions"]}

    assert by_section["Judging"]["target"].startswith("CP-")
    assert by_section["Close report"]["target"].startswith("CP-P")
    assert [s["id"] for s in prd_svc.lineage(db, approved)["successors"]] == [
        by_section["Close report"]["target"]]


def test_a_section_dispositioned_twice_is_refused(db, approved):
    """The precondition is SET equality, not a count — a count would let one section be
    dispositioned twice while another was missed."""
    doubled = _defer_all(db, approved)
    doubled.append(dict(doubled[0]))

    with pytest.raises(prd_svc.CloseRefused, match="more than once"):
        prd_svc.close_prd(db, approved, dispositions=doubled, closed_by="user:1")


def test_dispositioning_delivered_work_is_refused(db, approved):
    """Recording that a shipped section was dropped would corrupt the close report — the
    one artifact a PM is meant to act on."""
    bad = _defer_all(db, approved) + [
        {"section": "Baseline", "disposition": "deferred", "reason": "?"}]

    with pytest.raises(prd_svc.CloseRefused, match="nothing to disposition"):
        prd_svc.close_prd(db, approved, dispositions=bad, closed_by="user:1")


def test_nothing_is_created_when_the_close_is_refused(db, approved):
    """Everything is validated before anything is created, so the overwhelming majority of
    failures happen with nothing written."""
    before = len(items_svc.list_items(db, project_id="core"))
    bad = [{"section": s, "disposition": "promoted", "promote_to": "item"}
           for s in prd_svc.dropped_intent(db, approved)]
    bad.append({"section": "Baseline", "disposition": "deferred", "reason": "?"})

    with pytest.raises(prd_svc.CloseRefused):
        prd_svc.close_prd(db, approved, dispositions=bad, closed_by="user:1")

    assert len(items_svc.list_items(db, project_id="core")) == before


# ---- the record ---------------------------------------------------------------------------
def test_the_close_records_the_baseline_it_closed_against(db, approved):
    rec = prd_svc.close_prd(db, approved, dispositions=_defer_all(db, approved),
                            closed_by="user:1", verdict="delivered what mattered")

    assert rec["baseline_version"] == "v1.0"
    assert rec["verdict"] == "delivered what mattered"
    assert rec["closed_by"] == "user:1"


def test_a_mechanical_close_carries_its_disclosure(db, approved):
    """With no judge configured the close can prove every section was accounted for; it
    cannot say the work was any good. That limitation travels ON the record, so it cannot
    be dropped by whoever renders it."""
    rec = prd_svc.close_prd(db, approved, dispositions=_defer_all(db, approved),
                            closed_by="user:1")

    assert rec["mode"] == "mechanical"
    assert "not assessed" in rec["disclosure"]


def test_the_record_does_not_recompute(db, approved):
    """A SNAPSHOT. If it recomputed, a closed PRD could silently acquire undelivered
    sections nobody ever dispositioned, and the gate would be a thing that passed once
    rather than a thing that holds."""
    rec = prd_svc.close_prd(db, approved, dispositions=_defer_all(db, approved),
                            closed_by="user:1")
    before = list(rec["dispositions"])

    extra = items_svc.create_item(db, title="Late arrival", project_id="core",
                                  prd_id=approved.id, prd_section="Judging")
    items_svc.update_item(db, extra.id, status="done")
    db.refresh(approved)

    assert approved.close_record["dispositions"] == before


# ---- terminal means terminal ---------------------------------------------------------------
def test_a_closed_prd_cannot_be_edited(db, approved):
    prd_svc.close_prd(db, approved, dispositions=_defer_all(db, approved), closed_by="user:1")

    with pytest.raises(prd_svc.PrdClosed, match="cannot be edited"):
        prd_svc.update_prd(db, approved.id, body=BODY + "\n## Sneaky\n\nMore.\n")


def test_a_closed_prd_cannot_be_rebaselined(db, approved):
    prd_svc.close_prd(db, approved, dispositions=_defer_all(db, approved), closed_by="user:1")

    with pytest.raises(prd_svc.PrdClosed, match="successor"):
        prd_svc.request_rebaseline(db, approved, reason_type="learning", reason="Oops.",
                                   requested_by="agent:t")


def test_a_closed_prd_cannot_be_closed_twice(db, approved):
    prd_svc.close_prd(db, approved, dispositions=_defer_all(db, approved), closed_by="user:1")

    with pytest.raises(prd_svc.CloseRefused, match="already closed"):
        prd_svc.close_prd(db, approved, dispositions=[], closed_by="user:1")


def test_status_derivation_cannot_reopen_a_closed_prd(db, approved):
    """The quiet one. `sync_status` runs after every classification, and if it recomputed
    a closed PRD the state would reopen the moment a linked item changed."""
    prd_svc.close_prd(db, approved, dispositions=_defer_all(db, approved), closed_by="user:1")

    prd_svc.sync_status(db, approved)
    db.refresh(approved)

    assert approved.status == "closed"


def test_closed_cannot_be_set_by_hand(db, approved):
    """Reached through `close_prd`, never set — otherwise the disposition gate is one
    field assignment away from being skipped entirely."""
    with pytest.raises(prd_svc.PrdClosed, match="reached by closing"):
        prd_svc.update_prd(db, approved.id, status="closed")


# ---- readiness ------------------------------------------------------------------------------
def test_a_prd_with_no_baseline_cannot_close(db):
    prd = prd_svc.create_prd(db, title="Draft", project_id="core", body=BODY)

    with pytest.raises(prd_svc.CloseRefused, match="no agreed intent"):
        prd_svc.close_prd(db, prd, dispositions=[], closed_by="user:1")


def test_a_configured_judge_that_is_not_answering_blocks_the_close(db, approved, monkeypatch):
    """GRPH-311, through the close verb: that close is merely LATE, and closing
    mechanically now would silently downgrade a verdict someone is expecting."""
    from app.services import platform as platform_svc

    real = platform_svc.resolve_chat
    monkeypatch.setattr(platform_svc, "resolve_chat", lambda db, pid: Resolved("openai", real(db, pid).chat))

    with pytest.raises(prd_svc.CloseRefused, match="not answering"):
        prd_svc.close_prd(db, approved, dispositions=_defer_all(db, approved),
                          closed_by="user:1", judge_reachable=False)


def test_closing_is_reachable_over_the_api(client, auth, db, approved):
    out = client.post(f"/api/prds/{approved.id}/close",
                      json={"dispositions": _defer_all(db, approved)}, headers=auth)
    assert out.status_code == 200 and out.json()["mode"] == "mechanical"

    again = client.patch(f"/api/prds/{approved.id}", json={"title": "Renamed"}, headers=auth)
    assert again.status_code == 409
