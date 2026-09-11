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
    # A PATH-only env on Windows omits PATHEXT/SystemRoot, and CreateProcess then
    # cannot find `railway.cmd` (WinError 2) — a false red that is not the shim.
    env = shim.environment({**os.environ, "PATH": os.defpath}, where)

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


def test_windows_build_writes_cmd_stubs(tmp_path, monkeypatch):
    """Windows resolves executables via PATHEXT (.exe, .cmd, …). An extensionless
    #!/bin/sh file is inert there — GRPH-588: 32 of 50 failures on the first Windows
    run were CreateProcess reporting '%1 is not a valid Win32 application'. A name
    without extension must NOT be the only file."""
    monkeypatch.setattr(os, "name", "nt")

    where = shim.build(tmp_path / "shim")

    cmd_files = list(where.glob("*.cmd"))
    assert len(cmd_files) > 0, "no .cmd stubs written — Windows guard is inert"
    for name in ("railway", "gh", "psql"):
        assert (where / f"{name}.cmd").exists(), f"{name}.cmd missing"
        bare = where / name
        assert not bare.exists(), (
            f"extensionless '{name}' should not exist on nt — "
            f"a name without extension as the only file is the bug this fixes"
        )
    sample = (where / "railway.cmd").read_text(encoding="utf-8")
    assert "refusing to run 'railway'" in sample
    assert "exit /b 126" in sample


def test_posix_build_writes_shebang_stubs(tmp_path, monkeypatch):
    """POSIX keeps the existing #!/bin/sh files — the Windows branch must not
    change what POSIX writes."""
    monkeypatch.setattr(os, "name", "posix")

    where = shim.build(tmp_path / "shim")

    assert (where / "railway").exists()
    assert not (where / "railway.cmd").exists(), "POSIX must not write .cmd stubs"
    content = (where / "railway").read_text(encoding="utf-8")
    assert content.startswith("#!/bin/sh")
    assert "exit 126" in content


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
    the loop does not have.

    The FILTER is what has to match, not the whole argument set. GRPH-833 added `holds`, which
    asks the same server for the same clusters and a diagnostic table alongside them — it
    cannot change which clusters come back, so it cannot change the plan. Pinning byte-equality
    here would have made that additive read look like a divergence, which is the opposite of
    what this test is for."""
    planner = _Planner([_c(["SA-1"])])

    until_mod.plan(planner, "SA-P11", 4)

    assert [tool for tool, _ in planner.calls] == ["collision_clusters"]
    (_, args), = planner.calls
    assert args["prd_id"] == "SA-P11", "the dry run scoped differently from the loop"
    assert set(args) - {"holds"} == set(until_mod._scope("SA-P11")), (
        "the dry run sent a filter the loop does not")


def test_it_writes_nothing():
    """READS ONLY. A dry run with a side effect is a wave."""
    planner = _Planner([_c(["SA-1"])])

    until_mod.plan(planner, None, 4)

    assert [t for t, _ in planner.calls] == ["collision_clusters"]
