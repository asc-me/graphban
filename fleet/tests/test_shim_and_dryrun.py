"""A child's shell is bounded, and a wave can be inspected before it runs (GRPH-818, 819).

Both from the super-arc follow-up, which was right about the first in a way the earlier fix
was not. `--strict-mcp-config` bounded the child's TOOL surface and nothing else: children
still run `--dangerously-skip-permissions` with a full shell, and railway, vercel, gh, op and
psql are on PATH and authenticated. The worktree bounds the filesystem; it bounds nothing
else.

**What the shim is, stated so nobody has to infer it.** It stops an agent that wandered, not
one that is trying: an absolute path walks straight past it. The honest claim is that a child
reaching for a deployment CLI by name gets a refusal saying why, and the operator reads it in
a log instead of an incident. A sandbox is the thing that bounds a determined process.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from gbfleet import shim
from gbfleet import until as until_mod


# ---- the deny-list ---------------------------------------------------------------------------

def test_the_deployment_planes_are_denied():
    names = shim.denied()

    for tool in ("railway", "vercel", "gh", "op", "psql", "kubectl", "aws"):
        assert tool in names, f"{tool} reaches outside the worktree and is not stubbed"


def test_docker_is_not_denied():
    """A wave's children verified migrations against throwaway Postgres containers — exactly
    the verification worth having. Denying it costs real evidence to prevent a hypothetical."""
    assert "docker" not in shim.denied()


def test_allow_punches_a_hole():
    """psql is the uncomfortable one: how you reach production, and how you check a local
    container. Having to type --allow is the point — the trade is surfaced, not chosen."""
    assert "psql" not in shim.denied(allow=["psql"])
    assert "psql" in shim.denied()


def test_deny_adds_to_it():
    assert "ssh" in shim.denied(deny=["ssh"])


# ---- and what it actually does ------------------------------------------------------------------

def test_a_denied_command_really_refuses(tmp_path):
    """Run for real rather than asserted: a stub that is not executable, or not first on
    PATH, is a guard that is present, exported and inert."""
    where = shim.build(tmp_path / "shim")
    env = shim.environment({"PATH": os.defpath}, where)

    done = subprocess.run(["railway", "up"], env=env, capture_output=True, text=True)

    assert done.returncode == 126
    assert "refusing to run 'railway'" in done.stderr
    assert "--allow railway" in done.stderr, "refused without naming the way through"


def test_the_shim_goes_first_on_path(tmp_path):
    """PATH resolves left to right. Appending would put the stub behind the binary it exists
    to shadow."""
    where = shim.build(tmp_path / "shim")

    got = shim.environment({"PATH": "/usr/bin:/bin"}, where)["PATH"]

    assert got.startswith(f"{where}{os.pathsep}")
    assert "/usr/bin" in got, "replaced PATH instead of prepending to it"


def test_no_shim_leaves_the_environment_alone(tmp_path):
    env = {"PATH": "/usr/bin"}

    assert shim.environment(env, None) == env


# ---- --dry-run ------------------------------------------------------------------------------------

class _Planner:
    def __init__(self, clusters):
        self.clusters = clusters
        self.calls = []

    def call(self, tool, **kw):
        self.calls.append((tool, kw))
        return {"clusters": self.clusters, "total": len(self.clusters)}


def _c(items, held=None, free_in=None):
    out = {"items": items, "areas": items}
    if held:
        out["held_by"] = held
    if free_in is not None:
        out["free_in"] = free_in
    return out


def test_it_names_what_would_be_delegated():
    got = until_mod.plan(_Planner([_c(["SA-1"]), _c(["SA-2"])]), None, 4)

    assert got["would_delegate"] == ["SA-1", "SA-2"]
    assert got["clusters_free"] == 2


def test_held_clusters_are_shown_and_not_counted():
    got = until_mod.plan(_Planner([_c(["SA-1"]), _c(["SA-2"], held=["SA-A9"], free_in=90)]),
                         None, 4)

    assert got["would_delegate"] == ["SA-1"]
    assert got["held"][0]["held_by"] == ["SA-A9"] and got["held"][0]["free_in"] == 90


def test_the_cap_is_reported_rather_than_silently_applied():
    """"Would delegate 2" and "would delegate 2 of 5" are different answers to "is my scope
    right?"."""
    got = until_mod.plan(_Planner([_c([f"SA-{i}"]) for i in range(5)]), None, 2)

    assert len(got["would_delegate"]) == 2
    assert got["capped_by_max_workers"] is True
    assert got["clusters_free"] == 5


def test_it_asks_the_same_question_the_loop_asks():
    """A dry run that MODELLED the wave instead of asking it would reassure you about a plan
    the loop does not have."""
    planner = _Planner([_c(["SA-1"])])

    until_mod.plan(planner, "SA-P11", 4)

    assert planner.calls == [("collision_clusters", {"prd_id": "SA-P11"})]


def test_it_writes_nothing():
    """READS ONLY. A dry run with a side effect is a wave."""
    planner = _Planner([_c(["SA-1"])])

    until_mod.plan(planner, None, 4)

    assert [t for t, _ in planner.calls] == ["collision_clusters"]
