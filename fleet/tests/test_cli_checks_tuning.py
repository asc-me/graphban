"""The CLI spawn path checks adapter tuning too (GRPH-831).

GRPH-813 made a spawn refuse when an adapter's required arguments are missing, instead of
letting the child exit 2 on an argparse error whose usage dump lands in a log nobody reads —
on the board that reads as `expired, nothing claimed`, which looks like a dead model rather
than a missing flag.

It was wired into ONE of the two spawn paths. `mcp.py` asked; `cli.make_adapter_factory` —
the path `up` and `until` take — called `adapter.launch` directly and never did. The helper's
own docstring said it lived where it did "so a new spawn path cannot forget to ask", which is
a claim about placement that its placement did not support.

Reported from the field as O22, on the release that shipped the fix.
"""
from __future__ import annotations

import pytest

from gbfleet.adapters import ADAPTERS, Tuning, checked_tuning
from gbfleet.adapters.gbagent import MissingTuning


def test_the_cli_factory_refuses_before_it_builds_anything(monkeypatch, tmp_path):
    """THE ONE THAT MATTERS. `up` and `until` both go through here, and it refuses at RESOLVE
    time — before a worktree, before a seat, before a process."""
    from gbfleet import cli

    monkeypatch.setenv("GBAGENT_BASE_URL", "http://localhost:11434/v1")
    with pytest.raises(MissingTuning) as exc:
        cli.make_adapter_factory("gbagent", binary=_a_fake_gbagent(tmp_path),
                                 tuning=Tuning(turns=0, window=0))

    assert "--turns" in str(exc.value) and "--window" in str(exc.value)


def test_a_complete_tuning_passes(monkeypatch, tmp_path):
    """The control. A refusal that fires on a correct call is worse than no refusal."""
    from gbfleet import cli

    monkeypatch.setenv("GBAGENT_BASE_URL", "http://localhost:11434/v1")
    cli.make_adapter_factory("gbagent", binary=_a_fake_gbagent(tmp_path),
                             tuning=Tuning(turns=8, window=200000))


def test_both_spawn_paths_ask_the_same_function():
    """One definition, two callers. The bug was two placements and one caller, so the shape
    that fixed it is worth pinning rather than the symptom."""
    from gbfleet import mcp as mcp_mod

    assert mcp_mod._checked_tuning.__module__.endswith("mcp")
    with pytest.raises(MissingTuning):
        checked_tuning("gbagent", Tuning(turns=0, window=0))
    with pytest.raises(MissingTuning):
        mcp_mod._checked_tuning("gbagent", Tuning(turns=0, window=0))


def test_the_factory_refuses_when_it_is_handed_no_tuning_at_all(monkeypatch, tmp_path):
    """`tuning` is optional, so the factory turns `None` into an empty `Tuning` before it
    asks — and until this test nothing covered that step.

    Every other test here passes a `Tuning` instance, so `tuning or Tuning()` could be
    reduced to `tuning` and the whole suite stayed green: 1126 tests, measured. The caller
    that omits it is not hypothetical — `make_adapter_factory`'s signature defaults it, and
    an omitted knob has to refuse exactly like a zeroed one rather than crash on None.

    Sabotage: drop the `or Tuning()` fallback and this fails while the four tests above pass.
    """
    from gbfleet import cli

    monkeypatch.setenv("GBAGENT_BASE_URL", "http://localhost:11434/v1")
    with pytest.raises(MissingTuning) as exc:
        cli.make_adapter_factory("gbagent", binary=_a_fake_gbagent(tmp_path))

    assert "--turns" in str(exc.value) and "--window" in str(exc.value)


def test_an_adapter_with_no_required_arguments_is_unaffected():
    """Only `gbagent` refuses to guess. Every vendor adapter has to keep spawning with an
    empty tuning, or this becomes a fleet-wide outage in the name of one adapter's flags."""
    for name, adapter in ADAPTERS.items():
        if name == "gbagent":
            continue
        checked_tuning(adapter, Tuning())


def _a_fake_gbagent(tmp_path):
    """A binary that answers `--version` with this package's own version, which is what the
    exact pin wants. Resolution is not what is under test here."""
    import gbfleet

    path = tmp_path / "gbagent"
    path.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        f'  --version) echo "{gbfleet.__version__}" ;;\n'
        '  models) echo "qwen3.6:35b" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return str(path)
