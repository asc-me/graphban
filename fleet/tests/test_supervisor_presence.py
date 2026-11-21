"""GRPH-1069 — one long tool call keeps the claim; a dead child does not.

`watch_tick` posts `kind=lease` while the pid is alive, on the server's heartbeat
interval (a third of the 150s presence TTL), and posts nothing once `poll()` is
not None. The supervisor does not call `heartbeat`, and it does not raise the TTL.

Sabotage: delete `keep_leases(...)` from `watch_tick`. No post is recorded across
the five minutes, and the span assertion fails. The order pin lives in
`test_lease_keepalive.py`; this file is the span.
"""
from __future__ import annotations

import time
from pathlib import Path

from gbfleet.client import ALLOWED_TOOLS
from gbfleet.spawn import Child
from gbfleet.supervisor import Limits, Wave, watch_tick

#: The presence TTL the roster reports, and the bug's number. A third of it is the
#: interval the server tells the supervisor to post on.
PRESENCE_TTL = 150
HEARTBEAT_INTERVAL = 50
#: One tool call, longer than the TTL. The report's suite run was this shape.
TOOL_CALL_SECONDS = 300


class _Proc:
    def __init__(self, code: int | None = None) -> None:
        self.pid = 4242
        self._code = code

    def poll(self) -> int | None:
        return self._code


class _Roster:
    """The agent stays `working`, so the disowned backstop does not kill a busy child."""

    def __init__(self) -> None:
        self.posts: list[tuple[float, dict]] = []
        self.calls: list[str] = []
        self.order: list[str] = []
        self.allowed = frozenset()
        self.clock = 0.0

    def post_observation(self, **payload):
        self.order.append("lease")
        self.posts.append((self.clock, payload))
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
            "presence_ttl_seconds": PRESENCE_TTL,
            "heartbeat_interval_seconds": HEARTBEAT_INTERVAL,
        }

    def post_attempt(self, **_payload):
        return None

    def call(self, tool, **_args):
        self.calls.append(tool)
        return {}


def _child(worktree: Path) -> Child:
    return Child(
        adapter="fake", worktree=worktree, branch="gb/obs", base="",
        seat_path=worktree / "seat.json",
        process=_Proc(None),
        started_at=time.monotonic(), log_dir=worktree,
        agent_id="GRPH-A1", held_items=["GRPH-1"],
    )


def test_a_five_minute_tool_call_is_kept_and_a_dead_child_is_not(tmp_path: Path, monkeypatch):
    """THE CALL, across a span longer than the presence TTL.

    The clock patched here is the keep-alive's. `watch_tick`'s wall-clock cap keeps the
    real monotonic, so a simulated five minutes does not look like the child overran
    its hour. `base` stays empty so the publish path does not touch git.

    Sabotage: delete `keep_leases(...)` from `watch_tick`. `posts` is empty.
    """
    clock = {"t": 10_000.0}
    monkeypatch.setattr("gbfleet.heartbeat.time.monotonic", lambda: clock["t"])

    client = _Roster()
    client.clock = clock["t"]
    child = _child(tmp_path)
    wave = Wave()
    step = 10
    t0 = clock["t"]

    while clock["t"] <= t0 + TOOL_CALL_SECONDS:
        client.clock = clock["t"]
        watch_tick(wave, [child], Limits(), client)
        clock["t"] += step

    posts = client.posts
    assert [payload["kind"] for _, payload in posts] == ["lease"] * len(posts)
    assert [stamp for stamp, _ in posts] == [
        t0 + HEARTBEAT_INTERVAL * i for i in range(TOOL_CALL_SECONDS // HEARTBEAT_INTERVAL + 1)
    ]
    gaps = [b - a for a, b in zip((s for s, _ in posts), (s for s, _ in posts[1:]))]
    assert gaps and max(gaps) < PRESENCE_TTL
    assert posts[-1][0] - posts[0][0] >= TOOL_CALL_SECONDS
    assert client.order[0] == "lease"
    assert "heartbeat" not in client.calls
    assert "heartbeat" not in ALLOWED_TOOLS
    assert child.running
    assert wave.partition.heartbeat_interval == HEARTBEAT_INTERVAL
    assert wave.failures == []

    # The process has exited. Salvage is not this claim; marking the child reaped keeps
    # the watch loop off a worktree that was never a git repo. The posts stop because
    # `poll()` is not None.
    child.process._code = 0  # type: ignore[attr-defined]
    child.reaped = True
    alive = len(posts)
    dead_until = clock["t"] + PRESENCE_TTL
    while clock["t"] <= dead_until:
        client.clock = clock["t"]
        watch_tick(wave, [child], Limits(), client)
        clock["t"] += step

    assert not child.running
    assert len(client.posts) == alive
    assert "heartbeat" not in client.calls
    assert wave.failures == []
