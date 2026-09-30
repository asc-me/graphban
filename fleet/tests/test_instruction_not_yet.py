"""The reviewer is told that an unreadable revision is not a verdict (GRPH-987).

**What went wrong.** A child hands work over by moving its item to `review`, which makes it
visible to every reviewer everywhere at once. Its branch is pushed by its supervisor — and until
`_publish_in_review` that happened at reap, when the child exited, which a bound seat does not do
until it has also reviewed everybody else's work. A reviewer in any other checkout could win that
race, and on wave p47f one did:

    Branch gb/p47f-1 and commit 63b46358 are not on origin; reviewer cannot fetch or verify
    the diff.

Refusing to review a diff it cannot read is right and stays right — that is GRPH-973. What cost
the fleet was the channel: `bounce` put GRPH-961 back in `next` with a reason that reads like a
judgement on the work, a later reader cannot tell the difference, and the preference matrix
counted it as a failed attempt for the BUILDER's vendor and model. Minutes later
`origin/gb/p47f-1` was `63b46358` and the reviewer's own clone fetched it fine.

**This is a sentence, not a control** — the third layer, like BOUNDARY and DEPENDENCY, and a model
that ignores it is not stopped by it. The control is the supervisor publishing the branch the
moment it sees the item reach `review`, which closes the window instead of asking anybody to
behave inside it. This is the window that is left: a push that failed, a supervisor that died, or
a reviewer that reached the item inside one poll interval.
"""
from __future__ import annotations

from pathlib import Path

from gbfleet.seat import BOUND_INSTRUCTION, INSTRUCTION, NOT_YET, Seat, instruction_for


def test_both_templates_carry_it():
    """THE ONE THAT MATTERS, and the reason it is a shared constant.

    Sabotage: splice it into one template only. Every other test here still passes while half
    the fleet keeps bouncing for the environment. The two halves are the unbound reviewer
    (`INSTRUCTION`, what `until`'s review branch mints) and the bound builder that is told to
    review on the way out (`BOUND_INSTRUCTION`, PRD-39 D-h) — and p47f's reviewer was the
    second kind, so the template that looks less like a reviewer is the one that was observed
    failing.
    """
    assert NOT_YET in INSTRUCTION
    assert NOT_YET in BOUND_INSTRUCTION


def test_a_rendered_instruction_carries_it(tmp_path: Path):
    """Rendered, not merely declared: both templates go through `.format`, so a stray brace in
    this text would raise there rather than here."""
    unbound = Seat(code="WORKER-7F3K", server_url="https://gb.invalid", api_key="k")
    bound = Seat(code="WORKER-9X2M", server_url="https://gb.invalid", api_key="k",
                 item="GRPH-961")

    assert NOT_YET in instruction_for(unbound, tmp_path, "gb/w-1")
    assert NOT_YET in instruction_for(bound, tmp_path, "gb/w-2")


def test_it_forbids_the_bounce_and_names_what_to_do_instead():
    """A bare "that is not a verdict" leaves the reviewer holding an item it cannot read and no
    alternative, and `bounce` is the only exit it was told about. The replacement has to be
    concrete: leave it in `review`, say so, take the next one.

    Leaving it in `review` is what makes the outcome non-punitive AND retryable — the item stays
    claimable by the next reviewer, whose supervisor may well have published by then, and nothing
    is written against the builder."""
    assert "Do not `bounce`" in NOT_YET
    assert "Leave the item in `review`" in NOT_YET
    assert "take the next one" in NOT_YET


def test_it_says_why_rather_than_only_what():
    """The consequence is what makes it load-bearing, and it is specific: the bounce is scored
    against the builder's vendor and model, which is a measurement the fleet makes decisions
    from. A reviewer that understands it is corrupting a number ranks it below a real defect;
    one that does not treats it as tidiness."""
    assert "failed attempt against the builder's vendor and model" in NOT_YET


def test_it_does_not_license_reviewing_nothing():
    """The other failure mode, and the reason the clause is scoped to FETCHING.

    Told that an unreadable diff is not a verdict, a model can generalise it into "a diff I did
    not manage to read is not a verdict" and sign off work it never looked at — which is
    GRPH-954's false attestation with an extra step. The clause names the one condition (cannot
    fetch from the remote), keeps the item where a second reviewer will get it, and never
    mentions signing off.
    """
    lowered = NOT_YET.lower()
    assert "cannot fetch from the remote" in lowered
    assert "sign_off" not in lowered
    assert "sign off" not in lowered
    assert "approve" not in lowered


def test_it_does_not_ask_the_reviewer_to_push_or_wait():
    """Publishing is the supervisor's, exactly as COMMIT says on the builder's side — a reviewer
    that pushed a branch it was handed would be writing to a repository it does not own the
    judgement about. And "wait and retry" would have the reviewer sit on its claim until the
    600-second hold lapsed (GRPH-771), which is the second half of this item's title: the queue
    looks busy while nothing is happening."""
    lowered = NOT_YET.lower()
    assert "git push" not in lowered
    assert "gh " not in lowered
    assert "wait" not in lowered
    assert "retry" not in lowered
