"""A missing adapter argument refuses before the child starts (GRPH-813).

Reported from super-arc: gbagent's `--turns`/`--window` are required, and omitting them
exited 2 on an argparse error — a raw usage dump in a child's stderr, *before it registered*.

On the board that is not "bad arguments". A child that dies before registering leaves a
delegation reading `expired, nothing claimed`, which reads as a dead model rather than a
missing flag — so the operator debugs the wrong thing.

`AdapterError` is the type `spawn` already catches and reports, so the refusal reaches the
caller who can fix it instead of the log of a process that is already gone.
"""
from __future__ import annotations

import pytest

from gbfleet.adapters import AdapterError, Tuning
from gbfleet.adapters.gbagent import GbAgent, MissingTuning


def _argv(**kw):
    """The CHECK, not the argv builder. Inspecting a launch is not spawning one, and the
    first version of this raised from `tuning_argv` — which broke every caller that builds a
    launch to look at it."""
    return GbAgent().check_tuning(Tuning(**kw))


def test_both_present_is_the_happy_path():
    assert _argv(turns=40, window=262144) is None


def test_building_a_launch_to_inspect_it_is_not_a_spawn():
    """The separation this rests on. A rule that fired on `tuning_argv` could not tell an
    inspection from a spawn, and broke nine tests that only wanted to read argv."""
    assert GbAgent().tuning_argv(Tuning()) == []


@pytest.mark.parametrize("missing,given", [
    ("turns", {"window": 262144}),
    ("window", {"turns": 40}),
])
def test_a_missing_one_is_refused_by_name(missing, given):
    """By NAME. "gbagent needs arguments" sends somebody to the help text; naming the flag
    ends the question."""
    with pytest.raises(MissingTuning) as exc:
        _argv(**given)

    assert f"--{missing}" in str(exc.value)


def test_neither_names_both():
    with pytest.raises(MissingTuning) as exc:
        _argv()

    said = str(exc.value)
    assert "--turns" in said and "--window" in said


def test_the_refusal_says_why_it_will_not_guess():
    """The reason is the decision. Without it the next person adds a default and gets a
    fleet that compacts constantly or dies of overflow, with nothing explaining either."""
    with pytest.raises(MissingTuning) as exc:
        _argv()

    said = str(exc.value)
    assert "too large" in said and "too small" in said


def test_it_is_an_adapter_error_so_spawn_reports_it():
    """The type is the whole delivery mechanism: `spawn` catches `AdapterError` and turns it
    into an answer. A bespoke exception would escape as a traceback."""
    assert issubclass(MissingTuning, AdapterError)

    with pytest.raises(AdapterError):
        _argv()


# ---- the lock explains the choice it is forcing (GRPH-811) --------------------------------------

def test_the_lock_refusal_names_both_modes(tmp_path):
    """Reported as "the supervisor lock forces a choice between MCP mode and until". It does,
    and that is correct — both spawn children into one repository, which is what the lock
    keeps to one. What was missing is that the message named neither of the two things the
    reader is choosing between, so a correct refusal read as a limitation.
    """
    from gbfleet.lock import Holder, RepoLocked

    said = str(RepoLocked(tmp_path / "lock", Holder(pid=1, repo="/r", acquired_at="now",
                                                    version="0.2.0")))

    assert "gbfleet mcp" in said and "gbfleet until" in said
    assert "supervision modes" in said
    assert "own checkout" in said, "refused without naming the arrangement that works"


def test_it_still_names_the_holder_and_the_lock_file(tmp_path):
    """The original job of the message. An explanation that displaced the pid would trade one
    unanswerable question for another."""
    from gbfleet.lock import Holder, RepoLocked

    said = str(RepoLocked(tmp_path / "lock", Holder(pid=4242, repo="/r", acquired_at="now",
                                                    version="0.2.0")))

    assert "4242" in said and "lock" in said
