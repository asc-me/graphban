"""The work PHASE on a roster holding (GRPH-522).

The supervisor could always see WHO held WHAT. It could not see what they were DOING with
it: `in_progress` covers writing code, waiting on CI, and reworking a bounce alike.

The phase is DERIVED from signals every vendor already writes, rather than reported by the
child, because we own `gbagent`'s loop and none of `claude`, `cursor-agent` or `grok` — a
reported field would be populated by one adapter and blank for three, and a blank column
reads as an idle agent. So these tests care most about two things: that each rung is load
bearing (delete it and something fails), and that the two ADMISSIONS — `stale` and
`unknown` — never get rendered as an activity.

The other design — let the supervisor fetch each item and infer for itself — is ruled out
by `ALLOWED_TOOLS`, which does not include `get_item_details`. That set is pinned by exact
equality in `fleet/tests/test_client.py` and `fleet/tests/test_touchpoints.py`, so the
guard is real but lives in the fleet suite: `gbfleet` is a separate package and is not
importable from here. Asserting it again from an uninstalled module would have been a test
that could only ever error.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import ApiKey, Item
from app.services import fleet as fleet_svc


@pytest.fixture
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _item(db, project_id="core", **fields):
    """A REAL persisted item, mutated into the state under test.

    Not a stub with the five attributes the function happens to read: SQLAlchemy applies
    column defaults at flush, so an unpersisted `Item()` has `blocker=None` where a real row
    has `""` — and a ladder tested against `None` would not prove it handles the value the
    database actually stores.
    """
    it = db.scalars(select(Item).where(Item.project_id == project_id)).first()
    for k, v in fields.items():
        setattr(it, k, v)
    db.commit()
    db.refresh(it)
    return it


# ── the ladder, rung by rung ──────────────────────────────────────────────────

def test_a_working_agent_mid_item_is_building(db):
    it = _item(db, status="in_progress", blocker="", pr=None, evidence=[], bounce_reason="")
    assert fleet_svc.holding_phase(it, "working") == ("building", "status in_progress")


def test_a_test_receipt_reads_as_verifying_not_building(db):
    """The distinction the whole ticket exists for: `status` cannot tell these apart."""
    it = _item(db, status="in_progress", blocker="", pr=None, bounce_reason="",
               evidence=[{"kind": "test", "detail": "42 passed"}])
    assert fleet_svc.holding_phase(it, "working") == ("verifying", "test receipt")


def test_narration_is_not_a_test_run(db):
    """`note`, `url` and `screenshot` are things an agent SAID. Counting them as
    verification would let a model talk its way into a phase it never earned."""
    it = _item(db, status="in_progress", blocker="", pr=None, bounce_reason="",
               evidence=[{"kind": "note", "detail": "looks right to me"},
                         {"kind": "url", "url": "http://x"},
                         {"kind": "screenshot", "url": "http://y"}])
    assert fleet_svc.holding_phase(it, "working")[0] == "building"


def test_a_recorded_pr_outranks_the_test_receipt(db):
    """Both signals are true at once — the tests ran, THEN the branch went up. The later
    one is the current phase; reporting `verifying` would describe a finished moment."""
    it = _item(db, status="in_progress", blocker="", bounce_reason="",
               evidence=[{"kind": "test", "detail": "42 passed"}],
               pr={"url": "http://gh/pr/1", "number": 1})
    assert fleet_svc.holding_phase(it, "working") == ("integrating", "pr recorded")


def test_review_outranks_a_recorded_pr(db):
    it = _item(db, status="review", blocker="", bounce_reason="",
               pr={"url": "http://gh/pr/1"}, evidence=[])
    assert fleet_svc.holding_phase(it, "working") == ("review", "status review")


def test_a_blocker_shows_through_in_progress(db):
    """`update_item(blocker=...)` sets the text WITHOUT requiring the status move, so an
    agent can be stuck while still `in_progress`. Reading status alone would miss exactly
    the agent that said it was stuck."""
    it = _item(db, status="in_progress", blocker="waiting on a credential",
               pr={"url": "http://gh/pr/1"}, evidence=[{"kind": "test", "detail": "ok"}])
    assert fleet_svc.holding_phase(it, "working") == ("blocked", "blocker set")


def test_blocked_status_alone_also_reads_as_blocked(db):
    it = _item(db, status="blocked", blocker="", pr=None, evidence=[])
    assert fleet_svc.holding_phase(it, "working") == ("blocked", "status blocked")


def test_a_claim_that_has_not_started_is_not_building(db):
    """`claim_cluster` reserves work before the agent moves any of it. Calling that
    `building` would report typing that has not begun."""
    it = _item(db, status="next", blocker="", pr=None, evidence=[], bounce_reason="")
    assert fleet_svc.holding_phase(it, "working") == ("claimed", "claimed, status next")


def test_a_status_with_no_signal_is_unknown_and_says_so(db):
    """`done` while still held: nothing on the ladder matches. The answer is the admission,
    and its basis names the status so a human can see WHY nothing matched."""
    it = _item(db, status="done", blocker="", pr=None, evidence=[], bounce_reason="")
    phase, basis = fleet_svc.holding_phase(it, "working")
    assert phase == "unknown"
    assert "done" in basis


# ── rule 1: the absent agent ──────────────────────────────────────────────────

@pytest.mark.parametrize("state", ["offline", "quarantined"])
def test_an_absent_agent_is_stale_not_busy(db, state):
    """THE guard. An agent that died mid-item leaves an item that says `in_progress`
    forever. Derived from the item alone, a dead worker renders as busy indefinitely —
    this repo's recurring defect class, where the absence reads as clean."""
    it = _item(db, status="in_progress", blocker="", pr=None, evidence=[], bounce_reason="")
    assert fleet_svc.holding_phase(it, state) == ("stale", f"agent {state}")


