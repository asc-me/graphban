"""An item knows when the section it came from has moved (GRPH-360).

`decompose_prd` copies a section's markdown into the item it creates. That copy was a
point-in-time snapshot with nothing linking it back to the source text, so editing the PRD
afterwards left the item holding the old rules forever — silently.

**Found live on PRD-17: nine of eleven items had drifted from the approved body.** One said
the gate returns `403`; the approved PRD says an `unauthorized` tool error, because MCP has
no HTTP status to return. An agent claiming that item builds the wrong contract. Another
carried nine acceptance steps against the PRD's twelve, missing the role-laundering attack
the grill was run to find — it would have passed a build that never tested the hole.

`prd_coverage` read **100% covered** throughout, because it matches items to sections by
NAME: it measures existence, not agreement.

Two rules shape every test here:

* **Nothing is ever rewritten.** An item is legitimately edited away from its section —
  narrowed after a spike, annotated with findings. Auto-syncing would destroy that.
* **`unknown` is an answer, not a fallback.** An item that cannot be checked and an item
  that has been checked and agrees are different facts. Collapsing them is exactly how this
  stayed invisible.
"""
from app.db import SessionLocal
from app.models import Item
from app.services import items as items_svc
from app.services import prds as prd_svc

BODY = ("# Sync Spec\n\n"
        "## Ingest\n\nread the feed\n\n"
        "## Transform\n\nnormalize\n")


def _prd(client, auth, body=BODY, title="Sync Spec"):
    return client.post("/api/prds", json={"title": title, "body": body}, headers=auth).json()


def _decompose(client, auth, prd):
    from tests.prd_approve import approve_id
    approve_id(prd["id"])
    return client.post(f"/api/prds/{prd['id']}/decompose?create=true", headers=auth).json()


def _edit(client, auth, prd, section, text):
    db = SessionLocal()
    try:
        row = db.get(prd_svc.Prd, prd["id"])
        prd_svc.update_prd(db, prd["id"], body=prd_svc.replace_section(row.body, section, text))
    finally:
        db.close()


def _drift(client, auth, prd):
    """Every item's state, from BOTH places coverage reports them — per section, and the
    orphans whose section the PRD no longer has. A caller reading only `sections` sees a
    renamed-away item as simply absent, which is the failure this is about."""
    cov = client.get(f"/api/prds/{prd['id']}/coverage", headers=auth).json()
    states = {d["id"]: d["state"] for s in cov["sections"] for d in s["drift"]}
    states.update({d["id"]: d["state"] for d in cov["orphaned"]})
    return states, cov


class _Snap(dict):
    """Item fields read INSIDE the session and frozen.

    `Item.key` renders from the project's current tag (PRD-13), so touching it on a detached
    instance lazy-loads `project` and raises. Returning a snapshot rather than the row keeps
    every assertion below about values instead of about session lifetime.
    """
    __getattr__ = dict.__getitem__


def _item(item_id):
    db = SessionLocal()
    try:
        it = db.get(Item, item_id)
        return _Snap(key=it.key, prd_section=it.prd_section, description=it.description,
                     prd_section_hash=it.prd_section_hash, prd_section_ack=it.prd_section_ack)
    finally:
        db.close()


# ── the fingerprint ───────────────────────────────────────────────────────────

def test_decompose_records_what_the_section_said(client, auth):
    prd = _prd(client, auth)
    made = _decompose(client, auth, prd)
    for iid in made["created"]:
        it = _item(iid)
        assert it.prd_section_hash, f"{it.prd_section} was created with no fingerprint"
        assert it.prd_section_hash == prd_svc.section_fingerprint(BODY, it.prd_section)


def test_an_untouched_section_agrees(client, auth):
    prd = _prd(client, auth)
    _decompose(client, auth, prd)
    states, _ = _drift(client, auth, prd)
    assert set(states.values()) == {"agrees"}, states


# ── the failure it exists for ─────────────────────────────────────────────────

def test_editing_a_section_flags_its_items_and_only_its_items(client, auth):
    """The PRD-17 case. `prd_coverage` said 100% while the items contradicted the spec; the
    'only its items' half matters just as much, because a report that flags everything on
    any edit is one people turn off."""
    prd = _prd(client, auth)
    made = _decompose(client, auth, prd)
    by_section = {_item(i).prd_section: _item(i).key for i in made["created"]}

    _edit(client, auth, prd, "Ingest", "read the feed, then checkpoint the offset")

    states, cov = _drift(client, auth, prd)
    assert states[by_section["Ingest"]] == "drifted"
    assert states[by_section["Transform"]] == "agrees"
    assert cov["drift_counts"]["drifted"] == 1


def test_the_item_is_never_rewritten(client, auth):
    """Detection, not synchronisation. Overwriting the description from the PRD would
    destroy a narrowing somebody did by hand — the same class of error as re-asking a
    question that has already been answered."""
    prd = _prd(client, auth)
    made = _decompose(client, auth, prd)
    before = _item(made["created"][0]).description

    _edit(client, auth, prd, "Ingest", "completely different rules now")
    _drift(client, auth, prd)  # reading the report must not mutate anything

    assert _item(made["created"][0]).description == before


