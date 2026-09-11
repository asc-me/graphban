"""GRPH-771 — a reviewer holding a claim was reported `idle`, and `reviewing` was unreachable.

Observed on the deployed instance: GRPH-A142 held a review claim on GRPH-767 for fifteen
minutes while the roster said `idle` and `last_seen` was 26 seconds old. That read as a
contradiction — a live agent holding work while claiming to have none.

It was not. A review claim is not an item lease: the lease is `claimed_by`, the hold is
`review_claimed_by`. So a reviewer heartbeats WITHOUT an item id, and that branch of
`heartbeat` hardcoded `idle`. `"reviewing"` was in `STATES` and no code path ever set it, so
the roster had one word for "between tasks" and "reviewing right now".

The ticket's own first direction — expire a review hold on AGE — turned out to exist already:
`review_claim_holder` lapses after `DEFAULT_LEASE_SECONDS` whether the holder is alive or not.
The message telling reviewers otherwise is fixed here too.
"""
from __future__ import annotations

import pytest

from app.services import fleet as fleet_svc
from app.services.items import DEFAULT_LEASE_SECONDS


def _mcp(client, key, name, args=None):
    r = client.post("/api/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": args or {}}},
                    headers={"X-API-Key": key})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def _ok(res) -> dict:
    assert not res.get("isError"), res
    return res["structuredContent"]


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "Reviewing"},
                       headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "fleet", "project_id": proj,
                                              "scopes": ["read", "write"]},
                       headers=auth).json()["plaintext"]


@pytest.fixture()
def db(_clean_database):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _agent(client, key, label, role) -> str:
    """These share ONE credential, so each declares a distinct `instance` — the same
    requirement `tests.test_fleet_review._register` states. Before GRPH-848 this was moot:
    `_item_in_review` sends the item without claiming, so it had no author and every reviewer
    was independent of nobody. Now the sender is stamped, and two undeclared agents on one
    key are, correctly, not distinguishable."""
    return _ok(_mcp(client, key, "register_agent",
                    {"label": label, "role_hint": role,
                     "capabilities": {"instance": label}}))["agent_id"]


def _item_in_review(client, key, proj, builder, effort: int = 3) -> str:
    made = _ok(_mcp(client, key, "create_item",
                    {"project_id": proj, "title": "something to review", "effort": effort}))
    _ok(_mcp(client, key, "update_item",
             {"project_id": proj, "id": made["id"], "status": "in_progress",
              "agent_id": builder}))
    _ok(_mcp(client, key, "update_item",
             {"project_id": proj, "id": made["id"], "status": "review",
              "agent_id": builder}))
    return made["id"]


def test_a_reviewer_holding_a_claim_is_reviewing_not_idle(client, key, proj, db):
    """THE DEFECT. Sabotage: hardcode `idle` again and the roster goes back to describing a
    working reviewer and an agent between tasks with the same word."""
    builder = _agent(client, key, "builder", "worker")
    reviewer = _agent(client, key, "reviewer", "worker")
    _item_in_review(client, key, proj, builder)

    claimed = _ok(_mcp(client, key, "claim_review",
                       {"project_id": proj, "agent_id": reviewer}))
    assert claimed.get("claimed"), claimed

    beat = _ok(_mcp(client, key, "heartbeat", {"agent_id": reviewer}))
    assert beat["state"] == "reviewing"


def test_an_agent_holding_nothing_is_still_idle(client, key, proj, db):
    """The other half. "Between tasks" is a real state and must not be dressed up as work."""
    lone = _agent(client, key, "nobody's reviewer", "worker")
    assert _ok(_mcp(client, key, "heartbeat", {"agent_id": lone}))["state"] == "idle"


def test_an_expired_hold_does_not_keep_an_agent_looking_busy(client, key, proj, db):
    """A claim lapses on a clock. An agent still filed as `reviewing` on the strength of a
    dead hold would be the same defect pointing the other way."""
    from datetime import datetime, timedelta, timezone

    from app.models import Item

    builder = _agent(client, key, "builder", "worker")
    reviewer = _agent(client, key, "reviewer", "worker")
    item_id = _item_in_review(client, key, proj, builder)
    _ok(_mcp(client, key, "claim_review", {"project_id": proj, "agent_id": reviewer}))

    row = db.get(Item, item_id)
    row.review_claimed_at = (datetime.now(timezone.utc)
                             - timedelta(seconds=DEFAULT_LEASE_SECONDS + 60))
    db.commit()

    assert _ok(_mcp(client, key, "heartbeat", {"agent_id": reviewer}))["state"] == "idle"