def test_staleness_outranks_every_other_signal(db):
    """Not just the plain case: a dead agent whose item carries a blocker, a PR and a test
    receipt must still read `stale`. Placing rule 1 anywhere below the top would let the
    richest-looking rows be exactly the ones that lie."""
    it = _item(db, status="review", blocker="stuck", bounce_reason="redo it",
               pr={"url": "http://gh/pr/1"}, evidence=[{"kind": "test", "detail": "ok"}])
    assert fleet_svc.holding_phase(it, "offline")[0] == "stale"


def test_the_live_states_are_never_stale(db):
    """The complement, so `stale` cannot be reached by a state that means the agent is
    here. Without this, widening `_ABSENT` to every state would still pass the tests above."""
    it = _item(db, status="in_progress", blocker="", pr=None, evidence=[], bounce_reason="")
    for state in ("idle", "working", "reviewing"):
        assert fleet_svc.holding_phase(it, state)[0] != "stale", state


# ── rework is orthogonal, not a rung ──────────────────────────────────────────

def test_a_bounce_is_reported_beside_the_phase_not_instead_of_it(db):
    """Folding "fix" into the ladder would force an arbitrary precedence against
    `verifying`, and the bounce would vanish from the row the moment the agent ran a test.
    Both facts survive because they are separate fields."""
    it = _item(db, status="in_progress", blocker="", pr=None, bounce_reason="redo it",
               evidence=[{"kind": "test", "detail": "ok"}])
    assert fleet_svc.holding_phase(it, "working")[0] == "verifying"
    assert fleet_svc.was_bounced(it) is True


def test_an_unbounced_item_says_so(db):
    it = _item(db, status="in_progress", bounce_reason="")
    assert fleet_svc.was_bounced(it) is False


# ── the wiring, through the real roster ───────────────────────────────────────

def _agent(db, project_id="core", label="phase-1"):
    key = db.scalars(select(ApiKey).where(ApiKey.project_id == project_id)).first()
    agent = fleet_svc.register_agent(db, project_id=project_id, api_key=key, label=label)
    db.commit()
    return agent


