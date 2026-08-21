"""PRD-22 D-b — a spawned child is a separate process, and declares no parent.

**Most of what PRD-22 asks for here already existed**, delivered by PRD-19 and GRPH-361:
`test_a_minted_seat_records_its_minter_and_sets_no_parentage` proves a planner-minted
seat produces an agent with no parentage, `test_two_agents_a_planner_seated_can_review_
each_other` is the acceptance criterion, and `test_fleet_subagent_independence.py`
covers every parentage branch of the predicate with controls. Rewriting those here would
have added coverage-shaped noise and nothing else.

What was missing is narrower, and this file is only that:

1. **Both directions.** The existing acceptance test drives `claim_review` one way round.
   `independent` checks parentage with a two-sided `or`, and deleting either half leaves
   a one-directional test green — so the pair is asserted both ways, each with a control.
2. **The invitation in the tool surface.** `register_agent` described `parent_agent_id`
   as "Set if you are a SUBAGENT: who spawned you", and a process a supervisor launched
   has an obvious and wrong answer to that question.
"""
import json

import pytest

from app.mcp_server import TOOLS
from app.models import Agent
from app.services import fleet


# Reusing the enrolment suite's fixtures rather than copying them. The shared-credential
# shape is the whole point here too: gbfleet provisions a fleet from ONE key, and
# independence has to survive that.
from tests.test_fleet_enrolment import _seat, db, key, proj  # noqa: E402,F401


def _rpc(client, key, tool, args=None):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args or {}}},
        headers={"X-API-Key": key},
    ).json()["result"]


def _ok(client, key, tool, args=None):
    res = _rpc(client, key, tool, args)
    assert not res.get("isError"), res
    return json.loads(res["content"][0]["text"])


def _register_agent_schema():
    entry = next(t for t in TOOLS if t["name"] == "register_agent")
    return entry["inputSchema"]["properties"]


def _spawned_pair(db, proj, client, key):
    """Two agents provisioned exactly as gbfleet provisions them.

    One credential, a planner minting two seats, each child redeeming one, and nothing
    declaring parentage. `mint_enrolment_as` records `minted_by` and deliberately leaves
    parentage unset — recording the minter as parent is the intuitive move and would make
    every seat a planner issued mutually non-independent.
    """
    _, pcode = _seat(db, proj, "planner")
    boss = _ok(client, key, "register_agent", {"label": "planner", "enrolment_code": pcode})
    w = _ok(client, key, "mint_enrolment", {"agent_id": boss["agent_id"], "role": "worker"})
    r = _ok(client, key, "mint_enrolment", {"agent_id": boss["agent_id"], "role": "reviewer"})

    worker = _ok(client, key, "register_agent",
                 {"label": "w", "enrolment_code": w["enrolment_code"]})
    reviewer = _ok(client, key, "register_agent",
                   {"label": "r", "enrolment_code": r["enrolment_code"]})
    return db.get(Agent, worker["agent_id"]), db.get(Agent, reviewer["agent_id"])


def test_a_spawned_pair_is_independent_in_both_directions(client, key, proj, db):
    """`independent` checks parentage with a two-sided `or`, and a one-directional test
    passes with half of it deleted.

    Deleting `author.parent_agent_id == reviewer.id` leaves `independent(a, b)` returning
    True for a pair where B is A's child — caught only by asking the other way round.
    """
    worker, reviewer = _spawned_pair(db, proj, client, key)

    assert worker.parent_agent_id is None
    assert reviewer.parent_agent_id is None
    assert worker.enrolment_id and reviewer.enrolment_id
    assert worker.enrolment_id != reviewer.enrolment_id

    assert fleet.independent(reviewer, worker) is True
    assert fleet.independent(worker, reviewer) is True


@pytest.mark.parametrize("who", ["worker", "reviewer"])
def test_declaring_a_parent_flips_it_in_both_directions(client, key, proj, db, who):
    """The control, and without it the assertions above prove nothing.

    These two are independent for other reasons as well — distinct seats on one
    credential is exactly the case `independent` accepts. So a test that only asserted
    True would pass unchanged while parentage was being set, which is precisely what
    GRPH-361 was sent back for. Setting it has to be shown to CHANGE the answer.
    """
    worker, reviewer = _spawned_pair(db, proj, client, key)
    child, parent = (worker, reviewer) if who == "worker" else (reviewer, worker)

    child.parent_agent_id = parent.id
    db.flush()

    assert fleet.independent(reviewer, worker) is False
    assert fleet.independent(worker, reviewer) is False


def test_siblings_of_one_supervisor_would_lose_review_entirely(client, key, proj, db):
    """Why `mint_enrolment_as` does not record the minter as the parent.

    The intuitive implementation — a planner mints, so the planner is the parent — makes
    every seat it issued a sibling of every other, and `independent` treats siblings
    under one parent as one call tree. Nothing errors; the fleet reads as correctly
    provisioned and cannot review a single thing it builds.
    """
    worker, reviewer = _spawned_pair(db, proj, client, key)
    assert fleet.independent(reviewer, worker) is True

    worker.parent_agent_id = "GRPH-A-PLANNER"
    reviewer.parent_agent_id = "GRPH-A-PLANNER"
    db.flush()

    assert fleet.independent(reviewer, worker) is False
    assert fleet.independent(worker, reviewer) is False


def test_a_registration_that_omits_parentage_leaves_it_absent(client, key, proj, db):
    """Absence asserted as absence.

    The field must stay unset rather than acquiring a default that reads as intentional —
    an empty string, the minter, the credential name. `None` is the only value that means
    "this agent has no parent" as opposed to "something filled this in".
    """
    _, code = _seat(db, proj, "worker")
    hand = _ok(client, key, "register_agent", {"label": "w", "enrolment_code": code})
    agent = db.get(Agent, hand["agent_id"])

    assert agent.parent_agent_id is None
    assert agent.parent_agent_id != ""


def test_an_explicit_null_is_the_same_as_saying_nothing(client, key, proj, db):
    """gbfleet omits the key entirely; a hand-written client may send null. Both have to
    mean the same thing, or the guarantee depends on which client is talking."""
    _, code = _seat(db, proj, "worker")
    hand = _ok(client, key, "register_agent",
               {"label": "w", "enrolment_code": code, "parent_agent_id": None})
    assert db.get(Agent, hand["agent_id"]).parent_agent_id is None


def test_the_schema_does_not_ask_a_spawned_process_who_spawned_it(client, key, proj, db):
    """The invitation, which was live in the tool surface.

    `parent_agent_id` read "Set if you are a SUBAGENT: who spawned you." A gbfleet child
    HAS been spawned, by a supervisor, so that question has an obvious answer and the
    answer is wrong — it is a separate process, not a subagent inside anyone's turn.

    Prose is a weak guard and this is a weak test, and it is still the right place for
    it: the schema is what the child actually reads, and every stronger mechanism was
    considered and rejected. The server cannot refuse parentage-with-a-seat, because a
    subagent holding its own seat is a real supported shape that
    `test_parentage_refuses_a_child_holding_its_own_seat` exists to govern.
    """
    description = _register_agent_schema()["parent_agent_id"]["description"]

    assert "who spawned you" not in description.lower(), (
        "the schema asks a question a supervisor-launched child answers wrongly"
    )
    assert "inside" in description.lower(), "it must say what a subagent actually is"
    assert "spawned process" in description.lower(), (
        "it must name the case that is NOT a subagent, or the distinction is left implicit"
    )
