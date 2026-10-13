"""The child is told a bounced branch is its own work (GRPH-1031).

Builders were refusing to merge the item's own earlier branch — even with an
explicit planner note — so they blocked asking for a manual rebase or rebuilt
from main and regressed reviewed work. This is a sentence, not a control, the
same layer as BOUNDARY and DEPENDENCY.
"""
from __future__ import annotations

from pathlib import Path

from gbfleet.seat import (
    BOUND_INSTRUCTION,
    INSTRUCTION,
    OWN_WORK,
    REVIEW_ONLY_INSTRUCTION,
    Seat,
    instruction_for,
)


def test_both_builder_templates_carry_it():
    """THE ONE THAT MATTERS. Sabotage: splice it into one template only — half the
    fleet still treats the prior branch as foreign."""
    assert OWN_WORK in INSTRUCTION
    assert OWN_WORK in BOUND_INSTRUCTION
    assert OWN_WORK in REVIEW_ONLY_INSTRUCTION


def test_a_rendered_instruction_carries_it(tmp_path: Path):
    unbound = Seat(code="WORKER-7F3K", server_url="https://gb.invalid", api_key="k")
    bound = Seat(code="WORKER-9X2M", server_url="https://gb.invalid", api_key="k",
                 item="GRPH-1")
    reviewer = Seat(code="WORKER-R", server_url="https://gb.invalid", api_key="k",
                    review_only=True)

    assert OWN_WORK in instruction_for(unbound, tmp_path, "gb/w-1")
    assert OWN_WORK in instruction_for(bound, tmp_path, "gb/w-2")
    assert OWN_WORK in instruction_for(reviewer, tmp_path, "gb/w-3")


def test_it_names_own_work_and_forbids_rebuilding():
    said = OWN_WORK.lower()
    assert "your own work" in said
    assert "do not refuse" in said
    assert "do not rebuild from the base" in said
