"""Coverage says what it counted over (GRPH-486).

`prd_coverage` joins items to sections over the items carrying this `prd_id`, and nothing
measured what that set omits. **PRD-20 showed both failures at once**: `D8` read *delivered*
while two items implementing it sat in review unclaimed, and `5. Acceptance criteria` read
*undelivered* while three items were literally building AC-18 and AC-19 of it. One document,
over-reporting one section and under-reporting another, from a single cause.

Measured on 2026-08-25: of 221 PRs merged since 2026-08-01, 114 carry a `GRPH-###` and 46 of
those reference a ticket no PRD claims. That figure alone is not a defect — bug fixes, ops and
chores legitimately belong to no PRD. The defect is that the gap was **unmeasurable**.

It matters past a number on a page: `prd_acceptance(view=readiness)` answers `can_close` over
the same subset, so a PRD can read closeable because the check cannot see the work rather than
because the work is done.

**Reported, never linked.** "Link every item to a PRD" is the wrong instruction; most items
should not be.
"""
from __future__ import annotations

import pytest

BODY = ("# Living graph\n\n"
        "## D8 — Asking the graph\n\nthe hubs panel and shortest path.\n\n"
        "## 5. Acceptance criteria\n\nAC-18: hubs render. AC-19: shortest path is answerable.\n")


@pytest.fixture()
def prd(client, auth):
    return client.post("/api/prds", json={"title": "Living graph", "body": BODY,
                                          "project_id": "core"}, headers=auth).json()


def _cov(client, auth, prd):
    return client.get(f"/api/prds/{prd['id']}/coverage", headers=auth).json()


def _item(client, auth, title, **extra):
    return client.post("/api/items", json={"title": title, "status": "review", **extra},
                       headers=auth).json()


# ── the denominator ───────────────────────────────────────────────────────────

def test_an_item_naming_the_prd_but_not_linked_is_reported(client, auth, prd):
    _item(client, auth, f"something about {prd['id']} that was never linked")
    out = _cov(client, auth, prd)
    assert [r["title"] for r in out["unlinked"]["names_prd"]] == [
        f"something about {prd['id']} that was never linked"]


def test_an_item_citing_a_slice_of_this_prd_is_reported(client, auth, prd):
    """THE motivating case, and the reason an exact key match was not enough on its own:
    `AC-19: shortest path is answerable over MCP` never names PRD-20 at all. The label comes
    from the PRD's OWN body, so this is grounded in the document rather than guessed."""
    _item(client, auth, "AC-19: shortest path between two nodes is answerable over MCP")
    out = _cov(client, auth, prd)
    assert [r["title"] for r in out["unlinked"]["cites_slice"]] == [
        "AC-19: shortest path between two nodes is answerable over MCP"]


def test_the_two_signals_are_kept_apart(client, auth, prd):
    """They are not equally reliable — one is an exact key, the other a label match — and
    collapsing them would hide which of the two a reader should trust."""
    _item(client, auth, f"names {prd['id']} directly")
    _item(client, auth, "D8 — the other half")
    out = _cov(client, auth, prd)
    assert len(out["unlinked"]["names_prd"]) == 1
    assert len(out["unlinked"]["cites_slice"]) == 1


def test_a_linked_item_is_never_reported_as_unlinked(client, auth, prd):
    """The set this measures is the COMPLEMENT of what coverage counts. An item appearing in
    both would make the denominator double-count the work it exists to reveal."""
    _item(client, auth, "AC-18: hubs render", prd_id=prd["id"], prd_section="D8 — Asking the graph")
    out = _cov(client, auth, prd)
    assert out["unlinked"]["names_prd"] == []
    assert out["unlinked"]["cites_slice"] == []
    assert out["total_items"] == 1


def test_unrelated_work_is_not_swept_in(client, auth, prd):
    """Most items belong to no PRD and that is correct. A report that flagged them all would
    be the same as no report — the 46-of-114 figure is not itself the defect."""
    _item(client, auth, "bump a dependency")
    _item(client, auth, "fix a typo in the readme")
    out = _cov(client, auth, prd)
    assert out["unlinked"]["names_prd"] == []
    assert out["unlinked"]["cites_slice"] == []


# ── the label matcher ─────────────────────────────────────────────────────────

def test_a_label_this_prd_does_not_use_is_not_matched(client, auth, prd):
    """The labels come from THIS PRD's body. An item citing `E3` of some other spec is not
    this PRD's missing work, and claiming it would be a confident wrong answer."""
    _item(client, auth, "E3 — an enrolment slice from a different PRD")
    out = _cov(client, auth, prd)
    assert out["unlinked"]["cites_slice"] == []


def test_a_bare_word_is_not_a_label(client, auth, prd):
    """`D8` requires the digit. Matching a bare letter would light up on ordinary prose, and
    a report that cries wolf is one people stop reading."""
    _item(client, auth, "Deploy the thing, and see the notes")
    out = _cov(client, auth, prd)
    assert out["unlinked"]["cites_slice"] == []


def test_only_the_title_is_searched_for_labels(client, auth, prd):
    """The softer signal is kept narrow on purpose. A description mentioning `AC-18` in
    passing — a reference, a quoted comment — is not a claim to be implementing it."""
    _item(client, auth, "unrelated work", description="see AC-18 for background")
    out = _cov(client, auth, prd)
    assert out["unlinked"]["cites_slice"] == []


def test_the_report_costs_no_extra_query(client, auth, prd):
    """It is drawn from the item list coverage already loaded. A separate scan would put a
    second full-table read on the call that renders a PRD page."""
    from sqlalchemy import event

    from app.db import SessionLocal, engine
    from app.services import prds as prd_svc

    _item(client, auth, "AC-19: something")
    db = SessionLocal()
    try:
        row = db.get(prd_svc.Prd, prd["id"])
        seen: list[str] = []
        hook = lambda c, cur, stmt, *a: seen.append(stmt)  # noqa: E731
        event.listen(engine, "before_cursor_execute", hook)
        try:
            prd_svc.coverage(db, row)
        finally:
            event.remove(engine, "before_cursor_execute", hook)
    finally:
        db.close()
    items_reads = [q for q in seen if "FROM items" in q]
    assert len(items_reads) <= 1, f"{len(items_reads)} reads of items: {items_reads}"


@pytest.mark.parametrize("text,expected", [
    ("D8 — Asking the graph", {"D8"}),
    ("AC-18: hubs render", {"AC-18"}),
    ("E3 and S7 and G5", {"E3", "S7", "G5"}),
    ("PRD-20 is the parent", {"PRD-20"}),
    # The failures that matter, and they are all "matched something that is just a word":
    ("Deploy the thing", set()),
    ("Section D covers this", set()),
    ("A single G here", set()),
    ("Grand Slam", set()),
])
def test_the_label_matcher_needs_a_digit(text, expected):
    """Asserted on the regex, because the property cannot be reached through a fixture.

    Relaxing `[DEGS]-?\\d{1,2}` to `\\d{0,2}` — letting a bare `D` count — survived the
    sabotage pass end-to-end, and not because the tests were weak: a bare letter only
    produces a false match when the PRD's OWN body also yields one, which the fixture does
    not. The guard belongs where the rule lives.
    """
    from app.services.prds import _SLICE_LABEL

    assert {m.group(1).upper() for m in _SLICE_LABEL.finditer(text)} == expected
