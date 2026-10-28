"""The sidebar's Build/Observe badge numbers ride on `/counts` (GRPH-1132, PRD-P59 §D4).

Each badge restates the population of the page it sits on, so each test seeds a project
of its own, puts one row on either side of that page's definition, and puts a matching
row in a second project next door. With one project "count this project" and "count the
instance" are the same number (GRPH-436), so the neighbour is what lets a dropped WHERE
fail here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services import projects as proj_svc


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def pids(db):
    """A fresh project and its neighbour. `core` arrives seeded, so it cannot hold a literal."""
    from sqlalchemy import select

    from app.models import User

    owner = db.scalars(select(User)).first()
    pid = proj_svc.create_project(db, name="Badges", owner_user_id=owner.id, tag="BDG").id
    other = proj_svc.create_project(db, name="Next door", owner_user_id=owner.id, tag="NXT").id
    db.commit()
    return pid, other


@pytest.fixture()
def key(db):
    from sqlalchemy import select

    from app.models import ApiKey, User

    owner = db.scalars(select(User)).first()
    row = ApiKey(id="k_badges", user_id=owner.id, project_id=None, name="badges",
                 prefix="gb_sk_bdg1", hashed_key="x", scopes=["read", "write"], roles=[])
    db.add(row)
    db.commit()
    return row


def _agent(db, key, project_id, *, label="", host=None, seen_ago=0, dismissed=False):
    from app.services import fleet as fleet_svc

    caps = {"vendor": "anthropic"}
    if host is not None:
        caps["host"] = host
    a = fleet_svc.register_agent(db, project_id=project_id, api_key=key, label=label,
                                 capabilities=caps)
    a.last_seen_at = datetime.now(timezone.utc) - timedelta(seconds=seen_ago)
    if dismissed:
        a.dismissed_at = datetime.now(timezone.utc)
    db.commit()
    return a


def _counts(db, pid):
    return proj_svc.shell_counts(db, pid)


def test_no_project_zeroes_every_badge(db, pids, key):
    """Asked bare, every new field is zero, even with rows of each kind on the box."""
    pid, _ = pids
    from app.services import items as items_svc

    items_svc.create_item(db, title="open", project_id=pid)
    _agent(db, key, pid, host="mac")
    counts = proj_svc.shell_counts(db, None)
    for field in proj_svc.BADGE_FIELDS:
        assert counts[field] == 0, field


def test_items_open_is_every_status_but_done(db, pids):
    """The Tracker badge. Each non-done status counts once; `done` does not count."""
    from app.services import items as items_svc

    pid, other = pids
    assert _counts(db, pid)["items_open"] == 0
    for status in ("backlog", "next", "in_progress", "review", "blocked", "blocked"):
        it = items_svc.create_item(db, title=status, project_id=pid)
        if status != "backlog":
            items_svc.update_item(db, it.id, status=status)
    # Three `done`, a multiplicity no other status has, so excluding any other status in
    # its place gives a different number.
    for i in range(3):
        finished = items_svc.create_item(db, title=f"finished {i}", project_id=pid)
        finished.status = "done"  # the attested path to done is not what this counts
    db.commit()
    items_svc.create_item(db, title="next door", project_id=other)

    assert _counts(db, pid)["items_open"] == 6
    assert _counts(db, pid)["items"] == 9, "the plain item total still counts done"


def test_requests_untriaged_counts_only_new_requests(db, pids):
    """The Requests badge. Triaging, linked and next-door requests stay out."""
    from app.services import items as items_svc
    from app.services import requests as req_svc

    pid, other = pids
    req_svc.create_request(db, type_="bug", title="arrived", project_id=pid)
    mid = req_svc.create_request(db, type_="feedback", title="being triaged", project_id=pid)
    mid.status = "triaging"
    db.commit()
    linked = req_svc.create_request(db, type_="bug", title="linked", project_id=pid)
    req_svc.link_request(db, linked.id, items_svc.create_item(db, title="x", project_id=pid).id)
    req_svc.create_request(db, type_="bug", title="next door", project_id=other)

    counts = _counts(db, pid)
    assert counts["requests_untriaged"] == 1
    assert counts["requests"] == 3


def test_code_nodes_counts_this_projects_map(db, pids):
    """The Code graph badge equals the map's own total."""
    from app.models import CodeNode
    from app.services import code_graph as code_svc

    pid, other = pids
    for i, kind in enumerate(("module", "file", "symbol")):
        db.add(CodeNode(id=f"cn_bdg_{i}", project_id=pid, path=f"a/{i}.py", kind=kind))
    db.add(CodeNode(id="cn_nxt_0", project_id=other, path="a/0.py"))
    db.commit()

    assert _counts(db, pid)["code_nodes"] == 3 == code_svc.count_nodes(db, pid)


