"""One request for the switcher's per-project numbers (GRPH-1131 / PRD-P59 D3).

Accept: `GET /api/projects/summaries` returns, for every project the caller can read and
none it cannot, `item_count`, `agent_count`, `default_branch`, `live_agents` and
`last_activity_at` — from grouped aggregates whose statement count does not grow with the
item count.

What was wrong before: the switcher had no source for these numbers. `ProjectOut` carries 25
configuration fields and no counts, so drawing `24 items · 3 agents · main` meant fetching
each project's collections and calling `len()` — the shape GRPH-431 removed from the nav
badges after one `/prds` load moved 2.1 MB (765 KB of items, 740 KB of memory shards,
621 KB of candidates) to render three integers, with nginx logging three "upstream response
is buffered to a temporary file" warnings for a single view of it.

The failure modes pinned here are the ones an aggregate read makes easy to get wrong in the
REASSURING direction: a scope that quietly widens to the instance; a null a renderer reads
as a number (`last_activity_at` as 0 is "activity in 1970", `default_branch` as `main` is a
branch nobody set); and a count that agrees with nothing, which is worse than the slow
collection it replaced because the switcher is always on screen.
"""
import datetime as dt

import pytest
from sqlalchemy import select

from app.services import projects as proj_svc

from tests.decoy import assert_populated

COUNT_FIELDS = ("item_count", "agent_count", "live_agents")


