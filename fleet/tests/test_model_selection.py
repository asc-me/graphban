"""Naming a model at spawn — GRPH-483, PRD-22 S2.

**This is pass-through, not selection.** PRD-22 §1 says the supervisor "does not choose
models for subagents" and that stands: the caller names the model exactly as it names the
vendor. These tests pin the carrying, and one of them pins that the supervisor adds nothing
of its own when nobody named anything.

The vendors agree on the concept and on nothing else — `--model`, `--model`, `-m` — which
is why the flag lives per-adapter beside `seat_path` and `exit_meaning`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gbfleet.adapters import (
    ADAPTERS,
    Adapter,
    ModelUnsupported,
    Support,
    resolve,
)
from gbfleet.adapters.claude import ClaudeCode
from gbfleet.adapters.cursor import CursorAgent
from gbfleet.adapters.grok import Grok
from gbfleet.seat import Seat
from gbfleet.worktree import Worktree

SEAT = Seat(code="PLANNER-AAAAAA", server_url="http://localhost:8099", api_key="gb_sk_x")


def _tree(tmp_path: Path) -> Worktree:
    return Worktree(path=tmp_path, branch="gb/w-1", repo=tmp_path, base="abc1234")


@pytest.mark.parametrize("adapter,flag", [
    (ClaudeCode(), "--model"),
    (CursorAgent(), "--model"),
    (Grok(), "-m"),
])
def test_each_vendor_gets_its_own_spelling(adapter, flag, tmp_path):
    """Three CLIs, three spellings. `grok --help` puts `-m` first (`-m, --model <MODEL>`)."""
    launch = adapter.launch(SEAT, _tree(tmp_path), tmp_path / "instr", Path("/bin/true"), "zz-model")

    assert flag in launch.argv, launch.argv
    assert launch.argv[launch.argv.index(flag) + 1] == "zz-model"


@pytest.mark.parametrize("adapter", [ClaudeCode(), CursorAgent(), Grok()])
def test_naming_nothing_changes_nothing(adapter, tmp_path):
    """The trap this feature could most easily fall into.

    A test that only asserts `--model x` lands on argv passes just as well if the flag is
    appended unconditionally with an empty value — which would change the command line of
    every existing caller, silently, for a feature they did not ask for. So this asserts
    the DEFAULT path is byte-identical to naming nothing.
    """
    tree, instr, binary = _tree(tmp_path), tmp_path / "instr", Path("/bin/true")

    plain = adapter.launch(SEAT, tree, instr, binary)
    explicit_default = adapter.launch(SEAT, tree, instr, binary, "")

    # Claude mints a fresh temp seat file per launch, so compare with it normalised out.
    def shape(l):
        return [a.replace(str(l.seat_path), "<seat>") for a in l.argv]

    assert shape(plain) == shape(explicit_default)
    assert not any(a in ("--model", "-m") for a in plain.argv), plain.argv
    assert plain.model == ""


@pytest.mark.parametrize("adapter", [ClaudeCode(), CursorAgent(), Grok()])
def test_the_launch_records_which_model_ran(adapter, tmp_path):
    """Carried for the same reason `binary_version` is (S6): a record naming the vendor
    but not the model cannot answer 'was this the cheap one?'."""
    launch = adapter.launch(SEAT, _tree(tmp_path), tmp_path / "i", Path("/bin/true"), "grok-4.5")

    assert launch.model == "grok-4.5"


# ---- validation happens BEFORE a process starts -------------------------------------


class _Listing(Adapter):
    """A vendor that can enumerate, for the refusal path."""

    name = "listing"
    binary = "true"
    support = Support(minimum=(0,))

    def known_models(self, binary):
        return frozenset({"cheap-1", "dear-1"})

    def seat_path(self, worktree):
        return Path(worktree) / "seat.json"


class _Silent(Adapter):
    """A vendor that cannot be asked — the `claude` shape."""

    name = "silent"
    binary = "true"
    support = Support(minimum=(0,))

    def seat_path(self, worktree):
        return Path(worktree) / "seat.json"


class _Entitlementless(Adapter):
    """The `cursor-agent` shape: the listing call works and reports NO models."""

    name = "entitlementless"
    binary = "true"
    support = Support(minimum=(0,))

    def known_models(self, binary):
        # What CursorAgent.known_models returns for "No models available for this account".
        return None

    def seat_path(self, worktree):
        return Path(worktree) / "seat.json"


@pytest.fixture()
def fake_binary(tmp_path_factory):
    """A binary that answers `--version`, so these reach the MODEL gate.

    `/usr/bin/true` prints nothing, `parse_version("")` is `()`, and `Support.permits`
    refuses an unknown version — correctly. The first version of this file used it and
    every model test died on the version check instead of the thing it was about.
    """
    path = tmp_path_factory.mktemp("bin") / "fake-cli"
    path.write_text("#!/bin/sh\necho 1.0.0\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


class _EmptyListing(Adapter):
    """A vendor that CAN list and reports zero models.

    Unreachable through the shipped adapters today — both return `... or None` — but the
    contract in `known_models` says None and an empty set are different answers, and a
    contract nothing exercises is a comment.
    """

    name = "empty-listing"
    binary = "true"
    support = Support(minimum=(0,))

    def known_models(self, binary):
        return frozenset()

    def seat_path(self, worktree):
        return Path(worktree) / "seat.json"


@pytest.fixture()
def registered():
    for a in (_Listing, _Silent, _Entitlementless, _EmptyListing):
        ADAPTERS[a.name] = a()
    yield
    for a in (_Listing, _Silent, _Entitlementless, _EmptyListing):
        ADAPTERS.pop(a.name, None)


def test_a_model_the_vendor_does_not_have_is_refused_before_spawning(registered, fake_binary):
    """The version check refuses before a process starts, and for the same reason: a model
    the vendor rejects burns a registration window and reads as a broken adapter."""
    with pytest.raises(ModelUnsupported) as exc:
        resolve("listing", binary=fake_binary, model="nope-9")

    assert "nope-9" in str(exc.value)
    # Naming what IS available is the difference between a refusal and a dead end.
    assert "cheap-1" in str(exc.value) and "dear-1" in str(exc.value)


def test_a_model_the_vendor_does_have_is_permitted(registered, fake_binary):
    assert resolve("listing", binary=fake_binary, model="cheap-1").adapter.name == "listing"


def test_a_vendor_that_cannot_be_asked_passes_the_model_through(registered, fake_binary):
    """`claude` has no listing flag. Unchecked is not the same as invalid, and refusing
    here would make the feature unusable on the one vendor most likely to want it."""
    assert resolve("silent", binary=fake_binary, model="anything-at-all")


def test_an_account_with_no_entitlements_does_not_refuse_every_spawn(registered, fake_binary):
    """The measured cursor-agent case, and the sharp edge in this whole change.

    `cursor-agent --list-models` answers "No models available for this account" when the
    account has no entitlements. That is a statement about the ACCOUNT, not about the
    model. Treating it as an empty listing would refuse every model on a working setup —
    `None` and `frozenset()` must not collapse into one answer.
    """
    assert resolve("entitlementless", binary=fake_binary, model="composer-2")


def test_a_listing_of_zero_models_does_not_refuse_everything(registered, fake_binary):
    """An account with zero entitlements must not have every spawn refused.

    A listing that comes back empty says "I know of no models", not "every model you could
    name is wrong". `if known and ...` is what keeps those apart; `if known is not None`
    would refuse here, and this is the only test that can tell the two apart.
    """
    assert resolve("empty-listing", binary=fake_binary, model="composer-2")


def test_no_model_named_asks_the_vendor_nothing(registered, fake_binary, monkeypatch):
    """Validation costs a subprocess. Not naming a model must not pay for it."""
    called = []
    monkeypatch.setattr(_Listing, "known_models", lambda self, b: called.append(b) or frozenset())

    resolve("listing", binary=fake_binary)

    assert called == [], "the listing was fetched for a spawn that named no model"


# ---- the real binaries, where they are installed ------------------------------------


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_a_real_binary_lists_or_says_it_cannot(name):
    """Against the installed CLI, because a listing parser written from a docstring is a
    guess. Skips where the vendor is absent, the same as `test_a_real_installed_binary_resolves`.
    """
    import shutil

    adapter = ADAPTERS[name]
    found = shutil.which(adapter.binary)
    if not found:
        pytest.skip(f"{adapter.binary} is not installed on this machine")

    known = adapter.known_models(Path(found))

    # Either it enumerated something real, or it said it could not. Never an empty set.
    assert known is None or (known and all(known)), known


# ---- per-vendor tuning (GRPH-484) ---------------------------------------------------
#
# Unlike the model, these are NOT uniform: only claude takes a fallback list, only grok
# takes a reasoning effort. Both exist because a spawned child is UNATTENDED — nobody is
# there to notice an overloaded model, or to raise the effort when an answer comes back
# thin. An adapter declares which it has and `resolve` refuses the rest BY NAME.


def test_claude_carries_a_fallback_list(tmp_path):
    """The knob that turns a dead spawn into a slow one.

    An overloaded model on an interactive session is a wait. On an unattended child it is a
    dead registration window: the process starts, cannot get a model, never registers, and
    the supervisor reports the ADAPTER as broken.
    """
    from gbfleet.adapters import Tuning

    launch = ClaudeCode().launch(
        SEAT, _tree(tmp_path), tmp_path / "i", Path("/bin/true"),
        "opus", Tuning(fallback_model="sonnet,haiku"),
    )

    assert "--fallback-model" in launch.argv
    assert launch.argv[launch.argv.index("--fallback-model") + 1] == "sonnet,haiku"


def test_grok_carries_a_reasoning_effort(tmp_path):
    from gbfleet.adapters import Tuning

    launch = Grok().launch(
        SEAT, _tree(tmp_path), tmp_path / "i", Path("/bin/true"), "", Tuning(effort="high"),
    )

    assert "--reasoning-effort" in launch.argv
    assert launch.argv[launch.argv.index("--reasoning-effort") + 1] == "high"


@pytest.mark.parametrize("adapter", [ClaudeCode(), CursorAgent(), Grok()])
def test_tuning_nothing_changes_nothing(adapter, tmp_path):
    """Same trap as the model: a knob appended unconditionally with an empty value would
    rewrite the command line of every existing caller for a feature they did not ask for."""
    from gbfleet.adapters import Tuning

    tree, instr, binary = _tree(tmp_path), tmp_path / "i", Path("/bin/true")
    plain = adapter.launch(SEAT, tree, instr, binary)
    empty = adapter.launch(SEAT, tree, instr, binary, "", Tuning())

    def shape(l):
        return [a.replace(str(l.seat_path), "<seat>") for a in l.argv]

    assert shape(plain) == shape(empty)
    assert not any(a in ("--fallback-model", "--reasoning-effort") for a in plain.argv)


@pytest.mark.parametrize("vendor,knob,offers", [
    ("silent", "effort", "no tuning knobs"),
    ("listing", "fallback_model", "no tuning knobs"),
])
def test_a_knob_the_vendor_lacks_is_refused_not_ignored(registered, fake_binary, vendor, knob, offers):
    """Refused, never silently dropped.

    Ignoring it would let a caller believe it asked for cheap reasoning and pay for
    expensive — the setting evaporates and the bill does not. One clear error is cheaper
    than a wrong invoice nobody can explain afterwards.
    """
    from gbfleet.adapters import Tuning, TuningUnsupported

    with pytest.raises(TuningUnsupported) as exc:
        resolve(vendor, binary=fake_binary, tuning=Tuning(**{knob: "x"}))

    assert knob in str(exc.value)
    assert offers in str(exc.value), "the refusal must say what this vendor DOES accept"


def test_the_shipped_adapters_declare_what_they_really_have():
    """Pins the split against the binaries' own --help, so a knob cannot be quietly moved
    onto a vendor that has no such flag."""
    assert ClaudeCode.tuning == frozenset({"fallback_model"})
    assert Grok.tuning == frozenset({"effort"})
    assert CursorAgent.tuning == frozenset(), "cursor-agent has neither flag"
