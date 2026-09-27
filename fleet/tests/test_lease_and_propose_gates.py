"""Reap respects lease ownership; propose refuses UNDECLARED / BEHIND; the cap counts items (GRPH-949).

Sabotage: drop the ownership check in `_release_held_items` (both the `_lease_holder`
read and the "not the lease holder" classification) and the lease tests see FAILED again.
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gbfleet.client import Graphban, ToolFailed
from gbfleet.spawn import Child
from gbfleet.supervisor import Limits, Wave, _release_held_items, propose_branch
from gbfleet.until import CapError, _cap_children


def _child(held: list[str], agent_id: str = "GRPH-A1", *, running: bool = False) -> Child:
    class _Proc:
        pid = 4242

        def poll(self):
            return None if running else 0

    child = Child(adapter="fake", worktree=Path("/nowhere"), branch="wave/1", base="",
                  seat_path=Path("/nowhere/seat.json"), process=_Proc(),
                  started_at=time.monotonic(), log_dir=Path("/nowhere/logs"),
                  agent_id=agent_id)
    child.held_items = list(held)
    return child


def _client(allowed: set[str], details: dict | None = None, release_error=None) -> Graphban:
    client = MagicMock(spec=Graphban)
    client.allowed = frozenset(allowed)

    def call(tool, **kw):
        if tool == "get_item_details":
            return (details or {}).get(kw["id"], {})
        if tool == "release_item" and release_error is not None:
            raise release_error
        return {}

    client.call = MagicMock(side_effect=call)
    return client


def _released(client) -> list[str]:
    return [c.kwargs["id"] for c in client.call.call_args_list if c.args[0] == "release_item"]


# -- lease ownership ---------------------------------------------------------------------

def test_reap_skips_a_lease_another_agent_holds():
    """THE ONE THAT MATTERS. A lease that moved on is info, never FAILED."""
    wave = Wave()
    client = _client({"release_item", "get_item_details"},
                     details={"GRPH-1": {"claimed_by": "GRPH-OTHER"},
                              "GRPH-2": {"claimed_by": "GRPH-A1"}})
    _release_held_items(wave, _child(["GRPH-1", "GRPH-2"]), client)

    assert _released(client) == ["GRPH-2"]
    assert not any("FAILED" in f or "failed" in f for f in wave.failures), wave.failures
    assert wave.lease_moved == ["GRPH-1: not released for GRPH-A1; lease held by GRPH-OTHER"]


def test_server_refusal_without_a_read_is_info_and_logged_once():
    """SPAWN_READS has no `get_item_details`: the server's refusal is classified instead."""
    wave = Wave()
    refusal = ToolFailed("release_item", "conflict", "not the lease holder for 'GRPH-1'")
    client = _client({"release_item"}, release_error=refusal)
    _release_held_items(wave, _child(["GRPH-1"]), client)
    _release_held_items(wave, _child(["GRPH-1"]), client)

    assert wave.failures == []
    assert len(wave.lease_moved) == 1
    assert wave.lease_moved[0].startswith("GRPH-1:")


def test_other_release_errors_are_still_failures():
    wave = Wave()
    client = _client({"release_item"},
                     release_error=ToolFailed("release_item", "EPERM", "no"))
    _release_held_items(wave, _child(["GRPH-1"]), client)
    assert any("release_item GRPH-1 failed" in f for f in wave.failures)
    assert wave.lease_moved == []


def test_unheld_lease_is_not_released_either():
    """`claimed_by` empty: the child delivered or the lease lapsed — nothing of ours to free."""
    wave = Wave()
    client = _client({"release_item", "get_item_details"},
                     details={"GRPH-1": {"claimed_by": None}})
    _release_held_items(wave, _child(["GRPH-1"]), client)
    assert _released(client) == []
    assert wave.failures == []
    assert wave.lease_moved == ["GRPH-1: not released for GRPH-A1; lease no longer held by it"]


# -- propose gate ------------------------------------------------------------------------

@pytest.fixture
def no_forge(monkeypatch):
    import gbfleet.propose as propose_mod
    import gbfleet.worktree as wt_mod
    opened = []
    monkeypatch.setattr(wt_mod, "remote_for", lambda repo: "origin")
    monkeypatch.setattr(wt_mod, "default_ref", lambda repo, remote: "origin/main")
    monkeypatch.setattr(propose_mod, "subject", lambda repo, branch, base: "GRPH-1: real work")
    monkeypatch.setattr(propose_mod, "propose",
                        lambda *a, **k: opened.append(a) or propose_mod.Proposed(
                            branch=a[1], url="https://x/pull/1", ok=True))
    return opened


def test_undeclared_file_blocks_the_proposal_and_names_it(no_forge):
    wave = Wave()
    wave.undeclared["wave/1"] = ["web/src/App.tsx"]
    propose_branch(wave, Path("."), "wave/1", ["GRPH-1"], client=None)
    assert no_forge == []
    got = wave.proposed["wave/1"]
    assert got.skipped and not got.url
    assert "web/src/App.tsx" in got.reason
    assert "web/src/App.tsx" in wave.unproposed["wave/1"]


def test_behind_branch_is_refused(no_forge):
    wave = Wave()
    wave.stale["wave/1"] = (3, "origin/main")
    propose_branch(wave, Path("."), "wave/1", ["GRPH-1"], client=None)
    assert no_forge == []
    assert "3 commit(s) behind origin/main" in wave.proposed["wave/1"].reason


def test_clean_branch_is_still_proposed(no_forge):
    wave = Wave()
    propose_branch(wave, Path("."), "wave/1", ["GRPH-1"], client=None)
    assert len(no_forge) == 1
    assert wave.proposed["wave/1"].url


# -- cap counts items --------------------------------------------------------------------

def _spawned(*holdings: tuple[list[str], bool]) -> Wave:
    wave = Wave()
    wave.spawned = [SimpleNamespace(held_items=h, running=r) for h, r in holdings]
    return wave


def test_respawn_for_an_exited_childs_item_is_free():
    """Two items, three processes (one respawn): `--max-children 2` still has the respawn."""
    wave = _spawned((["GRPH-1"], False), (["GRPH-2"], True))
    _cap_children(wave, Limits(max_children=2), item="GRPH-1")  # no raise


def test_a_new_item_past_the_cap_still_raises():
    wave = _spawned((["GRPH-1"], False), (["GRPH-2"], True))
    with pytest.raises(CapError):
        _cap_children(wave, Limits(max_children=2), item="GRPH-3")


def test_duplicate_processes_for_one_item_count_once():
    wave = _spawned((["GRPH-1"], False), (["GRPH-1"], False), (["GRPH-1"], True))
    _cap_children(wave, Limits(max_children=2), item="GRPH-2")  # 1 item counted


def test_children_that_held_nothing_still_count():
    """GRPH-803: a loop spawning into nothing must stay bounded."""
    wave = _spawned(([], False), ([], False))
    with pytest.raises(CapError):
        _cap_children(wave, Limits(max_children=2))


def test_respawns_are_bounded_by_the_ceiling():
    wave = _spawned(*[(["GRPH-1"], False)] * 4)
    with pytest.raises(CapError):
        _cap_children(wave, Limits(max_children=2), item="GRPH-1")
