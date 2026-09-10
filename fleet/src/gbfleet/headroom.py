"""Whether this machine can hold another child (GRPH-842).

The supervisor could count seats, workers, children and wall-clock, and could not count
the one resource that actually ran out. A wave launched from a Claude Code background
task was stopped with *"Background command was stopped because the system is running low
on memory"* — the harness's monitor reacting to SYSTEM-WIDE pressure by killing the
process it happens to own, which was the supervisor, which took its children with it.

**The children were not the load, and the numbers matter here.** Measured 2026-09-10 on
the 24 GB box where this happened: 11 live `claude` processes came to 2.5 GB resident,
mean 0.23 GB, and the largest read `phys_footprint: 596 MB` / `peak: 671 MB`. Chrome,
Arc and Cursor together held 7.3 GB, and with *no* fleet children running at all the box
read `23G used, 207M unused`. Three children is about 2 GB. The wave was the straw and
not the load — so a gate that guesses a per-child cost and subtracts it from a total is
solving the wrong problem. This one asks the kernel what is actually free, every time.

**It never blocks the first child**, deliberately. A wave that spawns nothing produces
nothing, and a bad reading would then cost the operator everything rather than one slot.
One child is also the sequential fallback the fleet has always had. So the gate binds
from the second child on, where the harness kill actually happened.

**A reading of `None` does not bind.** `hostos.available_memory` returns three answers,
and "this host cannot be asked" must not spawn silently into an unknown *or* ground the
fleet — the same rule `is_owner_only` follows. It reports, and `doctor` shows UNKNOWN.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from . import hostos

#: Bytes left unclaimed no matter what fits. Not superstition: the figure this divides is
#: an ESTIMATE that counts reclaimable pages (Linux `MemAvailable`, macOS inactive +
#: speculative), and reclaiming them is work rather than magic. It also has to cover the
#: supervisor itself, the vendor's own helper processes, and whatever the operator does
#: on the machine while the wave runs. 2 GB is a little over three measured children.
RESERVE = 2 * 1024**3

#: What one child is charged against the budget, and what `Limits.child_memory` defaults
#: to. From the measurement above: 671 MB peak `phys_footprint` for the largest live
#: Claude Code process, rounded up. A per-child figure is unavoidable — the question
#: "does another one fit" cannot be answered without one — but it is used only to divide
#: a LIVE reading, never to model total usage.
DEFAULT_CHILD_MEMORY = 700 * 1024 * 1024

#: How long a spawned child is charged against the reading before the kernel is trusted to
#: count it. A vendor CLI is resident and allocating well inside this; the cost of it being
#: too long is one delayed spawn, and of too short is the stale-reading bug the charge
#: exists to prevent, so it errs long.
SETTLE = 30.0


@dataclass(frozen=True)
class Verdict:
    """One decision, with the numbers that produced it.

    `fits` is `None` for "not measured", never 0 — the whole point of the module below.
    """

    allowed: bool
    reason: str = ""
    available: int | None = None
    fits: int | None = None


def gigabytes(byte_count: int | None) -> str:
    return "unmeasured" if byte_count is None else f"{byte_count / 1024**3:.1f} GB"


def fits(available: int | None, per_child: int, *, reserve: int = RESERVE) -> int | None:
    """How many children of `per_child` bytes this much memory holds. None if unmeasured."""
    if available is None:
        return None
    if per_child <= 0:
        return None
    return max(0, (available - reserve) // per_child)


class Headroom:
    """The live reading, discounted for children too young to appear in it yet.

    A spawned Claude Code process does not reach its footprint the instant `Popen` returns,
    so a reading taken between two spawns in the same loop undercounts the one just started
    — and the loop would happily start four children on the strength of a reading taken
    before any of them allocated anything. That is how three ended up on a box with room for
    one. Each spawn is therefore charged `per_child` against the reading, and the charge
    EXPIRES after `SETTLE` seconds, by which time the kernel is counting the child itself.

    Expiry rather than a running total, because this object outlives one launch loop:
    `until` spawns one seat at a time for as long as there is ready work, and a charge that
    never lapsed would make an hour-long run refuse everything long after the children it
    was charged for had exited.
    """

    def __init__(
        self,
        per_child: int = DEFAULT_CHILD_MEMORY,
        *,
        reserve: int = RESERVE,
        settle: float = SETTLE,
        read: Callable[[], int | None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.per_child = per_child
        self.reserve = reserve
        self.settle = settle
        # Resolved at CALL time, not bound as a default. `read=hostos.available_memory` in
        # the signature captures the function this module imported, so anything that
        # replaces it afterwards — a test, a probe, a platform shim — is ignored while the
        # gate quietly keeps asking the original. `doctor.run` carries the same note about
        # `out=sys.stdout` for the same reason.
        self._read = hostos.available_memory if read is None else read
        self._clock = time.monotonic if clock is None else clock
        self._charges: list[float] = []
        self._baseline = self._read()

    @property
    def baseline(self) -> int | None:
        """What the machine had when this object was made. None if it could not be asked.

        Reported, never used for a decision — every verdict comes from a fresh reading. It
        exists so a wave can say what it was looking at, because `gated: []` on its own
        cannot distinguish "nothing was refused" from "nothing was measured".
        """
        return self._baseline

    def spawned(self) -> None:
        """Charge one child. Called AFTER a spawn, not before — a launch that failed costs
        nothing and must not shrink the budget for the seats behind it."""
        self._charges.append(self._clock())

    def _unsettled(self) -> int:
        now = self._clock()
        self._charges = [at for at in self._charges if now - at < self.settle]
        return len(self._charges)

    def _estimate(self) -> int | None:
        live = self._read()
        if live is None:
            return None
        return live - self.per_child * self._unsettled()

    def allow(self, running: int) -> Verdict:
        """Whether to start one more, given how many are already running here."""
        available = self._estimate()
        room = fits(available, self.per_child, reserve=self.reserve)
        if running <= 0:
            # Said as a reason rather than left implicit: an operator reading "allowed" on a
            # machine with 200 MB free deserves to know it was not a measurement.
            return Verdict(True, "first child is never gated", available, room)
        if room is None:
            return Verdict(
                True, "memory not measurable on this host; gate did not bind",
                available, room,
            )
        if room <= 0:
            return Verdict(
                False,
                f"{gigabytes(available)} available, {gigabytes(self.reserve)} reserved: "
                f"no room for another {gigabytes(self.per_child)} child",
                available, room,
            )
        return Verdict(True, "", available, room)
