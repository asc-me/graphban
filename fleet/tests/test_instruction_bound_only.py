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
