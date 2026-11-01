"""GRPH-1116 — a live child keeps its lease, and the post stops when the process does.

The supervisor may not call `heartbeat`. `watch_tick` posts `kind=lease` on the
observations route before it reads the roster, because `fleet_status` requeues an
agent whose presence has lapsed. Cadence is the server's heartbeat interval once a
roster has said, else 30s. HEAD at the cut base is not a commit.

Sabotage: delete the `keep_leases(...)` line at the top of `watch_tick`. The order
test fails. Adding `heartbeat` to `ALLOWED_TOOLS` fails `test_client.py`'s exact
equality, which is the pin this item is not allowed to move.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from gbfleet.client import ALLOWED_TOOLS
from gbfleet.heartbeat import FALLBACK_INTERVAL, branch_commit, keep
from gbfleet.spawn import Child
from gbfleet.supervisor import Limits, Wave, watch_tick


class _Proc:
    def __init__(self, code: int | None = None) -> None:
        self.pid = 4242
        self._code = code

    def poll(self) -> int | None:
        return self._code


class _Roster:
    """Records the order `watch_tick` talks to the server. The agent stays listed so
    the disowned backstop does not kill the child for being absent."""

    def __init__(self) -> None:
        self.order: list[str] = []
        self.posts: list[dict] = []
        self.calls: list[str] = []
        self.allowed = frozenset()

    def post_observation(self, **payload):
        self.order.append("lease")
        self.posts.append(payload)
        return {"item_id": payload["item_id"], "kind": "lease",
                "recorded": True, "extended": True}

    def fleet_status(self) -> dict:
        self.order.append("roster")
        return {
            "agents": [{
                "id": "GRPH-A1",
                "state": "working",
                "holdings": [{"id": "GRPH-1", "hold": "build"}],
            }],
            "presence_ttl_seconds": 150,
            "heartbeat_interval_seconds": 50,
        }

    def post_attempt(self, **_payload):
        return None

    def call(self, tool, **_args):
        self.calls.append(tool)
        return {}


def _child(worktree: Path, *, running: bool = True, agent_id: str | None = "GRPH-A1",
           held: list | None = None, base: str = "") -> Child:
    return Child(
        adapter="fake", worktree=worktree, branch="gb/obs", base=base,
        seat_path=worktree / "seat.json",
        process=_Proc(None if running else 0),
        started_at=time.monotonic(), log_dir=worktree,
        agent_id=agent_id, held_items=list(held if held is not None else ["GRPH-1"]),
    )


def test_watch_tick_posts_lease_before_the_roster_and_not_via_heartbeat(tmp_path: Path):
    """THE CALL. `fleet_status` requeues first. A keep-alive that runs after it is a
    post about an item the roster has already released.

    Sabotage: delete `keep_leases(...)` from `watch_tick`, or move it under `_roster`.
    `order[0]` is no longer `lease`.
    """
    client = _Roster()
    child = _child(tmp_path)
    wave = Wave()

    watch_tick(wave, [child], Limits(), client)
    watch_tick(wave, [child], Limits(), client)

    assert client.order[0] == "lease"
    assert client.order[1] == "roster"
    assert client.posts == [{
        "item_id": "GRPH-1", "kind": "lease", "branch": "gb/obs", "agent_id": "GRPH-A1",
    }]
    assert "heartbeat" not in client.calls
    assert "heartbeat" not in ALLOWED_TOOLS
    assert wave.partition.heartbeat_interval == 50
    assert wave.failures == []


def test_the_post_is_once_per_interval_and_stops_when_the_process_exits(tmp_path: Path):
    """A command longer than the lease is many ticks. The second tick inside the
    interval must not post. After the process exits, further ticks post nothing, which
    is what lets the server clock lapse the lease."""
    client = _Roster()
    child = _child(tmp_path)
    wave = Wave()
    wave.partition.heartbeat_interval = 50

    keep(wave, [child], client, now=1_000)
    keep(wave, [child], client, now=1_000 + 49)
    assert len(client.posts) == 1

    keep(wave, [child], client, now=1_000 + 50)
    assert len(client.posts) == 2

    child.process._code = 0  # type: ignore[attr-defined]
    keep(wave, [child], client, now=1_000 + 200)
    assert len(client.posts) == 2
    assert wave.failures == []


def test_a_child_with_no_agent_or_no_hold_is_not_kept(tmp_path: Path):
    client = _Roster()
    wave = Wave()
    keep(wave, [_child(tmp_path, agent_id=None)], client, now=1)
    keep(wave, [_child(tmp_path, held=[])], client, now=1)
    keep(wave, [_child(tmp_path, running=False)], client, now=1)
    assert client.posts == []


def test_head_past_the_base_is_the_commit_and_the_base_is_not(git_repo: Path):
    base = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=git_repo, text=True).strip()
    child = _child(git_repo, base=base)
    assert branch_commit(child) is None

    client = _Roster()
    keep(Wave(), [child], client, now=1)
    assert "commit" not in client.posts[0]

    (git_repo / "more.txt").write_text("y\n", encoding="utf-8")
    subprocess.check_call(["git", "add", "more.txt"], cwd=git_repo)
    subprocess.check_call(
        ["git", "-c", "user.email=t@e.invalid", "-c", "user.name=T",
         "commit", "-qm", "more"],
        cwd=git_repo,
    )
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=git_repo, text=True).strip()
    child.lease_kept_at = 0
    keep(Wave(), [child], client, now=FALLBACK_INTERVAL + 5)
    assert client.posts[1]["commit"] == head

    child.base = head[:12]
    child.lease_kept_at = 0
    assert branch_commit(child) is None


def test_a_refused_post_is_a_wave_failure_and_not_a_crash(tmp_path: Path):
    class _Down:
        def post_observation(self, **_payload):
            raise RuntimeError("down")

    child = _child(tmp_path)
    wave = Wave()
    keep(wave, [child], _Down(), now=10)
    assert child.running
    assert any("GRPH-1: lease not extended" in line for line in wave.failures)
    # The clock still advances, so the next poll inside the interval does not
    # append a second copy of the same refusal.
    keep(wave, [child], _Down(), now=10 + FALLBACK_INTERVAL - 1)
    assert len(wave.failures) == 1


def test_an_unread_head_still_extends_the_lease(tmp_path: Path):
    """Git failing is not the process dying. The post goes out with no commit."""
    missing = tmp_path / "not-a-repo"
    missing.mkdir()
    client = _Roster()
    child = _child(missing, base="7ea90ebebe2b88d263dcae8ca77f84b69cb00300")
    keep(Wave(), [child], client, now=1)
    assert client.posts[0]["kind"] == "lease"
    assert "commit" not in client.posts[0]
