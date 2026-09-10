"""GRPH-842: whether the machine can hold another child, and how that is reported.

The gate exists because a wave was killed for memory the fleet was not using — the
harness's monitor stops the process it owns, not the biggest consumer. So the two things
worth asserting are that it binds on a full machine and that it does NOT bind on a host
that cannot be measured, because the second is the reading that would otherwise look
identical to a roomy one.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbfleet import headroom  # noqa: E402
from gbfleet.headroom import Headroom, RESERVE, fits, gigabytes  # noqa: E402

GB = 1024**3
CHILD = headroom.DEFAULT_CHILD_MEMORY


def _reader(*values):
    """A memory reader that returns each value in turn, then repeats the last."""
    seq = list(values)

    def read() -> int | None:
        return seq[0] if len(seq) == 1 else seq.pop(0)

    return read


def test_fits_divides_what_is_left_after_the_reserve():
    assert fits(RESERVE + 3 * CHILD, CHILD) == 3
    assert fits(RESERVE + CHILD - 1, CHILD) == 0
    assert fits(0, CHILD) == 0


def test_fits_is_none_when_the_reading_is_none():
    # Not 0. A host that cannot be asked has not been found full, and the two answers
    # send an operator in opposite directions.
    assert fits(None, CHILD) is None
    assert gigabytes(None) == "unmeasured"


def test_full_machine_refuses_the_second_child():
    room = Headroom(CHILD, read=_reader(RESERVE))
    assert room.allow(0).allowed, "the first child is never gated"
    verdict = room.allow(1)
    assert not verdict.allowed
    assert verdict.fits == 0
    assert "no room" in verdict.reason


def test_unmeasurable_host_does_not_bind():
    room = Headroom(CHILD, read=_reader(None))
    verdict = room.allow(3)
    assert verdict.allowed
    assert verdict.fits is None
    assert verdict.available is None
    assert "not measurable" in verdict.reason


def test_first_child_is_never_gated_even_when_full():
    room = Headroom(CHILD, read=_reader(0))
    verdict = room.allow(0)
    assert verdict.allowed
    # And it says why, so an operator reading "allowed" on a full box is not misled.
    assert "first child" in verdict.reason
    assert verdict.fits == 0


def test_spawned_children_are_charged_before_the_kernel_notices_them():
    """The lag this exists for: a Popen that returned has not allocated anything yet.

    The reader below never changes — a machine with room for exactly two more children,
    reported identically forever. Without charging, the loop would start an unbounded
    number of them on the strength of that one reading.
    """
    room = Headroom(CHILD, read=_reader(RESERVE + 2 * CHILD))
    assert room.allow(1).allowed
    room.spawned()
    assert room.allow(2).allowed
    room.spawned()
    assert not room.allow(3).allowed


def test_a_failed_launch_costs_nothing():
    room = Headroom(CHILD, read=_reader(RESERVE + CHILD))
    assert room.allow(1).allowed
    # No `spawned()` — the launch raised. The next seat gets the same budget.
    assert room.allow(1).allowed


def test_the_live_reading_wins_when_it_is_worse_than_the_model():
    """Something else on the machine took the memory. The model would not know."""
    room = Headroom(CHILD, read=_reader(RESERVE + 10 * CHILD, RESERVE))
    assert not room.allow(1).allowed


def test_the_model_wins_when_the_live_reading_has_not_caught_up():
    room = Headroom(CHILD, read=_reader(RESERVE + CHILD))
    room.spawned()
    verdict = room.allow(1)
    assert not verdict.allowed, "the live reading still shows room the charged child will take"


def test_baseline_is_read_once_at_construction():
    room = Headroom(CHILD, read=_reader(7 * GB, 1 * GB))
    assert room.baseline == 7 * GB


@pytest.mark.parametrize("per_child", [0, -1])
def test_a_nonsense_per_child_does_not_gate(per_child):
    # Division by zero would be a crash inside a launch loop holding the repo lock.
    assert fits(10 * GB, per_child) is None


def test_a_charge_expires_so_a_long_run_does_not_starve():
    """`until` spawns one seat at a time for as long as there is ready work.

    A charge that never lapsed would make an hour-long run refuse everything long after the
    children it was charged for had exited — the gate slowly turning into a stop.
    """
    now = [1000.0]
    room = Headroom(CHILD, read=_reader(RESERVE + CHILD), clock=lambda: now[0])
    room.spawned()
    assert not room.allow(1).allowed, "charged: the reading has not caught up yet"
    now[0] += headroom.SETTLE + 1
    assert room.allow(1).allowed, "settled: the kernel counts that child itself now"


def test_the_charge_only_lapses_for_children_old_enough():
    now = [0.0]
    room = Headroom(CHILD, read=_reader(RESERVE + 2 * CHILD), clock=lambda: now[0])
    room.spawned()
    now[0] += headroom.SETTLE + 1
    room.spawned()  # this one is fresh; the first has settled
    assert room.allow(2).allowed, "one live charge leaves room for one more"
    room.spawned()
    assert not room.allow(3).allowed, "two live charges do not"