def test_mcp_tools_is_the_catalog_size(client, auth, db, pids):
    """The MCP Tools badge equals what `/mcp/tools` lists."""
    pid, _ = pids
    r = client.get("/api/mcp/tools", headers=auth)
    assert r.status_code == 200, r.text
    assert _counts(db, pid)["mcp_tools"] == r.json()["live"] == len(r.json()["tools"]) > 0


def test_fleet_agents_excludes_dismissed(db, pids, key):
    """The Fleet badge is the kept roster: offline counts, dismissed does not."""
    pid, other = pids
    _agent(db, key, pid, host="mac")
    _agent(db, key, pid, host="mac", seen_ago=86_400)
    _agent(db, key, pid, host="mac", dismissed=True)
    _agent(db, key, other, host="mac")

    assert _counts(db, pid)["fleet_agents"] == 2


def test_outposts_groups_hosts_the_way_the_page_does(db, pids, key):
    """Capability host first, then the label's `@ host`, else one unspecified bucket."""
    pid, other = pids
    _agent(db, key, pid, host="mac")
    _agent(db, key, pid, label="opus @ mac:wt-2")          # same host, from the label
    _agent(db, key, pid, label="sonnet @ linux-box:wt-1")  # a second host
    _agent(db, key, pid, host="  ")                       # blank -> unspecified
    _agent(db, key, pid, label="no host here")            # unspecified, same bucket
    _agent(db, key, pid, host="gone", dismissed=True)
    _agent(db, key, other, host="elsewhere")

    assert _counts(db, pid)["outposts"] == 3


def test_outpost_host_matches_the_page_rule():
    assert proj_svc.outpost_host("mac ", "x @ other") == "mac"
    assert proj_svc.outpost_host(None, "opus-5 @ macbook:wt-2") == "macbook"
    assert proj_svc.outpost_host("", "") == ""
    assert proj_svc.outpost_host(7, "plain") == ""


def test_live_agents_is_the_roster_not_offline(db, pids, key):
    """The Live badge: seen inside the presence TTL, not dismissed, key not revoked."""
    from app.services import fleet as fleet_svc

    pid, other = pids
    ttl = fleet_svc.presence_ttl_seconds()
    _agent(db, key, pid, host="mac")
    _agent(db, key, pid, host="mac", seen_ago=ttl + 60)
    _agent(db, key, pid, host="mac", dismissed=True)
    _agent(db, key, other, host="mac")
    assert _counts(db, pid)["live_agents"] == 1

    roster = [a for a in fleet_svc.list_agents(db, pid) if not a["dismissed"]]
    assert sum(1 for a in roster if a["state"] != "offline") == 1

    key.revoked = True
    db.commit()
    assert _counts(db, pid)["live_agents"] == 0, "a revoked credential's agent read as live"


def test_badges_are_aggregates_whatever_the_roster_size(db, pids, key):
    """Adding agents must not add statements."""
    from sqlalchemy import event

    from app.db import engine

    pid, _ = pids

    def measure():
        seen: list[str] = []

        def hook(conn, cursor, statement, parameters, context, executemany):
            seen.append(statement)

        event.listen(engine, "before_cursor_execute", hook)
        try:
            proj_svc.badge_counts(db, pid)
        finally:
            event.remove(engine, "before_cursor_execute", hook)
        return seen

    _agent(db, key, pid, host="mac")
    proj_svc.badge_counts(db, pid)
    before = measure()
    for i in range(6):
        _agent(db, key, pid, host=f"h{i}")
    assert len(measure()) == len(before)
