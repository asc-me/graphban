"""GRPH-1372 (GRPH-P72): a wave continues when one account is spent.

A `VendorLimit` marks the stored record the supervisor launched as exhausted, in this
process's memory only, and the SAME seat is started again as a new process: on the next
record for that vendor once, then — on the vendor's second limit — on the next vendor the
matrix resolves. Nothing left ends the wave `capacity`.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from gbfleet import entitlements as ent
from gbfleet import matrix as matrix_mod
from gbfleet.adapters import ADAPTERS
from gbfleet.cli import main
from gbfleet.spawn import Launch, VendorLimit
from gbfleet.supervisor import Limits
from gbfleet.until import run

from tests.test_supervisor import KEY, _factory
from tests.test_until import _clients

SAID = "You've hit your session limit · resets 12:20pm (America/New_York)"

MATRIX = """
[[rows]]
harness = "claude"
model = "sonnet"
vendor = "anthropic"
lane = "any"
tier = "cheap"
status = "unverified"
order = 1
cost_class = "cheap"
local = false
evidence = []

[[rows]]
harness = "grok"
model = "grok-code"
vendor = "xai"
lane = "any"
tier = "cheap"
status = "unverified"
order = 2
cost_class = "cheap"
local = false
evidence = []

[[rows]]
harness = "gbagent"
model = "local"
vendor = "gbagent"
lane = "any"
tier = "cheap"
status = "unverified"
order = 3
cost_class = "cheap"
local = true
evidence = []
"""


def _limited(seat, tree, instruction_file, debug_file=None):
    raise VendorLimit("out of quota", adapter="claude", said=SAID)


@pytest.fixture
def matrix(tmp_path, monkeypatch):
    """claude, then grok, then local gbagent — the ladder, every harness installed."""
    monkeypatch.setattr(matrix_mod, "installed_checker",
                        lambda *a, **k: (lambda row: (True, "")))
    _excluding(monkeypatch)
    path = tmp_path / "matrix.toml"
    path.write_text(MATRIX)
    return matrix_mod.load(path)


def _excluding(monkeypatch, *harnesses):
    """A key owner's profile that excludes `harnesses`, read the way the wave reads it.

    Only cost is weighed (all rows equal), so the matrix's own order decides: without that a local row
    outscores every vendor and the ladder is upside down.
    """
    from gbfleet import mcp

    real = getattr(mcp.read_preferences, "real", mcp.read_preferences)

    def patched(client):
        got = real(client)
        return (matrix_mod.Profile(user="u", weights={"cost": 1.0},
                                   excludes=tuple(harnesses)), *got[1:])

    patched.real = real

    monkeypatch.setattr(mcp, "read_preferences", patched)


def _wave(git_repo, tmp_path, state, matrix, scripts, *, limited, records,
          items=(["GRPH-7"],), minted_roles=None, max_workers=1):
    """Run one wave. `limited(adapter, record)` decides whether that launch hits the limit."""
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(workspace, clusters=len(items), cluster_items=list(items),
                                   minted_roles=minted_roles, workers=max_workers,
                                   sticky_clusters=len(items) > 1,
                                   drop_delegated=len(items) > 1)
    good = _factory(scripts, "works_then_exits")
    asked: list[tuple[str, str]] = []

    def launch_for(adapter, model="", entitlement=None):
        # Recorded when the factory STARTS a process, not when it is built: the resolver
        # builds one before the record is chosen, and that one never runs.
        def started(*args, **kwargs):
            asked.append((adapter, entitlement.name if entitlement else ""))
            factory = _limited if limited(adapter, entitlement) else good
            return factory(*args, **kwargs)
        return started

    result = run(
        git_repo, good, planner, supervisor, api_key=KEY, server="http://gb.invalid",
        adapter="fake", state=state, workspace=workspace, poll=0, sleep=lambda _: None,
        empty_ticks=3 if len(items) > 1 else 1, request="cheap",
        limits=Limits(max_workers=max_workers, max_children=max(len(items), 1)),
        launch_for=launch_for, matrix=matrix, records=records,
    )
    return result, asked


@pytest.fixture
def wave(git_repo, tmp_path, state, matrix, scripts):
    def go(**kw):
        return _wave(git_repo, tmp_path, state, matrix, scripts, **kw)
    return go


def _sub(name, vendor="claude", handle=None):
    return ent.Entitlement(name, vendor, "subscription", handle or f"/cfg/{name}")


def _spent(*names):
    return lambda adapter, rec: rec is not None and rec.name in names


# --- the first limit tries one other record on the same vendor, on the same seat -----------

def test_a_spent_record_retries_the_same_seat_on_the_other_record(wave):
    """Sabotage: make the first VendorLimit set `vendor_out` (skip the second-record branch)
    and the retry goes to grok, so `asked` is [a, grok]."""
    minted: list = []
    result, asked = wave(limited=_spent("a"), records=[_sub("a"), _sub("b")],
                         minted_roles=minted)
    assert result.reason != "capacity", result.detail
    assert asked == [("claude", "a"), ("claude", "b")]
    assert result.spawned == 1
    # Same enrolment: one seat minted for one item, not a second for the retry.
    assert len(minted) == 1
    assert result.wave.unused_seats == 0
    assert not result.wave.unredeemed
    assert not result.wave.failures
    # The failed process's worktree was reaped, not left behind.
    assert len(result.wave.reaped) >= 1
    payload = result.as_json()
    assert [e["entitlement"] for e in payload["exhausted"]] == ["a"]
    assert payload["vendors_launched"] == ["claude"]
    assert payload["vendors_not_launched"] == []


def test_the_second_record_is_selected_by_its_own_handle(wave, monkeypatch):
    """The retry passes record b to the adapter, so the child starts with b's config dir."""
    records = [_sub("a", handle="/cfg/one"), _sub("b", handle="/cfg/two")]
    got: list = []

    def launch():
        return Launch(adapter="claude", argv=["claude"], seat_path=Path("/x"), config={},
                      instruction="")

    def limited(adapter, rec):
        if rec is not None:
            got.append(ADAPTERS["claude"].select_entitlement(launch(), rec).env)
        return rec is not None and rec.name == "a"

    wave(limited=limited, records=records)
    assert [e["CLAUDE_CONFIG_DIR"] for e in got] == ["/cfg/one", "/cfg/two"]


