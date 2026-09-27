"""The child is told to commit (GRPH-974).

**Accept:** both templates carry `COMMIT`, a rendered instruction carries it, and the bound
template's own sequence names committing between building and moving the item to review.

**What went wrong.** Neither template said "commit". `BOUND_INSTRUCTION` said *"build it, move
it to review with evidence"*, and four of four productive children across the PRD-47 waves did
exactly that and exited on a dirty tree. Everything after was automatic and invisible:

1. `reap` finds the tree dirty and salvages it as `WIP: salvaged by gbfleet`.
2. A salvage subject is **deliberately never proposed** as a PR (GRPH-926 — those drafts had
   been landing as mergeable `WIP:` requests, which was the wrong receipt).
3. Nothing else opens one. No CI, no reviewer.
4. The item reaches `done` anyway, because `sign_off` does not ask whether a commit exists.

GRPH-959 stranded a 891-line `MemoryTriageView.tsx` that way — real work, five queues, bulk
actions, keyboard nav, reviewed by a second child that ran a sabotage against it — on an
unmerged branch, with the item reading `done` and main containing nothing.

The one child that got a PR that week (#865) committed of its own accord. So this was never a
model ignoring an instruction; the instruction did not exist.

**This is not one of the "sentence, not a control" layers.** BOUNDARY and DEPENDENCY ask a
child not to do something it might otherwise do, and a model that ignores them is not stopped.
This one restores a missing STEP in the loop the child is told to run. The reciprocal gate —
refusing `done` with no commit a reviewer could read — is GRPH-973 and is not built. Until it
is, this text is the only thing standing between real work and a stranded branch, which is why
it is pinned on both templates rather than the one that happened to be observed failing.
"""
from __future__ import annotations

from pathlib import Path

from gbfleet.seat import BOUND_INSTRUCTION, COMMIT, INSTRUCTION, Seat, instruction_for


def test_both_templates_carry_it():
    """THE ONE THAT MATTERS, and the reason it is written as a shared constant. Sabotage:
    splice it into one template only. Every other test in this file still passes while half
    the fleet strands its work, and the two halves are indistinguishable from outside — the
    same failure shape as the tail that made every review a respawn (PRD-39 §7.5)."""
    assert COMMIT in INSTRUCTION
    assert COMMIT in BOUND_INSTRUCTION


def test_a_rendered_instruction_carries_it(tmp_path: Path):
    """Rendered, not merely declared: the templates go through `.format`, so a stray brace in
    this text would raise there rather than here."""
    seat = Seat(code="WORKER-7F3K", server_url="https://gb.invalid", api_key="k")

    assert COMMIT in instruction_for(seat, tmp_path, "gb/w-1")


def test_a_bound_child_is_told_to_commit_before_review(tmp_path: Path):
    """The bound template is the one that spells out the sequence, and the sequence is where
    the step went missing: "build it, move it to review" left no room for a commit. A child
    reading only that line — which is the line naming its item — must be told there."""
    seat = Seat(code="WORKER-7F3K", server_url="https://gb.invalid", api_key="k",
                item="GRPH-959")
    text = instruction_for(seat, tmp_path, "gb/p47b-1")

    bound = next(line for line in text.splitlines() if "BOUND to GRPH-959" in line)
    assert "COMMIT it" in bound
    # Order matters: committing after the item is already in review is too late for the
    # supervisor's push-and-propose, which runs off what the child left behind.
    assert bound.index("COMMIT it") < bound.index("move it to review")


def test_it_says_why_rather_than_only_what(tmp_path: Path):
    """A bare "commit your work" is an instruction a model can rank below the thing it was
    asked to build. The consequence is what makes it load-bearing, and it is specific: no PR
    is opened at all, and the salvage draft is never proposed."""
    assert "pushes your branch" in COMMIT
    assert "never proposed" in COMMIT
    # And the one exception a child must not try to tidy away: its own credential file is
    # written into the tree by the supervisor, so "leave the tree clean" alone would be wrong.
    assert "credential file" in COMMIT


def test_it_does_not_ask_the_child_to_push_or_open_a_pr():
    """Pushing and proposing are the supervisor's, and a child that did them itself would
    bypass the UNDECLARED and BEHIND refusals that decide whether a branch may be proposed at
    all (GRPH-949). The instruction stops at the commit deliberately."""
    lowered = COMMIT.lower()
    assert "git push" not in lowered
    assert "open a pull request" not in lowered
    assert "gh pr" not in lowered
