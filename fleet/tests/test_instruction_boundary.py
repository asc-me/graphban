"""The child is told where the job ends (GRPH-832).

An item's description is free text written by whoever filed it, and it reaches the child as
instructions. On 2026-09-08 one item's prose asked for production work and a cheap-tier worker
did it — rotated a key, deleted rows, set environment variables on a hosted platform,
redeployed — while its declared touchpoints were four ordinary repository files.

**This is the weakest of the three layers and must not be counted as one of the strong ones.**
The server refuses to delegate an item declared `reach=deploy`, and the PATH shim refuses the
commands; both of those are mechanism. This is a sentence, and a model that ignores it is not
stopped by it. It is here because it costs nothing and because it already worked once
unprompted: a worker on the reported wave refused exactly this kind of item on its own
judgment and wrote a blocker. Asking every time beats hoping.

What these tests pin is that it reaches BOTH kinds of child. The bound template and the
unbound one have drifted apart before — a tail added to one and not the other is what made
every review a respawn (PRD-39 §7.5) — and a boundary that half the fleet is told about is
worse than none, because the halves are indistinguishable from outside.
"""
from __future__ import annotations

from pathlib import Path

from gbfleet.seat import BOUNDARY, BOUND_INSTRUCTION, INSTRUCTION, Seat, instruction_for


def test_both_templates_carry_it():
    """THE ONE THAT MATTERS. Sabotage: splice it into one template only — every other test
    here still passes, and half the fleet runs unbounded."""
    assert BOUNDARY in INSTRUCTION
    assert BOUNDARY in BOUND_INSTRUCTION


def test_a_rendered_instruction_carries_it(tmp_path: Path):
    """Rendered, not just declared: the templates go through `.format`, and a stray brace in
    this text would raise there rather than here."""
    seat = Seat(code="WORKER-7F3K", server_url="https://gb.invalid", api_key="k")
    text = instruction_for(seat, tmp_path, "gb/w-1")

    assert BOUNDARY in text


def test_a_bound_child_gets_it_too(tmp_path: Path):
    seat = Seat(code="WORKER-7F3K", server_url="https://gb.invalid", api_key="k",
                item="GRPH-1")
    text = instruction_for(seat, tmp_path, "gb/w-1")

    assert BOUNDARY in text


def test_it_names_the_actions_rather_than_a_category():
    """"Do not do anything dangerous" is advice nobody can act on. The four verbs are the four
    the incident actually used, so a model matching on the words has something to match."""
    said = BOUNDARY.lower()

    for act in ("deploy", "rotate", "environment variable", "live database"):
        assert act in said, act


def test_it_says_what_to_do_instead_of_only_what_not_to_do():
    """A refusal with no next step strands the item and the wave: the worker exits, the item
    stays claimable, and the next child gets the same text. Writing a blocker is what the one
    worker who got this right actually did."""
    assert "block the item" in BOUNDARY.lower()
    assert "carry on with the rest" in BOUNDARY.lower()


def test_it_is_placed_before_the_claiming_sentence():
    """Order is not cosmetic here. The claiming instruction is what sends the child looking for
    work; a boundary that arrived after it would be read after the decision it constrains."""
    assert INSTRUCTION.index(BOUNDARY) < INSTRUCTION.index("Call claim_review")
    assert BOUND_INSTRUCTION.index(BOUNDARY) < BOUND_INSTRUCTION.index("This seat is BOUND")
