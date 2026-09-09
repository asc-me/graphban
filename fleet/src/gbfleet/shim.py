"""Keep a child's shell away from the operator's production credentials (GRPH-818).

`--strict-mcp-config` (GRPH-802) bounded the child's TOOL surface and nothing else. Reported
back from a real wave, correctly: children still run `--dangerously-skip-permissions` with a
full shell, and `railway`, `vercel`, `gh`, `op` and `psql` are on PATH and authenticated. The
worktree bounds the filesystem; it bounds nothing else. A child that decided to redeploy
production could still do it — it would just type the command instead of calling a tool.

This prepends a directory of refusing stubs to the child's PATH. It survives
`--dangerously-skip-permissions` because it is not a harness feature: the flag governs whether
the vendor asks before running a command, and this governs what `railway` resolves to.

**WHAT THIS IS NOT.** It stops an agent that wandered, not one that is trying. `/usr/bin/env
railway`, an absolute path, or a shell built-in all walk straight past it, and a model that
wanted to would find that in one step. Saying otherwise would be the failure this repository
keeps naming — a guard whose real reach is smaller than its name. The honest claim is: a child
that reaches for a deployment CLI by name gets a refusal that says why, and the operator gets
it in the log instead of in an incident. A sandbox is the thing that bounds a determined
process, and it is a different piece of work.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

#: Denied unless the operator says otherwise. Every one of these reaches something OUTSIDE the
#: worktree that a coding agent has no business reaching on its own: deployment planes, cloud
#: control planes, a secret manager, a forge write surface, and database clients that are
#: authenticated to whatever the operator last logged into.
#:
#: `docker` is deliberately ABSENT. A wave's children verified migrations against throwaway
#: Postgres containers, which is exactly the verification worth having, and denying it would
#: cost real evidence to prevent a hypothetical.
#:
#: `psql` IS here and it is the uncomfortable one: it is how you talk to a production database
#: and also how you check a local container. `--allow psql` keeps the second, and having to
#: type that is the point — the trade is surfaced rather than chosen silently.
#:
#: `gh` is here because the supervisor opens PRs itself now (GRPH-804), so a child needs it
#: for nothing, while `gh api` is an arbitrary authenticated write to the whole forge.
DEFAULT_DENY: tuple[str, ...] = (
    "railway", "vercel", "fly", "flyctl", "heroku",
    "aws", "gcloud", "az", "doctl", "kubectl", "helm", "terraform", "tofu",
    "op", "gh", "psql", "mysql", "mongosh", "redis-cli",
)

_STUB = """#!/bin/sh
echo "gbfleet: refusing to run '{name}' inside a fleet child." >&2
echo "  This process is building one ledger item in a worktree. {name} reaches outside it," >&2
echo "  with the credentials of whoever started the wave." >&2
echo "  If this item genuinely needs it, the operator starts the wave with --allow {name}." >&2
exit 126
"""


def denied(deny: list[str] | None = None, allow: list[str] | None = None) -> list[str]:
    """The names to stub, in order. `allow` wins, so punching a hole is one flag."""
    names = list(DEFAULT_DENY) + [d.strip() for d in (deny or []) if d.strip()]
    permitted = {a.strip() for a in (allow or []) if a.strip()}
    out: list[str] = []
    for name in names:
        if name not in permitted and name not in out:
            out.append(name)
    return out


def build(where: Path, deny: list[str] | None = None, allow: list[str] | None = None) -> Path:
    """Write the stub directory and return it. Idempotent; safe to call per child."""
    where.mkdir(parents=True, exist_ok=True)
    for name in denied(deny, allow):
        stub = where / name
        stub.write_text(_STUB.format(name=name), encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return where


def environment(env: dict, shim: Path | None) -> dict:
    """`env` with the shim FIRST on PATH.

    First, or it does nothing: PATH resolves left to right, and appending would put the stub
    behind the real binary it exists to shadow — a guard that is present, exported, and inert.
    """
    if shim is None:
        return env
    current = env.get("PATH") or os.defpath
    return {**env, "PATH": f"{shim}{os.pathsep}{current}"}
