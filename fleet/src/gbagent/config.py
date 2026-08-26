"""`.gbagent.toml` — the repository declares how it is verified (PRD-24 D3, S2).

**Declared, never discovered.** Guessing a test command is the same class of mistake as
guessing a vendor flag, and `gbfleet.adapters.codex` already settles that argument: a
fabricated adapter is worse than a missing one, because it produces a child that runs, does
the wrong thing, and blames the vendor for a mistake the supervisor made.

The alternatives were weighed and rejected. Parsing `AGENTS.md` reads a command out of prose
that drifts, and a parser that half-matches runs something plausible and wrong. Letting the
model work it out costs 3–5 turns at ~30s each and might pick `pytest` at the repo root — 2200
tests here — or a deploy script that looked like a test.

**No file, no spawn**, and the same for a malformed one. `resolve()` refuses before a process
starts, exactly as it refuses an unsupported version, because a broken test command means the
agent could never verify anything and a build that starts is ten minutes spent to discover it.

    [tests]
    command = "./.venv/bin/python -m pytest -q"
    cwd = "backend"

**The executable check is not `which`.** This repository's own loop is
`./.venv/bin/python -m pytest -q`, and AGENTS.md says outright that *pytest is NOT on the host
PATH*. A check that only consulted PATH would refuse the repository it was written for. So a
command naming a path is checked as a path, relative to `cwd`; only a bare name is looked up on
PATH.

**`cwd` goes through the same boundary as every write** (S1). A config pointing the test run at
`../../` is refused for the reason any other escape is.

**`[setup]` builds the tree before any of that is checked** (GRPH-502). A fresh `git worktree`
— which is what PRD-22 gives every fleet child — has no `backend/.venv`, so the executable
check above refuses a repository that is merely unbuilt. Found by the S7 walk and worked around
there with a symlink, which was the finding: nothing anywhere prepares a child's environment.

    [setup]
    commands = ["uv venv --python 3.12 backend/.venv",
                "uv pip install -e backend[dev]"]

A LIST rather than one string, because D4 means there is no shell and `a && b` would be literal
arguments. Absent `[setup]` is legal: it means the tree needs no building, and if that turns out
to be false the executable check refuses anyway — the absence is covered by a different check
rather than assumed clean.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .workspace import OutsideWorktree, safe_path

CONFIG_NAME = ".gbagent.toml"


class ConfigRefused(Exception):
    """The repository cannot be verified, so the agent must not start (D3)."""


class SetupFailed(ConfigRefused):
    """A declared setup command failed, so the tree was never built.

    A subclass of `ConfigRefused` because the consequence is identical — no spawn — and every
    caller that already refuses on a bad config should refuse on an unbuilt tree without
    needing to learn a second exception.
    """


#: A setup that hangs is a child that never registers. PRD-22's registration window would
#: eventually catch it, but ten minutes of silence to discover a broken `uv` is not a
#: diagnosis anybody should have to wait for.
SETUP_TIMEOUT = 900


@dataclass(frozen=True)
class VerifyConfig:
    """A command that was checked before anything was spawned.

    Named `VerifyConfig` rather than `TestConfig` because pytest collects any class whose name
    starts with `Test`, and a warning nobody reads is where a real one goes to hide.
    """

    argv: list[str]
    cwd: Path
    #: How it was written, for error messages and the support matrix.
    source: str


def _argv_of(command) -> list[str]:
    """Accept a string or a list. `shlex` splits, it does not interpret.

    Deliberately not a shell: there is no shell in this agent (D4), so a `command` containing
    a pipe or a `&&` gets those characters as literal arguments and fails loudly rather than
    quietly running two things.
    """
    if isinstance(command, list):
        argv = [str(c) for c in command if str(c)]
    elif isinstance(command, str):
        argv = shlex.split(command)
    else:
        raise ConfigRefused(
            f"{CONFIG_NAME}: [tests].command must be a string or a list, got "
            f"{type(command).__name__}"
        )
    if not argv:
        raise ConfigRefused(f"{CONFIG_NAME}: [tests].command is empty")
    return argv


def _executable(argv0: str, cwd: Path) -> None:
    """Refuse a command whose program is not there — as a path or on PATH, not only on PATH."""
    if os.sep in argv0 or (os.altsep and os.altsep in argv0) or argv0.startswith("."):
        candidate = (cwd / argv0).resolve()
        if not candidate.is_file():
            raise ConfigRefused(
                f"{CONFIG_NAME}: [tests].command names {argv0!r}, which does not exist "
                f"relative to {str(cwd)!r}. Refusing to spawn an agent that could never "
                "verify anything."
            )
        if not os.access(candidate, os.X_OK):
            raise ConfigRefused(f"{CONFIG_NAME}: {argv0!r} is not executable")
        return
    if shutil.which(argv0) is None:
        raise ConfigRefused(
            f"{CONFIG_NAME}: [tests].command names {argv0!r}, which is not on PATH. If it "
            "lives in the repository, write it as a path — this project's own loop is "
            "`./.venv/bin/python -m pytest -q` and pytest is not on the host PATH."
        )


def prepare(root: Path | str) -> list[str]:
    """Run `[setup].commands` in a fresh worktree. Returns what it ran; [] when nothing is
    declared.

    Deliberately NOT part of `load`. `load` reads and checks; this one runs processes in the
    caller's tree, and a function named `load` that installs packages is the kind of surprise
    that gets discovered at the worst moment. The caller does `prepare` then `load`, in that
    order, because the executable check in `load` is exactly what an unbuilt tree fails.

    Re-running is the caller's business and is expected to be safe: these commands run once per
    worktree, and both `uv venv` and `uv pip install` are idempotent. Nothing here checks
    whether the tree is already built, because a check that guessed wrong would skip the
    build and leave the refusal to `load` — one step further from the cause.
    """
    base = Path(root)
    path = base / CONFIG_NAME
    if not path.is_file():
        return []  # `load` is what refuses a missing config, and says so better than this could
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return []  # likewise: `load` names the parse error
    setup = data.get("setup")
    if not isinstance(setup, dict):
        return []
    raw = setup.get("commands")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SetupFailed(f"{CONFIG_NAME}: [setup].commands must be a list of commands")

    ran: list[str] = []
    for entry in raw:
        argv = _argv_of(entry)
        try:
            proc = subprocess.run(
                argv, cwd=str(base), capture_output=True, text=True, timeout=SETUP_TIMEOUT,
            )
        except FileNotFoundError:
            raise SetupFailed(
                f"{CONFIG_NAME}: [setup] names {argv[0]!r}, which is not installed here. "
                "The tree cannot be built, so this agent could never verify anything."
            ) from None
        except subprocess.TimeoutExpired:
            raise SetupFailed(
                f"{CONFIG_NAME}: [setup] command {' '.join(argv)!r} exceeded {SETUP_TIMEOUT}s "
                "and was killed. The tree is in an unknown state."
            ) from None
        if proc.returncode != 0:
            tail = ((proc.stderr or proc.stdout or "").strip().splitlines() or [""])[-5:]
            raise SetupFailed(
                f"{CONFIG_NAME}: [setup] command {' '.join(argv)!r} exited {proc.returncode}. "
                "Refusing to spawn an agent into a tree that did not build.\n"
                + "\n".join(tail)
            )
        ran.append(" ".join(argv))
    return ran


def load(root: Path | str) -> VerifyConfig:
    """Read and CHECK the config. Raises `ConfigRefused` with what failed and why."""
    base = Path(root)
    path = base / CONFIG_NAME
    if not path.is_file():
        raise ConfigRefused(
            f"no {CONFIG_NAME} at the repository root. The test command is declared, never "
            "guessed (PRD-24 D3) — without it this agent has no way to verify its own work."
        )
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ConfigRefused(f"{CONFIG_NAME} is not valid TOML: {e}") from None

    tests = data.get("tests")
    if not isinstance(tests, dict):
        raise ConfigRefused(f"{CONFIG_NAME}: no [tests] table")
    if "command" not in tests:
        raise ConfigRefused(f"{CONFIG_NAME}: [tests] has no `command`")

    argv = _argv_of(tests["command"])
    try:
        cwd = safe_path(base, str(tests.get("cwd") or "."))
    except OutsideWorktree as e:
        raise ConfigRefused(f"{CONFIG_NAME}: [tests].cwd is outside the worktree — {e}") from None
    if not cwd.is_dir():
        raise ConfigRefused(f"{CONFIG_NAME}: [tests].cwd {str(tests.get('cwd'))!r} is not a directory")

    _executable(argv[0], cwd)
    return VerifyConfig(argv=argv, cwd=cwd, source=" ".join(argv))
