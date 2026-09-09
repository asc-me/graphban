"""`fleet_status` is lean by default and can drop the dead (GRPH-807).

Reported as "unbounded and unfilterable — ~11k tokens at 34 agents, and it only grows since
nothing prunes the roster. A planner polling a wave pays that repeatedly."

Measured on the live instance before choosing anything, because the reported remedy turned out
to be the smaller half:

    agents 177, of which ONE was live
    full   27,391 tokens      nearly twice the whole MCP manifest, per poll
    lean    9,883             64% smaller, same agents
    live       48             99.8% smaller

So the lean/full split the finding asked for is worth 64%, and the dominant term is dead
agents nothing prunes (GRPH-814). `live` is the lever, and it is opt-in on purpose.
"""
import pytest

from app.services import fleet as fleet_svc


def _payload():
    return {
        "agents": [
            {"id": "A1", "key": "CORE-A1", "state": "working", "active_role": "worker",
             "enrolment_id": "e1", "worktree": "/w/1", "assigned": {"item": "X-1"},
             "label": "claude @ box", "capabilities": {"vendor": "claude"},
             "credential_roles": ["worker"], "last_refusal": None, "enrolled": True,
             "dismissed": False, "branch": "gb/w-1", "branch_orphaned": False,
             "last_seen_at": "2026-09-09T00:00:00+00:00",
             "holdings": [{"id": "X-1", "status": "in_progress", "phase": "building",
                           "claimed_by": "A1", "touchpoints": ["a.py"],
                           "phase_basis": "evidence", "bounced": False}]},
            {"id": "A2", "key": "CORE-A2", "state": "offline", "active_role": "worker",
             "label": "dead one", "holdings": []},
            {"id": "A3", "key": "CORE-A3", "state": "quarantined", "holdings": []},
        ],
        "seats": [{"id": "s1"}],
    }


# ---- what lean keeps, and why ------------------------------------------------------------------

def test_lean_keeps_everything_a_supervisor_reads():
    """Drop one of these and a wave stops being able to match a child it spawned to the roster
    row it became."""
    row = fleet_svc.roster_view(_payload(), "lean")["agents"][0]

    for field in ("id", "state", "enrolment_id", "worktree", "assigned", "holdings"):
        assert field in row, f"lean dropped {field}, which the supervisor reads"
    held = row["holdings"][0]
    for field in ("id", "status", "phase", "claimed_by", "touchpoints"):
        assert field in held, f"lean dropped holdings.{field}"


def test_lean_drops_what_only_a_human_reads():
    """The Fleet view uses REST, not this tool."""
    row = fleet_svc.roster_view(_payload(), "lean")["agents"][0]

    for field in ("label", "capabilities", "credential_roles", "last_refusal", "last_seen_at"):
        assert field not in row, f"lean kept {field}"
    assert "phase_basis" not in row["holdings"][0]


def test_lean_is_a_denylist_so_a_contract_field_survives():
    """`enrolled` is on the roster because PRD-22 §6 put it there. The first version of this
    listed what to KEEP, was 25 points smaller, and dropped it — caught by the test that holds
    that contract. An allowlist erodes a contract every time somebody adds a field and forgets
    this list."""
    row = fleet_svc.roster_view(_payload(), "lean")["agents"][0]

    assert row["enrolled"] is True
    assert row["branch"] == "gb/w-1", "a field nobody named was dropped anyway"


def test_lean_keeps_every_agent():
    """Fewer FIELDS is a different act from fewer AGENTS. A caller that silently stopped
    seeing offline agents would stop being able to notice one."""
    got = fleet_svc.roster_view(_payload(), "lean")["agents"]

    assert [a["id"] for a in got] == ["A1", "A2", "A3"]


# ---- live, which is the lever ---------------------------------------------------------------------

def test_live_drops_the_absent():
    got = fleet_svc.roster_view(_payload(), "live")["agents"]

    assert [a["id"] for a in got] == ["A1"]


def test_live_drops_quarantined_too():
    """`_ABSENT` is the existing definition of "its signals are frozen". Reusing it means
    this cannot drift from what `holding_phase` already calls stale."""
    assert "A3" not in [a["id"] for a in fleet_svc.roster_view(_payload(), "live")["agents"]]


def test_live_is_not_the_default():
    """OPT-IN, deliberately. Quietly changing which agents a roster reports is a different
    act from changing which fields it carries, and only one of them is safe to default."""
    assert len(fleet_svc.roster_view(_payload(), "lean")["agents"]) == 3


# ---- full, and honesty about the shape -----------------------------------------------------------

def test_full_is_exactly_what_it_was():
    """The escape hatch has to be untouched, or `full` is a third shape rather than the old
    one."""
    payload = _payload()

    assert fleet_svc.roster_view(payload, "full") is payload


def test_the_reply_says_which_shape_it_is():
    """`search_items` echoes `fields` so a reader can tell an absent field from an empty one.
    A narrowed payload that did not say so is indistinguishable from an agent with nothing
    set."""
    got = fleet_svc.roster_view(_payload(), "lean")

    assert got["view"] == "lean"
    assert "label" in got["dropped"], "narrowed a payload without saying what it dropped"


def test_seats_and_the_rest_of_the_payload_survive():
    """Only `agents` is narrowed. A view that ate the seats would break minting."""
    assert fleet_svc.roster_view(_payload(), "lean")["seats"] == [{"id": "s1"}]


def test_an_unknown_view_is_treated_as_lean_rather_than_full():
    """The safe direction for a typo: a smaller payload, never the 27,000-token one."""
    got = fleet_svc.roster_view(_payload(), "nonsense")

    assert "label" not in got["agents"][0]
