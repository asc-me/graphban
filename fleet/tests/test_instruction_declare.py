"""The child is told to declare every file it changed (GRPH-1025).

The supervisor withholds a PR from a branch that changed a file outside every declared
touchpoint (GRPH-949). A planner files the source path; the acceptance asks for a test, a
migration, a regenerated README — and nothing told the child to declare them, so a child that
did exactly what it was asked was refused. Planner decision: children are told to declare; no
path-pattern exemption for tests. This pins the text.
"""
from __future__ import annotations

from pathlib import Path

from gbfleet.seat import (
    BOUND_INSTRUCTION, DECLARE, INSTRUCTION, REVIEW_ONLY_INSTRUCTION, Seat, instruction_for,
)


def test_both_build_templates_carry_it():
    """Sabotage: splice it into one template only and half the fleet is refused again."""
    assert DECLARE in INSTRUCTION
    assert DECLARE in BOUND_INSTRUCTION


def test_a_review_only_seat_builds_nothing_and_is_not_told_to_declare():
    assert DECLARE not in REVIEW_ONLY_INSTRUCTION


def test_it_names_the_files_planners_forget():
    for word in ("update_item", "touchpoints", "tests", "migrations", "generated"):
        assert word in DECLARE


def test_it_comes_before_review_in_the_bound_sequence():
    """Declared after the move to review is declared after the reviewer has the item."""
    assert "Before you move the item to review" in DECLARE


def test_a_rendered_instruction_carries_it(tmp_path: Path):
    seat = Seat(code="WORKER-7F3K", server_url="https://gb.invalid", api_key="k")
    assert DECLARE in instruction_for(seat, tmp_path, "gb/w-1")
    bound = Seat(code="WORKER-7F3K", server_url="https://gb.invalid", api_key="k",
                 item="GRPH-1")
    assert DECLARE in instruction_for(bound, tmp_path, "gb/w-1")