def test_the_queue_says_how_long_a_hold_has_run_and_who_holds_it(client, auth, key, proj, db):
    """A hold renders identically to progress on the board, which is why the deployed
    diagnosis needed a database query. Sabotage: drop the two fields and it needs one again."""
    builder = _agent(client, key, "builder", "worker")
    reviewer = _agent(client, key, "reviewer", "worker")
    _item_in_review(client, key, proj, builder)
    _ok(_mcp(client, key, "claim_review", {"project_id": proj, "agent_id": reviewer}))
    _ok(_mcp(client, key, "heartbeat", {"agent_id": reviewer}))

    queue = client.get(f"/api/fleet?project_id={proj}", headers=auth).json()["review_queue"]
    row = queue[0]
    assert row["reviewed_by"] == reviewer
    assert row["held_for_seconds"] is not None and row["held_for_seconds"] < 60
    assert row["holder_state"] == "reviewing"


def test_an_unheld_item_reports_no_hold_rather_than_zero(client, auth, key, proj, db):
    """Zero seconds held is a duration; nothing holding it is not. Reading the first as the
    second is how "held for 0s" ends up rendered next to an item nobody has looked at."""
    builder = _agent(client, key, "builder", "worker")
    _item_in_review(client, key, proj, builder)

    row = client.get(f"/api/fleet?project_id={proj}",
                     headers=auth).json()["review_queue"][0]
    assert row["reviewed_by"] is None
    assert row["held_for_seconds"] is None
    assert row["holder_state"] is None


def test_the_reason_names_the_clock_not_silence(client, key, proj, db):
    """The ticket's first direction — expire a hold on AGE — already existed; the message
    describing it did not. A reviewer told to wait for the holder to go silent waits for a
    signal that never comes."""
    builder = _agent(client, key, "builder", "worker")
    first = _agent(client, key, "first reviewer", "worker")
    second = _agent(client, key, "second reviewer", "worker")
    _item_in_review(client, key, proj, builder)
    _ok(_mcp(client, key, "claim_review", {"project_id": proj, "agent_id": first}))

    reason = fleet_svc.review_block_reason(db, agent_id=second, project_id=proj)
    assert "silent" not in reason, "it lapses on a clock, held or not"
    assert f"{DEFAULT_LEASE_SECONDS // 60} minutes" in reason


# ---- the deployed verification of the above found the rest of it ------------------------------

def test_re_claiming_what_you_already_hold_does_not_restart_the_clock(client, key, proj, db):
    """MEASURED ON THE DEPLOYED INSTANCE. GRPH-A142 called `claim_review` every 50 seconds and
    nothing else; each call reset `review_claimed_at`, so a 600-second lease never came within
    550 seconds of lapsing. The hold was unbounded — not because the reclaim path "only fires
    on silence", but because the holder kept restarting the clock.

    Sabotage: set the timestamp unconditionally again and a polling loop owns an item forever.
    """
    from datetime import datetime, timedelta, timezone

    from app.models import Item

    builder = _agent(client, key, "builder", "worker")
    reviewer = _agent(client, key, "reviewer", "worker")
    item_id = _item_in_review(client, key, proj, builder)
    _ok(_mcp(client, key, "claim_review", {"project_id": proj, "agent_id": reviewer}))

    # Nine minutes in, still no verdict — and the loop asks again.
    row = db.get(Item, item_id)
    first = datetime.now(timezone.utc) - timedelta(seconds=540)
    row.review_claimed_at = first
    db.commit()

    _ok(_mcp(client, key, "claim_review", {"project_id": proj, "agent_id": reviewer}))
    db.expire_all()
    assert abs((fleet_svc._aware(db.get(Item, item_id).review_claimed_at)
                - first).total_seconds()) < 2, "the poll restarted the lease"


def test_a_hold_lapses_on_its_own_age_and_the_item_is_offered_to_somebody_else(
        client, key, proj, db):
    """The consequence that matters: the item really does go back in the queue. Before, the
    poll restarted the lease, so the moment of availability never arrived at all."""
    from datetime import datetime, timedelta, timezone

    from app.models import Item

    builder = _agent(client, key, "builder", "worker")
    stuck = _agent(client, key, "the loop", "worker")
    other = _agent(client, key, "somebody else", "worker")
    item_id = _item_in_review(client, key, proj, builder)
    _ok(_mcp(client, key, "claim_review", {"project_id": proj, "agent_id": stuck}))

    row = db.get(Item, item_id)
    row.review_claimed_at = datetime.now(timezone.utc) - timedelta(
        seconds=DEFAULT_LEASE_SECONDS + 30)
    db.commit()

    taken = _ok(_mcp(client, key, "claim_review", {"project_id": proj, "agent_id": other}))
    assert taken.get("claimed"), "an expired hold must be offered to another reviewer"


