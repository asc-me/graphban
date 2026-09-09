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


# ---- and why those clusters are clusters (GRPH-810) --------------------------------------------

def _because(rule):
    return [{"items": ["SA-1", "SA-2"], "on": [{"a": "x/a.py", "b": "x/b.py", "rule": rule}]}]


def test_a_wait_caused_only_by_the_directory_rule_says_so():
    """A wait is easier to judge when you know what you are waiting for: real overlap, or an
    artefact of the grouping rule."""
    held = _cluster(["SA-1"], held=["SA-A2"], free_in=60)
    held["because"] = _because("directory")

    said = _waiting([held])

    assert "share a directory" in said


def test_a_real_overlap_is_not_explained_away():
    """The control, and the direction that matters. Reporting a genuine overlap as directory
    noise would talk an operator out of a wait they should take seriously."""
    held = _cluster(["SA-1"], held=["SA-A2"], free_in=60)
    held["because"] = _because("exact")

    assert "share a directory" not in _waiting([held])


def test_a_mixture_is_not_called_directory_noise():
    held = _cluster(["SA-1"], held=["SA-A2"])
    held["because"] = [{"items": ["SA-1", "SA-2"],
                        "on": [{"a": "x/a.py", "b": "x/b.py", "rule": "directory"},
                               {"a": "x/a.py", "b": "x/a.py", "rule": "exact"}]}]

    assert "share a directory" not in _waiting([held])


def test_no_reasons_at_all_says_nothing_extra():
    """An older server sends no `because`. Silence is the honest answer, not a guess."""
    assert "share a directory" not in _waiting([_cluster(["SA-1"], held=["SA-A2"])])


# ---- 566 of 575 log lines were this message (GRPH-817) -------------------------------------

def _blocked(holders=("SA-A2",), free_in=172, n=1):
    return [_cluster([f"SA-{i}"], held=list(holders), free_in=free_in) for i in range(n)]


def test_the_same_contention_is_reported_once_not_every_tick():
    """THE REGRESSION. This loop polls once a second, so the first version wrote 566 of a
    wave's 575 lines — and the nine that mattered were in there somewhere."""
    from gbfleet.until import _Repeats, _holders_key

    seen = _Repeats()
    clock = [1000.0]
    said = [seen.changed(_holders_key(_blocked()), now=lambda: clock[0]) for _ in range(60)]

    assert said[0] is True
    assert not any(said[1:]), "reported the same contention more than once"


def test_the_dedup_key_ignores_the_countdown():
    """The trap this fix nearly walked into. `_waiting` embeds seconds that tick every poll,
    so deduplicating on the MESSAGE compares two strings that always differ — the fix
    reintroducing the bug through its own dedup key."""
    from gbfleet.until import _holders_key

    assert _holders_key(_blocked(free_in=172)) == _holders_key(_blocked(free_in=9))


def test_a_new_holder_is_reported_immediately():
    from gbfleet.until import _Repeats, _holders_key

    seen = _Repeats()
    clock = [1000.0]
    seen.changed(_holders_key(_blocked(("SA-A2",))), now=lambda: clock[0])

    assert seen.changed(_holders_key(_blocked(("SA-A9",))), now=lambda: clock[0]) is True


def test_a_long_wait_still_heartbeats():
    """Silence for ten minutes and silence because the supervisor died look the same."""
    from gbfleet.until import _HEARTBEAT, _Repeats, _holders_key

    seen = _Repeats()
    clock = [1000.0]
    key = _holders_key(_blocked())
    seen.changed(key, now=lambda: clock[0])
    clock[0] += _HEARTBEAT + 1

    assert seen.changed(key, now=lambda: clock[0]) is True


def test_a_repeat_says_the_lease_was_renewed():
    """A countdown that resets when a holder renews — which is what a working agent does —
    reads as a hang. The line says the wait is still live rather than leaving the reader to
    infer it from a figure that went the wrong way."""
    assert "still held" in _waiting(_blocked(), repeat=1)
    assert "still held" not in _waiting(_blocked())


def test_clearing_lets_the_next_occurrence_report_at_once():
    """A wave that contends, clears, and contends again must not inherit the first one's
    timer and go quiet."""
    from gbfleet.until import _Repeats, _holders_key

    seen = _Repeats()
    clock = [1000.0]
    key = _holders_key(_blocked())
    seen.changed(key, now=lambda: clock[0])
    seen.clear()

    assert seen.changed(key, now=lambda: clock[0]) is True