def test_the_roster_carries_the_phase_for_a_live_holder(db):
    """End to end through `list_agents`, not just the pure function: the pair could be
    computed perfectly and never reach the payload."""
    agent = _agent(db)
    agent.last_seen_at = datetime.now(timezone.utc)
    agent.state = "working"
    _item(db, status="in_progress", blocker="", pr=None, bounce_reason="",
          claimed_by=agent.id, evidence=[{"kind": "sabotage", "claim": "broke the guard",
                                          "tests_failed": 1}])
    row = next(r for r in fleet_svc.list_agents(db, "core") if r["id"] == agent.id)
    held = row["holdings"][0]
    assert held["phase"] == "verifying"
    assert held["phase_basis"] == "test receipt"
    assert held["bounced"] is False
    # The pre-existing fields are still there — this is an addition, not a reshape.
    assert set(held) >= {"id", "stored_id", "title", "status"}


def test_the_roster_reports_a_lapsed_holder_as_stale(db):
    """The same row, with only the clock moved. The item is untouched and still says
    `in_progress`; what changed is that nobody is there to be doing it."""
    agent = _agent(db, label="phase-2")
    agent.last_seen_at = datetime.now(timezone.utc) - timedelta(days=1)
    _item(db, status="in_progress", blocker="", pr=None, bounce_reason="", evidence=[],
          claimed_by=agent.id)
    row = next(r for r in fleet_svc.list_agents(db, "core") if r["id"] == agent.id)
    assert row["state"] == "offline"
    assert row["holdings"][0]["phase"] == "stale"


def _count_selects(db, fn):
    from sqlalchemy import event

    seen = []
    engine = db.get_bind()
    hook = lambda conn, cur, stmt, *a: seen.append(stmt)  # noqa: E731
    event.listen(engine, "before_cursor_execute", hook)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", hook)
    return [s for s in seen if s.lstrip().upper().startswith("SELECT")]


def test_the_phase_does_not_cost_a_query_per_holding(db):
    """`held` already loads full `Item` rows, so the derivation reads fields that are in
    hand. A per-holding lookup would turn the roster — the one call a supervisor makes on
    every tick — into an N+1.

    Asserted as "the count does not GROW", not against a magic number: the absolute count
    varies with the engine, and CI runs two. What must hold on both is that three holdings
    cost what one does.
    """
    agent = _agent(db, label="phase-n1")
    agent.last_seen_at = datetime.now(timezone.utc)
    items = db.scalars(select(Item).where(Item.project_id == "core")).all()[:3]
    assert len(items) == 3, "fixture needs three items to tell N+1 from a constant"

    items[0].claimed_by = agent.id
    db.commit()
    one = _count_selects(db, lambda: fleet_svc.list_agents(db, "core"))
    held_one = fleet_svc.list_agents(db, "core")
    assert sum(len(r["holdings"]) for r in held_one if r["id"] == agent.id) == 1

    for it in items:
        it.claimed_by = agent.id
    db.commit()
    three = _count_selects(db, lambda: fleet_svc.list_agents(db, "core"))
    held_three = fleet_svc.list_agents(db, "core")
    assert sum(len(r["holdings"]) for r in held_three if r["id"] == agent.id) == 3

    assert len(three) == len(one), (
        f"{len(one)} selects for one holding, {len(three)} for three — the derivation is "
        f"querying per holding")


def test_every_phase_the_ladder_can_return_is_declared():
    """`PHASES` is what a consumer switches on. A rung returning a phase missing from the
    tuple renders as an unhandled case in whatever reads the roster."""
    import inspect

    src = inspect.getsource(fleet_svc.holding_phase)
    returned = set(__import__("re").findall(r'return "([a-z]+)"', src))
    assert returned, "found no literal returns — the regex has drifted from the source"
    assert returned <= set(fleet_svc.PHASES), returned - set(fleet_svc.PHASES)
