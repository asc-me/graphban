"""A roster retires what cannot come back (GRPH-814).

Reported as "31 offline agents, nothing prunes them". Measured on the live instance while
fixing GRPH-807: **177 agents, of which one was live** — so this is not tidiness, it is the
dominant term in the roster's size and it inflates everything that reads one.

DERIVED, never swept. `presence_state` is computed on read for a stated reason — "there is no
sweep to forget to run, and no window in which the roster shows green for a process that
stopped an hour ago" — and a retirement sweeper would reintroduce exactly that.

THE SEAT TTL IS THE ANCHOR. A seat is single-use and lives 30 minutes, so an agent silent for
longer cannot resume: anything that came back would register on a new seat and be a new row.
That is what makes this a claim rather than a guess, and it is why a retired agent may be
dropped from a narrowed view while an offline one may not — offline says we have not heard
from it, retired says the thing it would need is gone.
"""
from datetime import timedelta

import pytest

from app.services import fleet as fleet_svc
from app.services import items as items_svc


class _Agent:
    def __init__(self, *, ago=None, dismissed=False):
        self.last_seen_at = None if ago is None else items_svc.utcnow() - timedelta(seconds=ago)
        self.dismissed_at = items_svc.utcnow() if dismissed else None


TTL = fleet_svc.ENROLMENT_TTL_MINUTES * 60


# ---- when an agent counts as gone for good --------------------------------------------------

def test_silent_for_longer_than_a_seat_can_live_is_retired():
    assert fleet_svc.retired(_Agent(ago=TTL + 60)) is True


def test_silent_for_less_is_not():
    """Inside the seat's life it can still resume, so retiring it would delete a live worker
    from the roster its planner is reading."""
    assert fleet_svc.retired(_Agent(ago=TTL - 60)) is False


def test_merely_offline_is_not_retired():
    """The distinction the whole design rests on. Offline is 150s; retired is 30 minutes."""
    assert fleet_svc.retired(_Agent(ago=fleet_svc.presence_ttl_seconds() + 10)) is False


def test_never_seen_is_not_retired():
    """An agent registers before its first heartbeat. Calling that gone would retire every
    child in the second between the two."""
    assert fleet_svc.retired(_Agent(ago=None)) is False


def test_dismissed_is_retired_whatever_the_clock_says():
    """Somebody decided. A fresh heartbeat does not un-decide it."""
    assert fleet_svc.retired(_Agent(ago=1, dismissed=True)) is True


# ---- and what a roster does with it -----------------------------------------------------------

def _payload(rows):
    return {"agents": rows, "seats": []}


def _row(agent_id, *, state="idle", retired=False):
    return {"id": agent_id, "state": state, "retired": retired, "holdings": []}


def test_a_narrowed_roster_drops_them(dummy=None):
    got = fleet_svc.roster_view(_payload([_row("A1"), _row("A2", retired=True)]), "lean")

    assert [a["id"] for a in got["agents"]] == ["A1"]


def test_full_still_carries_them():
    """The escape hatch for anybody reading history."""
    payload = _payload([_row("A1"), _row("A2", retired=True)])

    assert len(fleet_svc.roster_view(payload, "full")["agents"]) == 2


def test_the_reply_counts_what_it_dropped():
    """A roster that shrank from 177 to 1 with no explanation is the same class of surprise as
    one that never shrank."""
    got = fleet_svc.roster_view(_payload([_row("A1"), _row("A2", retired=True),
                                          _row("A3", retired=True)]), "lean")

    assert got["retired"] == 2


def test_an_offline_agent_is_still_listed_by_lean():
    """GRPH-807's rule, unchanged: fewer FIELDS is a different act from fewer AGENTS, and an
    offline agent may yet come back."""
    got = fleet_svc.roster_view(_payload([_row("A1", state="offline")]), "lean")

    assert [a["id"] for a in got["agents"]] == ["A1"]