@pytest.fixture()
def db(client):
    """Depends on `client` so the app has started and the database is reset — the same shape
    every other suite here uses."""
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _login(client, email):
    r = client.post("/api/auth/login", json={"email": email, "password": "graphban"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _summaries(client, headers):
    """`(rows, by_id)`, so a test can ask about one row without caring what else the seeded
    dataset put in the list."""
    r = client.get("/api/projects/summaries", headers=headers)
    assert r.status_code == 200, r.text
    rows = r.json()
    assert isinstance(rows, list), rows
    by_id = {row["project_id"]: row for row in rows}
    assert len(by_id) == len(rows), "a project appeared twice in one switcher read"
    return rows, by_id


def _register(client, db, *, email="outsider@ascme-labs.com", handle="outsider"):
    """A caller with NO memberships — the second tenant.

    Every seeded user holds one on `core` (u2 has `core="write"`, u3 `core="read"`), so none
    of them can prove a scope actually narrowed: their readable set is non-empty either way.
    """
    from app.models import User

    r = client.post("/api/auth/register", json={
        "name": "Out Sider", "email": email, "handle": handle, "password": "s3cure-pass"})
    assert r.status_code == 201, r.text
    user = db.scalar(select(User).where(User.email == email))
    assert user is not None, "registration returned a token for a user that does not exist"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, user.id


def _agent(db, project_id="core", label="a1"):
    """A REAL agent row, registered the way the product registers one.

    An invented `agent_id` passes on SQLite, which does not enforce foreign keys by default;
    Postgres does, and CI runs both. The same reason `test_fleet_presence` insists on this.
    """
    from app.models import ApiKey
    from app.services import fleet as fs

    key = db.scalars(select(ApiKey).where(ApiKey.project_id == project_id)).first()
    if key is None:
        key = db.scalars(select(ApiKey)).first()
    agent = fs.register_agent(db, project_id=project_id, api_key=key, label=label)
    db.commit()
    return agent


def _age(db, agent, *, seconds=0, state=None, dismissed=False):
    """Move an agent's presence into the past, or into a verdict the server owns."""
    from app.models import Agent

    row = db.get(Agent, agent.id)
    row.last_seen_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds)
    if state is not None:
        row.state = state
    if dismissed:
        row.dismissed_at = dt.datetime.now(dt.timezone.utc)
    db.commit()
    return row


def _when(raw: str | None) -> dt.datetime | None:
    """Parse a wire timestamp to an aware UTC datetime, whatever the engine stored."""
    if raw is None:
        return None
    parsed = dt.datetime.fromisoformat(raw)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def test_one_request_labels_every_readable_project_with_its_own_numbers(client, auth, db, decoy):
    """Each count must equal `len()` of the collection it labels, on a SECOND populated
    project: with one project, "scoped to this project" and "the whole instance" are the same
    set, so neither reading can fail (GRPH-436)."""
    from app.services import fleet as fleet_svc
    from app.services import items as items_svc

    assert_populated(db, decoy)
    did = decoy["project_id"]

    rows, by_id = _summaries(client, auth)
    assert {"core", did} <= set(by_id), rows

    for pid in ("core", did):
        row = by_id[pid]
        assert row["item_count"] == len(items_svc.list_items(db, project_id=pid)), pid
        # The roster population `live.board` counts as `total_agents`: every registered agent
        # on the project that has not been dismissed. Offline ones stay IN — the switcher says
        # how many exist and, separately, how many are here.
        roster = [a for a in fleet_svc.list_agents(db, pid) if not a.get("dismissed")]
        assert row["agent_count"] == len(roster), pid
        assert row["live_agents"] <= row["agent_count"], pid

    # Neither row is the instance total. Dropping the `project_id` predicate from an aggregate
    # would make both equal it, and the two comparisons above are what catch that.
    assert by_id["core"]["item_count"] >= decoy["items"] >= 0
    assert by_id["core"]["item_count"] != by_id[did]["item_count"]


def test_a_caller_with_no_projects_gets_an_empty_list_not_the_instance(client, auth, db):
    """Zero readable projects is a real state — a new user on a hosted box — and an empty list
    is the honest answer. It must not fall back to every project on the instance, which is
    what an unscoped aggregate returns."""
    from app.security import authz

    headers, uid = _register(client, db)
    assert authz.readable_project_ids(db, uid) == [], "the fixture is not actually an outsider"
    assert proj_svc.project_summaries(db, []) == []

    r = client.get("/api/projects/summaries", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == [], "a caller with no memberships was handed somebody's projects"

    # Control: the instance is NOT empty, so `[]` above is the scope talking rather than an
    # empty database making every assertion pass for nothing.
    assert "core" in _summaries(client, auth)[1]


def test_a_second_tenant_is_neither_listed_nor_counted(client, auth, db):
    """The refusal half. A project the caller cannot read must not appear, and its rows must
    not join the counts of one that can."""
    from app.services import items as items_svc

    headers, uid = _register(client, db)
    theirs = proj_svc.create_project(db, name="Outsider only", owner_user_id=uid, tag="OUTS").id
    for i in range(5):
        items_svc.create_item(db, title=f"their item {i}", project_id=theirs)
    _agent(db, project_id=theirs, label="their agent")
    db.commit()

    # Control: the other tenant's project really is populated. Without this, "excluded" and
    # "never existed" are the same observation.
    assert len(items_svc.list_items(db, project_id=theirs)) == 5

    rows, hers = _summaries(client, headers)
    assert [r["project_id"] for r in rows] == [theirs], rows
    assert hers[theirs]["item_count"] == 5
    assert hers[theirs]["agent_count"] == 1

    _, mine = _summaries(client, auth)
    assert theirs not in mine, "a project the caller cannot read appeared in the switcher"
    core_before = {k: v for k, v in mine["core"].items() if k in COUNT_FIELDS}
    assert core_before["item_count"] > 0, "core has no items, so a leak into it is invisible"
    _, mine_again = _summaries(client, auth)
    assert {k: mine_again["core"][k] for k in COUNT_FIELDS} == core_before


def test_an_empty_project_reports_null_and_not_a_number(client, auth, db):
    """`last_activity_at: null`, not 0; `default_branch: null`, not `main`.

    Created through the SERVICE so nothing writes a ledger row: a project nobody has done
    anything in is the state the switcher renders as `idle`, and both a zero timestamp and a
    guessed branch would render as an answer instead.
    """
    pid = proj_svc.create_project(db, name="Fresh", owner_user_id="u1", tag="FRSH").id

    r = client.get("/api/projects/summaries", headers=auth)
    assert r.status_code == 200, r.text
    row = next(row for row in r.json() if row["project_id"] == pid)

    assert row["item_count"] == 0
    assert row["agent_count"] == 0
    assert row["live_agents"] == 0
    # On the wire, not on the service dict: a serialiser that turned None into 0 or "" would
    # satisfy a Python-side comparison and still mislead the renderer.
    assert row["last_activity_at"] is None, row
    assert row["default_branch"] is None, row
    assert '"last_activity_at":null' in r.text.replace(" ", ""), r.text
    assert '"default_branch":null' in r.text.replace(" ", ""), r.text
    assert row["project_id"] in r.text


def test_default_branch_is_the_gitops_overlay_and_unset_never_becomes_main(client, auth, db):
    """Project overlay, then org, then null — gitops' precedence, borrowed not restated.

    The switcher and `GET /projects/{id}/gitops` answer the same question, so a project whose
    branch comes from its org's house process must show it here too; and unset must stay
    unset, because "use main" is the reading gitops exists to stop.
    """
    from app.models import Organization, Project
    from app.services import gitops

    pid = proj_svc.create_project(db, name="Branched", owner_user_id="u1", tag="BRAN").id

    def branch():
        return _summaries(client, auth)[1][pid]["default_branch"]

    assert branch() is None, "an unset branch invented one"

    project = db.get(Project, pid)
    project.gitops_base_branch = "trunk"
    db.commit()
    assert branch() == "trunk"
    assert branch() == gitops.resolve_local(db, pid).fields.base_branch.value

    # The org level, and the project's precedence over it.
    org = Organization(id="org-summaries", name="Summaries org")
    org.gitops_base_branch = "release"
    db.add(org)
    project = db.get(Project, pid)
    project.gitops_base_branch = None
    project.org_id = org.id
    db.commit()
    assert branch() == "release", "the org's house branch did not reach the switcher"

    project = db.get(Project, pid)
    project.gitops_base_branch = "trunk"
    db.commit()
    assert branch() == "trunk", "the project overlay lost to the org"
    assert branch() == gitops.resolve_local(db, pid).fields.base_branch.value


def test_live_agents_are_fresh_heartbeats_and_not_the_whole_roster(client, auth, db):
    """`2 live` beside `3 agents` is the point of the row: a roster count that included dead
    agents would read as capacity, and a live count that included a quarantined one would
    report an agent the server is refusing as available."""
    from app.services import fleet as fleet_svc

    pid = proj_svc.create_project(db, name="Fleeted", owner_user_id="u1", tag="FLET").id
    before = _summaries(client, auth)[1][pid]
    ttl = fleet_svc.presence_ttl_seconds()

    _agent(db, project_id=pid, label="fresh")
    stale = _agent(db, project_id=pid, label="stale")
    _age(db, stale, seconds=ttl * 4)
    quarantined = _agent(db, project_id=pid, label="quarantined")
    _age(db, quarantined, state="quarantined")
    dismissed = _agent(db, project_id=pid, label="dismissed")
    _age(db, dismissed, dismissed=True)

    row = _summaries(client, auth)[1][pid]
    # The dismissed agent is out of the roster entirely; the stale and quarantined ones are
    # still registered, they are simply not here.
    assert row["agent_count"] == before["agent_count"] + 3, row
    assert row["live_agents"] == before["live_agents"] + 1, row
    # And the roster agrees with the fleet's own, which is the number the Live page shows.
    assert row["agent_count"] == len(
        [a for a in fleet_svc.list_agents(db, pid) if not a.get("dismissed")])

    # The clock is the fleet's, not a constant of this module's own: an agent just past the
    # TTL is offline everywhere or nowhere.
    fresh = _agent(db, project_id=pid, label="fresh-then-gone")
    assert _summaries(client, auth)[1][pid]["live_agents"] == row["live_agents"] + 1
    _age(db, fresh, seconds=ttl + 60)
    assert _summaries(client, auth)[1][pid]["live_agents"] == row["live_agents"]


def test_last_activity_is_the_newest_of_the_items_and_the_ledger(client, auth, db):
    """Both sources, because either one alone under-reports and `idle` is the reassuring
    wrong answer. An item moved a fortnight ago through a path that wrote no ledger row, and
    a mutation that touched no item, are both activity."""
    from app.models import Event, Item
    from app.services import items as items_svc

    pid = proj_svc.create_project(db, name="Activity", owner_user_id="u1", tag="ACTV").id

    def last():
        return _when(_summaries(client, auth)[1][pid]["last_activity_at"])

    item = items_svc.create_item(db, title="moved a fortnight ago", project_id=pid)
    fortnight = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=12)
    row = db.get(Item, item.id)
    row.updated_at = fortnight
    db.commit()

    seen = last()
    assert seen is not None, "a project with a moved item reported no activity"
    assert abs(seen - fortnight) < dt.timedelta(minutes=5), seen

    # An OLDER ledger row must not win: this is a max over both sources, not "whatever the
    # audit log last said".
    db.add(Event(ts=fortnight - dt.timedelta(days=3), actor_type="system", actor_id="",
                 surface="mcp", action="create_item", project_id=pid))
    db.commit()
    assert last() == seen, "an older ledger row moved the project's last activity"

    now = dt.datetime.now(dt.timezone.utc)
    db.add(Event(ts=now, actor_type="system", actor_id="", surface="mcp",
                 action="update_item", project_id=pid))
    db.commit()
    assert last() > seen
    assert abs(last() - now) < dt.timedelta(minutes=5)


def test_the_statement_count_does_not_grow_with_the_item_count(db):
    """The budget, and the reason this is a service read rather than a loop over collections.

    Sabotage: replace any grouped aggregate with a fetch-and-`len()` and this fails, while
    every assertion about the NUMBERS above stays green.
    """
    from sqlalchemy import event as sa_event

    from app.db import engine
    from app.services import items as items_svc

    def measure():
        seen: list[str] = []

        def hook(conn, cursor, statement, parameters, context, executemany):
            seen.append(statement)

        sa_event.listen(engine, "before_cursor_execute", hook)
        try:
            proj_svc.project_summaries(db, ["core"])
        finally:
            sa_event.remove(engine, "before_cursor_execute", hook)
        return seen

    # Warm the session first. A Project already in the identity map is not selected again, so
    # the first call of a fresh session issues statements the second does not — which would
    # look like the count shrinking.
    proj_svc.project_summaries(db, ["core"])
    before = measure()
    for i in range(8):
        items_svc.create_item(db, title=f"more {i}", project_id="core")
        _agent(db, project_id="core", label=f"more-{i}")
    after = measure()

    assert len(after) == len(before), (
        f"statement count grew from {len(before)} to {len(after)} after 8 more items and 8 "
        "more agents:\n" + "\n".join(after)
    )
    grouped = [s for s in after if "group by" in s.lower()]
    assert len(grouped) >= 3, after
    assert any("count(" in s.lower() and "items" in s.lower() for s in grouped), after
    assert any("count(" in s.lower() and "agents" in s.lower() for s in grouped), after
    assert any("max(" in s.lower() and "events" in s.lower() for s in grouped), after