def test_editing_the_ITEM_does_not_flag_it(client, auth):
    """This reports SOURCE drift. An item edited by hand while its section stands still is
    doing exactly what items are for, and flagging it would drown the real signal."""
    prd = _prd(client, auth)
    made = _decompose(client, auth, prd)
    db = SessionLocal()
    try:
        items_svc.update_item(db, made["created"][0],
                              description="narrowed after the spike: offsets only")
    finally:
        db.close()
    states, _ = _drift(client, auth, prd)
    assert set(states.values()) == {"agrees"}, states


# ── the two answers that are not about drift ──────────────────────────────────

def test_an_item_with_no_fingerprint_is_unknown_not_clean(client, auth):
    """THE guard. Every item predating this column, and every item linked by hand, has no
    fingerprint. Reporting those as `agrees` would reproduce the original defect precisely:
    a check that cannot tell, reporting nothing, and reading as fine."""
    prd = _prd(client, auth)
    client.post("/api/items", json={"title": "linked by hand", "prd_id": prd["id"],
                                    "prd_section": "Ingest"}, headers=auth)
    states, cov = _drift(client, auth, prd)
    assert set(states.values()) == {"unknown"}, states
    assert cov["drift_counts"]["unknown"] == 1
    assert cov["drift_counts"]["agrees"] == 0


def test_a_renamed_section_is_reported_as_gone_not_drifted(client, auth):
    """A hash comparison would call this 'drifted', which is true and useless — nobody can
    diff against text that is not there. The remedy is relinking, not re-reading."""
    prd = _prd(client, auth)
    _decompose(client, auth, prd)
    db = SessionLocal()
    try:
        row = db.get(prd_svc.Prd, prd["id"])
        prd_svc.update_prd(db, prd["id"],
                           body=row.body.replace("## Ingest", "## Ingestion"))
    finally:
        db.close()
    states, cov = _drift(client, auth, prd)
    assert "section_gone" in states.values(), states
    # And it is REPORTED, not merely computable: iterating the PRD's own headings skips an
    # item whose heading is gone, so before `orphaned` existed this state was unreachable
    # through the only surface that shows it.
    assert [o["section"] for o in cov["orphaned"]] == ["Ingest"]
    assert cov["drift_counts"]["section_gone"] == 1


# ── acknowledging ─────────────────────────────────────────────────────────────

def test_acknowledging_stops_the_flag_without_changing_either_text(client, auth):
    prd = _prd(client, auth)
    made = _decompose(client, auth, prd)
    iid = next(i for i in made["created"] if _item(i).prd_section == "Ingest")
    _edit(client, auth, prd, "Ingest", "read the feed, then checkpoint the offset")
    desc_before = _item(iid).description

    db = SessionLocal()
    try:
        items_svc.update_item(db, iid, ack_section_drift=True)
    finally:
        db.close()

    states, _ = _drift(client, auth, prd)
    assert states[_item(iid).key] == "acknowledged"
    assert _item(iid).description == desc_before, "acknowledging must not rewrite the item"


def test_a_later_edit_flags_again(client, auth):
    """The difference between "I have read this change" and "stop telling me about this
    section". An acknowledgement that silenced the section forever would hide the next
    contradiction, which is the one that gets built wrong."""
    prd = _prd(client, auth)
    made = _decompose(client, auth, prd)
    iid = next(i for i in made["created"] if _item(i).prd_section == "Ingest")
    _edit(client, auth, prd, "Ingest", "first change")
    db = SessionLocal()
    try:
        items_svc.update_item(db, iid, ack_section_drift=True)
    finally:
        db.close()
    assert _drift(client, auth, prd)[0][_item(iid).key] == "acknowledged"

    _edit(client, auth, prd, "Ingest", "second, different change")
    assert _drift(client, auth, prd)[0][_item(iid).key] == "drifted"


def test_acknowledging_an_unlinked_item_is_refused(client, auth):
    """It cannot mean anything, and silently succeeding would leave a caller believing it
    had reviewed something."""
    import pytest

    made = client.post("/api/items", json={"title": "no prd"}, headers=auth).json()
    db = SessionLocal()
    try:
        with pytest.raises(ValueError):
            items_svc.update_item(db, made["id"], ack_section_drift=True)
    finally:
        db.close()


# ── the parser the fingerprint depends on ─────────────────────────────────────

def test_section_bodies_agrees_with_the_splitter_about_fences(client):
    """`section_bodies` was a THIRD independent section parser, matching `^##` line by line
    with no idea what a code fence is, while `replace_section` had been made fence-aware in
    GRPH-357. That is load-bearing here: the fingerprint hashes what decompose copied, so a
    copier and a checker that disagree about where a section ends would mark every item on
    such a PRD as drifted the moment it was created."""
    fenced = "## A\n\nsee below\n\n```markdown\n## Not a heading\n```\n\n## B\n\nreal\n"
    assert list(prd_svc.section_bodies(fenced)) == prd_svc.parse_sections(fenced) == ["A", "B"]
    assert "## Not a heading" in prd_svc.section_bodies(fenced)["A"]
