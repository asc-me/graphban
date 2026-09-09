"""A wave waits for contention instead of spending its children on it (GRPH-803).

Reported from super-arc: wave 2 spawned all ten children into fully-reserved clusters. Each
registered, found nothing, exited. `{"ok": false, "reason": "cap", "spawned": 10, "minted": 0}`
— a wave that failed purely by arriving early, and burned its whole budget doing so.

The supervisor sized itself off `total`, which counts every cluster including the ones whose
areas are held. `_delegate_next` has always skipped clusters carrying `held_by`; nothing ever
set it, so that guard never fired once. Both halves are fixed: the server marks held clusters,
and this stops counting them as capacity.
"""
from __future__ import annotations

from gbfleet.until import _free_and_blocked, _waiting


def _cluster(items, held=None, free_in=None):
    out = {"items": items, "areas": items}
    if held:
        out["held_by"] = held
    if free_in is not None:
        out["free_in"] = free_in
    return out


def test_held_clusters_are_not_capacity():
    """THE REGRESSION. Ten held clusters used to read as ten workers' worth of work."""
    free, blocked = _free_and_blocked(
        {"clusters": [_cluster(["SA-1"], held=["SA-A2"]), _cluster(["SA-2"], held=["SA-A3"])]})

    assert free == []
    assert len(blocked) == 2


def test_free_clusters_still_are():
    free, blocked = _free_and_blocked({"clusters": [_cluster(["SA-1"])]})

    assert len(free) == 1 and blocked == []


def test_a_mixed_divvy_splits():
    free, blocked = _free_and_blocked(
        {"clusters": [_cluster(["SA-1"]), _cluster(["SA-2"], held=["SA-A9"])]})

    assert [c["items"] for c in free] == [["SA-1"]]
    assert [c["items"] for c in blocked] == [["SA-2"]]


def test_an_empty_divvy_is_neither():
    assert _free_and_blocked({"clusters": []}) == ([], [])
    assert _free_and_blocked({}) == ([], [])


def test_a_malformed_cluster_is_ignored_rather_than_counted():
    """A non-dict entry counted as free is a spawn into nothing."""
    free, blocked = _free_and_blocked({"clusters": ["nonsense", None, _cluster(["SA-1"])]})

    assert len(free) == 1 and blocked == []


# ---- what it tells the operator ---------------------------------------------------------------

def test_the_wait_names_the_holder_and_the_time():
    """"Wait" and "give up" are different instructions, and nobody can choose without both."""
    said = _waiting([_cluster(["SA-1"], held=["SA-A2"], free_in=172)])

    assert "SA-A2" in said
    assert "172s" in said
    assert "cannot be claimed" in said


def test_it_reports_the_soonest_not_the_last():
    said = _waiting([_cluster(["SA-1"], held=["A"], free_in=600),
                     _cluster(["SA-2"], held=["B"], free_in=90)])

    assert "90s" in said and "600s" not in said


def test_a_missing_expiry_says_so_rather_than_implying_forever():
    """No expiry reported is not "never frees". Printing nothing would leave an operator
    reading an unbounded wait into a hold that may lapse in seconds."""
    said = _waiting([_cluster(["SA-1"], held=["SA-A2"])])

    assert "no expiry reported" in said
    assert "SA-A2" in said