def test_a_loop_that_re_takes_what_it_let_lapse_is_counted(client, auth, key, proj, db):
    """Releasing the item is not the same as NOTICING. A loop that re-takes every time its
    hold lapses looks, on any single read, exactly like a reviewer who started a moment ago —
    which is precisely how this went undiagnosed until somebody watched the number reset.

    Sabotage: stop counting and "taken 7 times, no verdict" cannot be said."""
    from datetime import datetime, timedelta, timezone

    from app.models import Item

    builder = _agent(client, key, "builder", "worker")
    stuck = _agent(client, key, "the loop", "worker")
    item_id = _item_in_review(client, key, proj, builder)

    for _ in range(4):
        _ok(_mcp(client, key, "claim_review", {"project_id": proj, "agent_id": stuck}))
        row = db.get(Item, item_id)
        row.review_claimed_at = datetime.now(timezone.utc) - timedelta(
            seconds=DEFAULT_LEASE_SECONDS + 30)
        db.commit()

    queue = client.get(f"/api/fleet?project_id={proj}", headers=auth).json()["review_queue"]
    assert queue[0]["review_takes"] == 4


def test_polling_a_hold_you_already_have_is_not_a_take(client, auth, key, proj, db):
    """Otherwise the count measures the poll interval, which is the same mistake
    `held_for_seconds` made before the clock was fixed."""
    builder = _agent(client, key, "builder", "worker")
    reviewer = _agent(client, key, "reviewer", "worker")
    _item_in_review(client, key, proj, builder)
    for _ in range(5):
        _ok(_mcp(client, key, "claim_review", {"project_id": proj, "agent_id": reviewer}))

    queue = client.get(f"/api/fleet?project_id={proj}", headers=auth).json()["review_queue"]
    assert queue[0]["review_takes"] == 1


@pytest.mark.parametrize("verdict", ["bounce", "sign_off"])
def test_a_verdict_clears_the_count(client, auth, key, proj, db, verdict):
    """EITHER verdict. The count is the absence of a decision, and both paths are decisions —
    a bounce that comes back for review starts at zero, and so does a signed-off item.

    Parametrised because clearing it in one path and not the other is exactly the shape that
    passes a test written against whichever path the author happened to pick."""
    from app.models import Item

    builder = _agent(client, key, "builder", "worker")
    reviewer = _agent(client, key, "reviewer", "worker")
    # Effort 1: above it a sign-off needs adversarial evidence (the sabotage receipts), which
    # is a different gate and not what this test is about.
    item_id = _item_in_review(client, key, proj, builder, effort=1)
    _ok(_mcp(client, key, "claim_review", {"project_id": proj, "agent_id": reviewer}))

    args = {"project_id": proj, "id": item_id, "agent_id": reviewer}
    if verdict == "bounce":
        args["reason"] = "needs a test"
    res = _mcp(client, key, verdict, args)
    assert not res.get("isError"), res

    db.expire_all()
    assert db.get(Item, item_id).review_takes == 0


def test_a_first_claim_still_starts_the_clock(client, key, proj, db):
    """Not resetting must not become never setting: an item claimed for the first time needs
    a timestamp, or `review_claim_holder` reads it as expired the moment it is taken."""
    from app.models import Item

    builder = _agent(client, key, "builder", "worker")
    reviewer = _agent(client, key, "reviewer", "worker")
    item_id = _item_in_review(client, key, proj, builder)
    _ok(_mcp(client, key, "claim_review", {"project_id": proj, "agent_id": reviewer}))

    db.expire_all()
    row = db.get(Item, item_id)
    assert row.review_claimed_at is not None
    assert fleet_svc.review_claim_holder(row) == reviewer


