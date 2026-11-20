"""GRPH-1089: a bound_only seat is told to exit. The default bound seat still reviews."""
from pathlib import Path

from gbfleet.seat import instruction_for, Seat


def test_bound_only_instruction_exits_and_the_default_still_reviews():
    bound_only = instruction_for(
        Seat(code="WORKER-1", server_url="https://x", api_key="k",
             item="GRPH-9", bound_only=True),
        Path("/wt"), "gb/x")
    default = instruction_for(
        Seat(code="WORKER-1", server_url="https://x", api_key="k", item="GRPH-9"),
        Path("/wt"), "gb/x")

    assert "GRPH-9" in bound_only
    assert "bound-only" in bound_only
    assert "Do NOT call claim_review or claim_next" in bound_only
    assert "COMMIT" in bound_only
    # The default tail is the review loop. It must not leak into the bound-only text.
    assert "review what you did NOT build" not in bound_only
    assert "call claim_review with wait_seconds=0 and review what you did NOT build" in default


def _seats():
    base = dict(code="WORKER-1", server_url="https://x", api_key="k")
    return [Seat(**base), Seat(**base, item="GRPH-9"),
            Seat(**base, item="GRPH-9", bound_only=True), Seat(**base, review_only=True)]


def test_subagents_omitted_leaves_every_instruction_unchanged():
    for seat in _seats():
        assert instruction_for(seat, Path("/wt"), "gb/x", subagents=None) == \
            instruction_for(seat, Path("/wt"), "gb/x")


def test_subagents_cap_is_stated_with_what_the_subagents_may_not_do():
    text = instruction_for(_seats()[1], Path("/wt"), "gb/x", subagents=2)
    assert "at most 2 vendor subagent" in text
    for phrase in ("do not register", "do not set parent_agent_id",
                   "sign_off, bounce, or mint_enrolment",
                   "integrate their edits, commit, and move the item to review"):
        assert phrase in text


def test_subagents_zero_forbids_and_states_no_positive_cap():
    text = instruction_for(_seats()[1], Path("/wt"), "gb/x", subagents=0)
    assert "Do not spawn vendor subagents" in text
    assert "at most" not in text