def test_a_second_limit_stops_the_vendor_and_the_next_child_is_another_vendor(wave):
    """Sabotage: change `hits >= 2` to `hits >= 3` and record c is started — a third
    record on a vendor whose limit is still in force."""
    records = [_sub("a"), _sub("b"), _sub("c"),
               ent.Entitlement("g", "grok", "subscription", "/cfg/g")]
    result, asked = wave(limited=_spent("a", "b", "g"), records=records)
    assert asked[:2] == [("claude", "a"), ("claude", "b")]
    assert ("claude", "c") not in asked
    # The vendor is excluded and the matrix resolved again: grok next, then gbagent.
    assert [a for a, _ in asked[2:]] == ["grok", "gbagent"]
    assert result.reason != "capacity", result.detail
    assert result.as_json()["vendors_launched"] == ["gbagent"]
    assert result.as_json()["vendors_not_launched"] == ["claude", "grok"]


def test_the_ladder_ends_at_local_gbagent_with_a_local_record(wave):
    records = [_sub("a"), _sub("b"), ent.Entitlement("here", "gbagent", "local", "")]
    result, asked = wave(limited=lambda adapter, rec: adapter in ("claude", "grok"),
                         records=records)
    assert ("claude", "a") in asked and ("claude", "b") in asked
    assert asked[-1] == ("gbagent", "here")
    assert result.spawned == 1


def test_a_profile_that_excludes_gbagent_does_not_gain_it(wave, monkeypatch):
    _excluding(monkeypatch, "gbagent")
    result, asked = wave(limited=lambda adapter, rec: adapter != "gbagent",
                         records=[_sub("a"), _sub("b")])
    assert "gbagent" not in [a for a, _ in asked]
    assert result.reason == "capacity"


def test_the_only_record_spent_moves_to_another_vendor_on_the_same_seat(wave):
    minted: list = []
    result, asked = wave(limited=_spent("a"), records=[_sub("a")], minted_roles=minted)
    assert asked[0] == ("claude", "a")
    assert asked[1][0] == "grok"
    assert len(minted) == 1
    assert result.spawned == 1
    assert result.wave.unused_seats == 0


# --- nothing left ----------------------------------------------------------------------