def test_taking_over_a_lapsed_hold_starts_a_fresh_clock(client, key, proj, db):
    """A new holder gets its own full lease. Inheriting the previous one's age would hand a
    reviewer an item that expires under it seconds later."""
    from datetime import datetime, timedelta, timezone

    from app.models import Item

    builder = _agent(client, key, "builder", "worker")
    first = _agent(client, key, "first", "worker")
    second = _agent(client, key, "second", "worker")
    item_id = _item_in_review(client, key, proj, builder)
    _ok(_mcp(client, key, "claim_review", {"project_id": proj, "agent_id": first}))

    row = db.get(Item, item_id)
    row.review_claimed_at = datetime.now(timezone.utc) - timedelta(
        seconds=DEFAULT_LEASE_SECONDS + 30)
    db.commit()

    _ok(_mcp(client, key, "claim_review", {"project_id": proj, "agent_id": second}))
    db.expire_all()
    row = db.get(Item, item_id)
    assert fleet_svc.review_claim_holder(row) == second
    assert (datetime.now(timezone.utc)
            - fleet_svc._aware(row.review_claimed_at)).total_seconds() < 5


# ---- what an independent reviewer found by bouncing the above --------------------------------

def test_a_reviewer_is_not_flagged_before_its_first_heartbeat(client, auth, key, proj, db):
    """THE BOUNCE. `claim_review` touches no agent state, so `holder_state` read the agent's
    STORED state and said `idle` until the next presence-only heartbeat — up to a full
    heartbeat interval later. The board paints that in the blocked colour with `· idle`
    appended: the exact contradiction this ticket was reported as, on a reviewer doing
    nothing wrong. A review that decided in under fifty seconds was red for its whole life.

    The test that "covered" this heartbeat FIRST, so it only ever exercised the case that
    already worked. This one deliberately does not.

    Sabotage: read the stored state again and this fails."""
    builder = _agent(client, key, "builder", "worker")
    reviewer = _agent(client, key, "reviewer", "worker")
    _item_in_review(client, key, proj, builder)

    _ok(_mcp(client, key, "claim_review", {"project_id": proj, "agent_id": reviewer}))
    # NO heartbeat here. This is the first second of every review.
    row = client.get(f"/api/fleet?project_id={proj}", headers=auth).json()["review_queue"][0]
    assert row["holder_state"] == "reviewing"


def test_a_dead_holder_is_still_flagged(client, auth, key, proj, db):
    """The case the board most needs to catch, and the reviewer found it UNGUARDED: swapping
    `presence_state(...)` for the raw `.state` left all 442 tests passing, so a crashed
    reviewer would have read `reviewing` in faint text forever.

    Sabotage: return the stored state and this fails."""
    from datetime import datetime, timedelta, timezone

    from app.models import Agent

    builder = _agent(client, key, "builder", "worker")
    reviewer = _agent(client, key, "reviewer", "worker")
    _item_in_review(client, key, proj, builder)
    _ok(_mcp(client, key, "claim_review", {"project_id": proj, "agent_id": reviewer}))

    # The process dies: the hold is still live, presence is not.
    row = db.get(Agent, reviewer)
    row.last_seen_at = datetime.now(timezone.utc) - timedelta(
        seconds=fleet_svc.presence_ttl_seconds() + 60)
    db.commit()

    queue = client.get(f"/api/fleet?project_id={proj}", headers=auth).json()["review_queue"][0]
    assert queue["holder_state"] == "offline", "a crashed reviewer must not read as reviewing"


def test_a_quarantined_holder_is_flagged_too(client, auth, key, proj, db):
    """Quarantined is the other state where a hold is a real problem, and it is checked
    before the clock — a quarantined agent may still be heartbeating."""
    from app.models import Agent

    builder = _agent(client, key, "builder", "worker")
    reviewer = _agent(client, key, "reviewer", "worker")
    _item_in_review(client, key, proj, builder)
    _ok(_mcp(client, key, "claim_review", {"project_id": proj, "agent_id": reviewer}))

    db.get(Agent, reviewer).state = "quarantined"
    db.commit()

    queue = client.get(f"/api/fleet?project_id={proj}", headers=auth).json()["review_queue"][0]
    assert queue["holder_state"] == "quarantined"


def test_an_agent_building_and_reviewing_at_once_reads_as_reviewing(client, auth, key, proj, db):
    """A build lease and a review claim legitimately coexist (GRPH-429). The QUEUE's question
    is about the review, not about everything else the agent is doing — flagging `working`
    would fire on a healthy agent for the same reason `idle` did."""
    from app.models import Agent

    builder = _agent(client, key, "builder", "worker")
    reviewer = _agent(client, key, "reviewer", "worker")
    _item_in_review(client, key, proj, builder)
    _ok(_mcp(client, key, "claim_review", {"project_id": proj, "agent_id": reviewer}))

    db.get(Agent, reviewer).state = "working"
    db.commit()

    queue = client.get(f"/api/fleet?project_id={proj}", headers=auth).json()["review_queue"][0]
    assert queue["holder_state"] == "reviewing"
