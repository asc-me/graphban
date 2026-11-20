"""A blocking fix reaching done returns the item it blocks to next (GRPH-1772).

Filing the fix releases the holder to `blocked` with the blocker text. When the
last such fix reaches `done` — through `update_item`, and through `sign_off`,
which writes `done` itself — that item goes back through `unblock`. A wait or a
manual blocker is left alone, and a second open blocking fix keeps the block.
"""
import ast
import inspect

from app.db import SessionLocal
from app.services import items as items_svc
from app.services import links as links_svc
from app.services import prds as prd_svc
from tests import attest

ACC = "Does it.\n\n## Acceptance\n- the fix works\n"


def _setup(db, holder="A-1"):
    prd = prd_svc.create_prd(db, title="Spec", project_id="core",
                             body="# S\n\n## Build\n\nBuild it.\n")
    feat = items_svc.create_item(db, title="feat", project_id="core", kind="feature",
                                 prd_id=prd.id, prd_section="Build")
    feat.status, feat.claimed_by = "in_progress", holder
    db.commit()
    return prd, feat


def _file(db, prd, feat, held, touchpoints):
    return items_svc.create_item(
        db, title="bug", project_id="core", kind="fix", prd_id=prd.id,
        description=ACC, agent_create=True, touchpoints=touchpoints,
        blocks=feat.key, held=held)


def test_one_blocking_fix_returns_the_item_to_next(client):
    """Removing the `unblock` call leaves this item `blocked`."""
    with SessionLocal() as db:
        prd, feat = _setup(db)
        fix = _file(db, prd, feat, [feat], ["a/x.py"])
        db.refresh(feat)
        assert feat.status == "blocked" and fix.key in feat.blocker

        attest.complete(db, fix.id)
        db.refresh(feat)
        assert feat.status == "next"
        assert feat.blocker == ""


def test_two_blocking_fixes_stay_blocked_until_both_are_done(client):
    with SessionLocal() as db:
        prd, feat = _setup(db, "A-2")
        first = _file(db, prd, feat, [feat], ["a/x.py"])
        db.refresh(feat)
        assert feat.status == "blocked"
        second = _file(db, prd, feat, [], ["b/y.py"])
        db.refresh(feat)
        assert feat.status == "blocked" and first.key in feat.blocker

        attest.complete(db, first.id)
        db.refresh(feat)
        assert feat.status == "blocked", "the second blocking fix was ignored"
        assert feat.blocker

        attest.complete(db, second.id)
        db.refresh(feat)
        assert feat.status == "next"
        assert feat.blocker == ""


def test_a_wait_or_a_manual_blocker_stays_blocked_when_an_unrelated_fix_is_done(client):
    with SessionLocal() as db:
        # A person's note, and a dependency that is not a blocking fix.
        manual = items_svc.create_item(db, title="stuck", project_id="core", status="blocked")
        items_svc.update_item(db, manual.id, blocker="needs a person")
        unrelated = items_svc.create_item(db, title="other work", project_id="core")
        links_svc.create_link(db, a=manual.id, b=unrelated.id, type_="dependency",
                              project_id="core", reason="see also")

        # A blocking-fix link whose blocker a person rewrote. The link alone is not the block.
        prd, noted = _setup(db, "A-3")
        noted_fix = _file(db, prd, noted, [noted], ["c/z.py"])
        items_svc.update_item(db, noted.id, blocker="needs a person")

        # Still carrying the filing's blocker text, and also waiting on a person.
        prd2, held = _setup(db, "A-4")
        held_fix = _file(db, prd2, held, [held], ["d/w.py"])
        wait = items_svc.create_item(db, title="needs a decision", project_id="core",
                                     status="blocked", tags=["wait:decision"])
        links_svc.create_link(db, a=held.id, b=wait.id, type_="dependency",
                              project_id="core", reason="wait:decision")
        db.refresh(held)
        assert held.blocker.endswith("blocking fix filed on this item")

        attest.complete(db, unrelated.id)
        attest.complete(db, noted_fix.id)
        attest.complete(db, held_fix.id)
        db.refresh(manual)
        db.refresh(noted)
        db.refresh(held)
        assert manual.status == "blocked" and manual.blocker == "needs a person"
        assert noted.status == "blocked" and noted.blocker == "needs a person"
        assert held.status == "blocked" and "blocking fix filed on this item" in held.blocker


def test_sign_off_calls_the_same_release():
    """`sign_off` writes `done` itself. Dropping this call leaves the blocked item
    where the drain found it, and every `update_item` test above stays green."""
    from app.services import fleet as fleet_svc

    tree = ast.parse(inspect.getsource(fleet_svc.sign_off))
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else (
            fn.id if isinstance(fn, ast.Name) else "")
        if name == "release_when_blocking_fix_done":
            found = True
    assert found, "sign_off no longer calls release_when_blocking_fix_done"
