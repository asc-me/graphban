"""A second project, populated, that must never appear in a `core` read (GRPH-436).

A suite that seeds exactly one project cannot tell "scoped to this project" from "everything
in the instance", because with one project the two sets are equal. So the WHERE clause that
provides tenant isolation is invisible to the tests that look like they cover it. Measured on
2026-08-21, both found by sabotage during review and both green afterwards:

- `shell_counts` — deleting `.where(model.project_id == project_id)` left all 5 tests in
  `test_shell_counts.py` passing (GRPH-431).
- `held_areas` — swapping `active_reservations(db, project_id, …)` for
  `active_reservations(db, None, …)` left all 20 tests in `test_fleet_presence.py` passing
  (GRPH-387).

Both were correct; neither was defended. Hosted mode is where a second tenant stops being
exotic, so this is the fixture those suites were missing rather than an extra assertion — you
cannot assert your way out of having nothing to differ from.

WHAT A CALLER MUST DO WITH THE RETURN VALUE. `seed_decoy` hands back a manifest of what it
created, and a scoping test should assert against it before asserting scope. An empty decoy
makes every downstream assertion pass for the wrong reason — the exact failure this file
exists to prevent, reappearing inside the tool built to prevent it. `assert_populated` is the
one-line version.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

DECOY = "decoy"


def seed_decoy(db, *, items: int = 4, in_progress: int = 2, requests: int = 3,
               candidates: int = 5, reservations: int = 2) -> dict:
    """Create a populated second project and return a manifest of what is in it.

    The id is whatever `create_project` mints, and callers read it off the manifest. An
    earlier draft forced a memorable one by rewriting the primary key after the insert, which
    silently orphans the owner's membership row — its FK still points at the old id, so the
    decoy would have had no members and `require_readable` would have refused it for reasons
    that had nothing to do with the test.

    The default quantities deliberately differ from what the `core` fixtures seed. Equal
    counts would let a leak survive a `==` assertion: three of ours plus three of theirs is
    not six only when the numbers happen to match, and that is a coincidence to design out
    rather than to rely on.
    """
    from sqlalchemy import select

    from app.models import ApiKey, AreaReservation, User
    from app.services import fleet as fleet_svc
    from app.services import items as items_svc
    from app.services import memory as mem_svc
    from app.services import projects as proj_svc
    from app.services import requests as req_svc

    owner = db.scalars(select(User)).first()
    assert owner is not None, "the seeded dataset always has a user; without one there is no owner"

    project_id = proj_svc.create_project(
        db, name="Decoy", owner_user_id=owner.id, tag="DEC").id
    db.commit()

    item_ids = []
    for i in range(items):
        it = items_svc.create_item(db, title=f"decoy item {i}", project_id=project_id)
        if i < in_progress:
            items_svc.update_item(db, it.id, status="in_progress")
        item_ids.append(it.id)
    for i in range(requests):
        req_svc.create_request(db, type_="feedback", title=f"decoy req {i}",
                               project_id=project_id)
    for i in range(candidates):
        mem_svc.add_memory(db, text_body=f"decoy cand {i}", project_id=project_id,
                           status="candidate")

    # A REAL agent on a real key, for the same reason `test_fleet_presence` insists on one:
    # Postgres enforces the foreign key that SQLite does not, and the holder join reads
    # Agent -> ApiKey -> User, so an invented id would test the join against nothing.
    key = db.scalars(select(ApiKey)).first()
    agent = fleet_svc.register_agent(db, project_id=project_id, api_key=key,
                                     label="decoy agent")
    db.commit()

    areas = []
    for i in range(reservations):
        area = f"backend/app/services/decoy_{i}.py"
        db.add(AreaReservation(
            agent_id=agent.id, item_id=item_ids[i % len(item_ids)], area=area,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=600),
        ))
        areas.append(area)
    db.commit()

    return {
        "project_id": project_id,
        "items": items,
        "items_in_progress": in_progress,
        "requests": requests,
        "candidates": candidates,
        "item_ids": item_ids,
        "agent_id": agent.id,
        "areas": areas,
    }


def assert_populated(manifest: dict) -> None:
    """The control every scoping assertion depends on.

    Call it before asserting that a `core` read excludes the decoy: if the decoy is empty,
    "excluded" and "never existed" are the same observation, and the test passes without
    being able to fail.
    """
    assert manifest["items"] > 0, "no decoy items — a scoping assertion cannot fail"
    assert manifest["requests"] > 0, "no decoy requests — a scoping assertion cannot fail"
    assert manifest["candidates"] > 0, "no decoy shards — a scoping assertion cannot fail"
    assert manifest["areas"], "no decoy reservations — a scoping assertion cannot fail"
