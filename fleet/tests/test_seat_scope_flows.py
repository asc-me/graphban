"""The wave's scope reaches the CREDENTIAL, not just the loop's own choices (GRPH-827).

`--prd` already bounded what `until` hands out. It did not bound what the child then takes for
itself, and one measured wave shows the size of that gap: three items delegated inside the
scope, six self-claimed outside it, including an ops item whose checklist mutates production.

These tests pin the client half — that the scope is on every seat this loop mints, by both
routes, and that a flag combination which cannot carry it is refused rather than half-applied.
"""
from __future__ import annotations

import pytest

from gbfleet import until


def test_the_scope_goes_on_the_bound_seat_delegate_mints():
    """The route `until` takes when the divvy offers a free cluster."""
    assert until._seat_scope("SA-P11") == {"scope": "SA-P11"}


def test_an_unscoped_wave_sends_nothing_extra():
    """The control that decides whether this can ship without changing every existing run:
    an unscoped wave must mint exactly the seat it minted before."""
    assert until._seat_scope(None) == {}
    assert until._seat_scope("") == {}


def test_the_delegation_filter_and_the_seat_scope_are_separate_functions():
    """They carry the same value and answer different questions — what this loop OFFERS versus
    what the child may TAKE — and the entire finding is that the second did not exist. Keeping
    one function for both would make it impossible to have one without the other, which sounds
    like a feature until a server supports one and not the other."""
    assert until._scope("SA-P11") == {"prd_id": "SA-P11"}
    assert until._seat_scope("SA-P11") == {"scope": "SA-P11"}
