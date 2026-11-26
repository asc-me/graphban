"""`gban rules import --prose`: open an import run for the customer's agent (GRPH-1952 / P92 D4).

The CLI does not read prose and nothing on the box does: the customer's own agent reads it
through the `graphban-rules-import` skill and files through `propose_rule` with the run's id.
So this verb lists the repo's prose sources at HEAD, opens one run with every one of them
UNREAD, and prints the skill invocation. Until an agent attaches and marks a source read, the
run's report lists it as unread and never reads as clean. The server enforces that too: a run
cannot be opened with a prose source already read.

A repo whose files cannot be listed is a refusal, never an empty list: "no prose sources" and
"could not look" are different reports, and the second must not open a clean run.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

SKILL = "graphban-rules-import"

#: The prose sources (P92 D1). Kept in step with `app.services.import_runs.is_prose`.
PROSE_FILES = ("CONTRIBUTING.md", "AGENTS.md", "CLAUDE.md", ".cursorrules",
               ".github/copilot-instructions.md")
PROSE_PREFIXES = ("docs/adr/", "doc/adr/", "adr/", "docs/architecture")
PROSE_SUFFIXES = (".md", ".markdown", ".txt", ".rst", ".adoc")


class Unreadable(Exception):
    """The repo could not be read, so no run is opened."""


def is_prose(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    return path in PROSE_FILES or base in ("CONTRIBUTING.md", "AGENTS.md", "CLAUDE.md") or (
        path.startswith(PROSE_PREFIXES) and path.lower().endswith(PROSE_SUFFIXES))


def _git(repo: Path, *args: str) -> str:
    try:
        done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                              timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Unreadable(f"could not run git in {repo}: {exc}") from exc
    if done.returncode != 0:
        raise Unreadable(f"could not read {repo} with `git {' '.join(args)}`: "
                         f"{(done.stderr or done.stdout).strip()[:300]}")
    return done.stdout


def head_commit(repo: Path) -> str:
    sha = _git(repo, "rev-parse", "HEAD").strip()
    if not sha:
        raise Unreadable(f"could not read HEAD of {repo}")
    return sha


def prose_sources(repo: Path, commit: str) -> list[str]:
    """Every prose path in the tree at `commit`. Raises `Unreadable`; never returns [] for a
    tree it could not list."""
    out = _git(repo, "ls-tree", "-r", "--name-only", "-z", commit)
    return sorted(p for p in out.split("\0") if p and is_prose(p))


def run_body(repo: Path) -> dict:
    commit = head_commit(repo)
    return {"commit": commit,
            "sources_unread": [{"path": p, "reader": "agent"}
                               for p in prose_sources(repo, commit)]}


def invocation(run: dict) -> str:
    """What to paste into the customer's agent, in any harness with MCP."""
    return (f"/{SKILL} import_run={run.get('id')} commit={run.get('commit')}")


def summary(run: dict, prog: str = "gban") -> str:
    unread = run.get("sources_unread") or []
    head = (f"{prog}: import run {run.get('id')} at {str(run.get('commit'))[:12]}: "
            f"{len(unread)} prose sources, all unread until an agent reads them")
    lines = [head]
    lines += [f"     unread  {s.get('path')}" for s in unread]
    lines += ["     in your agent (any harness with the Graphban MCP server), run:",
              f"       {invocation(run)}",
              ("     it files through `propose_rule` with that run; a person adopts the batch "
               "from the run's report.")]
    return "\n".join(lines)