def test_every_candidate_spent_ends_capacity_and_names_them(wave):
    """Sabotage: delete the `CapError("capacity")` raise in `failover` and this crashes
    out as `vendor_limit`, not `capacity`."""
    records = [_sub("a"), _sub("b"), ent.Entitlement("g", "grok", "subscription", "/g")]
    result, _asked = wave(limited=lambda adapter, rec: True, records=records)
    assert result.reason == "capacity"
    assert result.reason not in ("cap", "vendor_limit")
    payload = result.as_json()
    by_key = {e["entitlement"] or e["adapter"]: e for e in payload["exhausted"]}
    assert set(by_key) == {"a", "b", "g", "gbagent"}
    assert by_key["a"]["vendor"] == "claude" and by_key["g"]["vendor"] == "grok"
    assert payload["vendors_launched"] == []
    assert payload["vendors_not_launched"] == ["claude", "grok", "gbagent"]


def test_the_reset_words_are_kept_as_printed_and_not_parsed(wave):
    result, _ = wave(limited=_spent("a"), records=[_sub("a"), _sub("b")])
    entry = result.as_json()["exhausted"][0]
    assert entry["said"] == SAID
    assert "resets 12:20pm (America/New_York)" in json.dumps(result.as_json())
    # No parsed timestamp beside it: only when it was SEEN.
    assert set(entry) == {"entitlement", "adapter", "vendor", "said", "seen_at"}


# --- a registered child and a later slot -------------------------------------------------

def test_a_later_slot_does_not_return_to_the_spent_record_and_register_does_not_clear(wave):
    """Item 1 starts on a and registers; item 2 hits the limit on a and moves to b; item 3
    goes straight to b. Sabotage: clear `wave.exhausted` on a successful spawn and item 3
    asks for a again."""
    calls = {"n": 0}

    def limited(adapter, rec):
        calls["n"] += 1
        return calls["n"] == 2  # the second launch on a is the one that is spent

    result, asked = wave(limited=limited, records=[_sub("a"), _sub("b")],
                         items=(["GRPH-7"], ["GRPH-8"], ["GRPH-9"]), max_workers=3)
    assert asked == [("claude", "a"), ("claude", "a"), ("claude", "b"), ("claude", "b")]
    assert result.spawned == 3
    # The child that had registered before the limit was left to finish, not killed.
    first = result.wave.spawned[0]
    assert first.agent_id
    assert [e["entitlement"] for e in result.as_json()["exhausted"]] == ["a"]


# --- the mark is process memory -----------------------------------------------------------

def test_the_mark_is_never_written_beside_the_registry_and_a_new_wave_tries_again(
    git_repo, tmp_path, state, matrix, scripts, monkeypatch,
):
    reg_dir = tmp_path / "registry"
    reg = reg_dir / ent.FILENAME
    monkeypatch.setenv(ent.PATH_ENV, str(reg))
    ent.add(_sub("a"))
    ent.add(_sub("b"))
    before = reg.read_bytes()
    result, asked = _wave(git_repo, tmp_path, state, matrix, scripts,
                          limited=_spent("a"), records=None)
    assert asked[:2] == [("claude", "a"), ("claude", "b")]
    assert [p.name for p in reg_dir.iterdir()] == [ent.FILENAME]
    assert reg.read_bytes() == before
    assert "session limit" not in reg.read_text()
    # A new wave (a new Wave object, as a new process gets) launches `a` again.
    result2, asked2 = _wave(git_repo, tmp_path / "second", state, matrix, scripts,
                            limited=lambda *_: False, records=None)
    assert asked2[0] == ("claude", "a")


# --- adding records does not judge the login -------------------------------------------

def test_a_repeated_handle_or_same_login_is_stored_without_a_warning(tmp_path, monkeypatch,
                                                                       capsys):
    monkeypatch.setenv(ent.PATH_ENV, str(tmp_path / "e.json"))
    args = ["entitlement", "add", "--vendor", "claude", "--kind", "subscription"]
    assert main([*args, "--name", "a", "--handle", "/home/me/.claude"]) == 0
    assert main([*args, "--name", "b", "--handle", "/home/me/.claude"]) == 0
    # Two spellings of one login: the file cannot tell, and does not try.
    assert main([*args, "--name", "c", "--handle", "/home/me/../me/.claude"]) == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""
    assert [r.name for r in ent.load()] == ["a", "b", "c"]


def test_a_local_record_has_nothing_to_select_and_launches_as_is():
    launch = Launch(adapter="gbagent", argv=["gbagent"], seat_path=Path("/x"), config={},
                    instruction="")
    rec = ent.Entitlement("here", "gbagent", "local", "")
    assert ADAPTERS["gbagent"].select_entitlement(launch, rec) is launch
