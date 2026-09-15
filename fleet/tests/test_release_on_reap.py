"""Reap releases held items so choose_resume can pick up the salvage branch (GRPH-850).

A child that dies (wall_clock / reap / stop) leaves its items `in_progress` / `claimed_by`
set to the dead agent. `choose_resume` skips `in_progress` unconditionally, so the salvage
branch sits orphaned while a fresh spawn cuts from main — losing the work just salvaged.

The fix: `_reap_exited` calls `release_item` for each held item after salvaging the git
branch. The planner client (passed as `client` from `until.run`) has `release_item` in its
allowlist; a pure supervisor client does not, and the call is silently skipped.

Sabotage: delete the `_release_held_items` call from `_reap_exited` and this test fails —
the items stay `in_progress` / `claimed_by=<dead agent>`, and `choose_resume` cannot pick
up the salvage branch.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gbfleet.client import Graphban
from gbfleet.spawn import Child
from gbfleet.supervisor import Wave, _reap_exited
from gbfleet.worktree import create


@pytest.fixture
def wave() -> Wave:
    return Wave()


@pytest.fixture
def planner_client() -> Graphban:
    """A mock planner client with release_item in its allowlist."""
    client = MagicMock(spec=Graphban)
    client.allowed = frozenset({
        "fleet_status",
        "propose_allocation",
        "search_items",
        "update_item",
        "release_item",
    })
    client.call = MagicMock(return_value={})
    return client


@pytest.fixture
def supervisor_client() -> Graphban:
    """A mock supervisor client WITHOUT release_item in its allowlist."""
    client = MagicMock(spec=Graphban)
    client.allowed = frozenset({"fleet_status", "propose_allocation"})
    client.call = MagicMock(return_value={})
    return client


def _make_child(git_repo: Path, workspace: Path, *, held_items: list[str],
                agent_id: str = "GRPH-A123") -> Child:
    """A dead child with held items and a worktree that can be reaped."""
    import subprocess
    import time
    from pathlib import Path
    
    workspace.mkdir(exist_ok=True)
    tree = create(git_repo, workspace / "wave-1", "wave", "1")
    (tree.path / "feature.py").write_text("print('work')\n", encoding="utf-8")
    
    class _Dead:
        def __init__(self):
            self.pid = 4242
        def poll(self):
            return 0
    
    child = Child(
        adapter="fake",
        worktree=tree.path,
        branch=tree.branch,
        base=tree.base,
        seat_path=workspace / "seat.json",
        process=_Dead(),
        started_at=time.monotonic() - 30,
        log_dir=workspace / "logs",
        agent_id=agent_id,
    )
    child.held_items = list(held_items)
    return child


def test_reap_releases_held_items(
    git_repo: Path, tmp_path: Path, wave: Wave, planner_client: Graphban
):
    """THE ONE THAT MATTERS. Reap frees the ledger row so choose_resume can pick it up.

    Sabotage: delete the `_release_held_items` call from `_reap_exited` and this test fails —
    `client.call` is never invoked with `release_item`, and the items stay `in_progress`."""
    child = _make_child(git_repo, tmp_path / "ws", held_items=["GRPH-850", "GRPH-851"])

    _reap_exited(wave, [child], client=planner_client)

    # release_item called once per held item
    release_calls = [
        c for c in planner_client.call.call_args_list
        if c.args and c.args[0] == "release_item"
    ]
    assert len(release_calls) == 2, f"expected 2 release_item calls, got {len(release_calls)}"
    # Each call has the right id and agent_id
    called_ids = {c.kwargs.get("id") or c.args[1] for c in release_calls}
    assert called_ids == {"GRPH-850", "GRPH-851"}
    for c in release_calls:
        agent = c.kwargs.get("agent_id") or c.args[2] if len(c.args) > 2 else c.kwargs.get("agent_id")
        assert agent == "GRPH-A123"


def test_reap_skips_release_when_client_lacks_permission(
    git_repo: Path, tmp_path: Path, wave: Wave, supervisor_client: Graphban
):
    """The supervisor's ALLOWED_TOOLS does not include release_item. The call is skipped,
    not raised — the row stays stuck until fleet_status calls requeue_offline_items."""
    child = _make_child(git_repo, tmp_path / "ws", held_items=["GRPH-850"])

    _reap_exited(wave, [child], client=supervisor_client)

    # release_item NOT called because it's not in the allowlist
    release_calls = [
        c for c in supervisor_client.call.call_args_list
        if c.args and c.args[0] == "release_item"
    ]
    assert len(release_calls) == 0


def test_reap_skips_release_when_no_held_items(
    git_repo: Path, tmp_path: Path, wave: Wave, planner_client: Graphban
):
    """A child with no held items does not call release_item."""
    child = _make_child(git_repo, tmp_path / "ws", held_items=[])

    _reap_exited(wave, [child], client=planner_client)

    release_calls = [
        c for c in planner_client.call.call_args_list
        if c.args and c.args[0] == "release_item"
    ]
    assert len(release_calls) == 0


def test_reap_skips_release_when_no_client(
    git_repo: Path, tmp_path: Path, wave: Wave
):
    """A reap with no client (pure supervisor) does not call release_item."""
    child = _make_child(git_repo, tmp_path / "ws", held_items=["GRPH-850"])

    _reap_exited(wave, [child], client=None)

    # No exception, no call — the row stays stuck until offline requeue


def test_reap_reports_release_failure(
    git_repo: Path, tmp_path: Path, wave: Wave, planner_client: Graphban
):
    """A failed release is reported, never fatal — the salvage is already committed."""
    from gbfleet.client import ToolFailed
    child = _make_child(git_repo, tmp_path / "ws", held_items=["GRPH-850"])
    planner_client.call.side_effect = ToolFailed("release_item", "EPERM", "no", "no")

    _reap_exited(wave, [child], client=planner_client)

    # The failure is recorded on the wave
    assert any("release_item GRPH-850 failed" in f for f in wave.failures)
    # But the reap still succeeded (the child was reaped)
    assert child.reaped
    assert len(wave.reaped) == 1
