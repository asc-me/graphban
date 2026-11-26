"""`gban rules import`: the structured read and its rescan (GRPH-1953 / P92 D5), and `--prose`,
which opens an import run for the customer's agent (GRPH-1952 / P92 D4).

**Structured.** The CLI reads the structured sources at HEAD (CODEOWNERS, workflows,
`.gitattributes`, lockfiles, files whose header says DO NOT EDIT) and sends their text to the
server, whose tested parsers read them. On a project that already holds adopted rules the server
files only differences and reports drift as notices (`source gone`, `source moved`) it never acts
on. A run with an unread source is not clean, and the command exits non-zero, so the CI template
(`--ci-template`) reports it as a failed step, not a pass.

**Prose.**
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


#: Generated-file headers, as the server's parser reads them (`import_sources._GENERATED_HEADER`).
GENERATED_HEADER = r"do not edit|code generated|@generated|auto-?generated"
LOCKFILES = ("uv.lock", "poetry.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
             "Cargo.lock", "Gemfile.lock", "go.sum", "composer.lock")
HEAD_LINES = 5
#: How many DO-NOT-EDIT candidates are sent. Past it the rest are listed unread, never dropped.
MAX_HEADERS = 500
#: Past this a structured file is listed unread rather than sent.
MAX_BYTES = 512 * 1024


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


# ---- the structured read (GRPH-1953 / P92 D5) ----------------------------------------------

def is_structured(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    return (base in ("CODEOWNERS", ".gitattributes")
            or (path.startswith(".github/workflows/") and "/" not in path[18:]
                and path.endswith((".yml", ".yaml"))))


def _show(repo: Path, commit: str, path: str) -> str:
    return _git(repo, "show", f"{commit}:{path}")


def _header_candidates(repo: Path, commit: str) -> list[str]:
    """Paths whose text matches a generated-file header anywhere; the server checks the head.
    `git grep` exits 1 for "no match", which is an answer, not a failure."""
    try:
        done = subprocess.run(["git", "-C", str(repo), "grep", "-I", "-l", "-i", "-E",
                               GENERATED_HEADER, commit], capture_output=True, text=True,
                              timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Unreadable(f"could not search {repo} for generated-file headers: {exc}") from exc
    if done.returncode == 1:
        return []
    if done.returncode != 0:
        raise Unreadable(f"could not search {repo} for generated-file headers: "
                         f"{(done.stderr or done.stdout).strip()[:300]}")
    pre = f"{commit}:"
    return sorted(line[len(pre):] if line.startswith(pre) else line
                  for line in done.stdout.splitlines() if line)


def default_branch(repo: Path) -> str | None:
    try:
        ref = _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").strip()
    except Unreadable:
        return None
    return ref.split("/", 1)[1] if "/" in ref else (ref or None)


def scan_body(repo: Path, branch: str | None = None) -> dict:
    """What `POST /rules/import-runs/scan` takes. Raises `Unreadable` when the tree cannot be
    listed; a single file that cannot be sent is listed unread with the reason."""
    commit = head_commit(repo)
    tree = [p for p in _git(repo, "ls-tree", "-r", "--name-only", "-z", commit).split("\0") if p]
    files: dict[str, str] = {}
    unread: list[dict] = []
    for p in sorted(tree):
        if is_structured(p):
            try:
                text = _show(repo, commit, p)
            except Unreadable as exc:
                unread.append({"path": p, "reason": str(exc)[:200]})
                continue
            if len(text.encode()) > MAX_BYTES:
                unread.append({"path": p, "reason": f"larger than {MAX_BYTES} bytes"})
                continue
            files[p] = text
        elif p.rsplit("/", 1)[-1] in LOCKFILES:
            files[p] = ""  # a lockfile is generated by its name; its text is not needed
    heads = [p for p in _header_candidates(repo, commit) if p not in files]
    for p in heads[:MAX_HEADERS]:
        try:
            files[p] = "\n".join(_show(repo, commit, p).splitlines()[:HEAD_LINES])
        except Unreadable as exc:
            unread.append({"path": p, "reason": str(exc)[:200]})
    if len(heads) > MAX_HEADERS:
        unread.append({"path": f"generated-file headers: {len(heads) - MAX_HEADERS} more files",
                       "reason": f"only the first {MAX_HEADERS} candidates were read"})
    return {"commit": commit, "branch": branch or default_branch(repo), "files": files,
            "sources_unread": unread}


def scan_summary(rep: dict, prog: str = "gban") -> str:
    fs = rep.get("findings") or []
    filed = [f for f in fs if f.get("proposal_id") or f.get("guidance_id")]
    notices = [f for f in fs if f.get("bucket") == "notice"]
    refused = [f for f in fs if f.get("refused")]
    unread = rep.get("sources_unread") or []
    lines = [f"{prog}: import run {rep.get('id')} at {str(rep.get('commit'))[:12]}: "
             f"{len(filed)} filed, {len(notices)} notices, {len(unread)} unread"]
    for f in filed:
        p = f.get("proposal") or {}
        what = (f"{p.get('kind')} {p.get('rule_id')} ({p.get('direction')})" if p
                else f"guidance {f.get('label')}")
        lines.append(f"     filed   {what}  [{f.get('source', {}).get('path')}]")
    for f in notices:
        lines.append(f"     {f.get('label')}  {f.get('text')}")
    for f in refused:
        lines.append(f"     refused {f.get('source', {}).get('path')}: {f.get('refused')}")
    for s in unread:
        lines.append(f"     unread  {s.get('path')}" + (f" — {s['reason']}" if s.get("reason")
                                                         else ""))
    lines.append("     nothing is adopted here: a person adopts from the run's report on the "
                 "Rules page.")
    if not rep.get("clean"):
        lines.append("     NOT CLEAN: a source went unread or a finding was refused.")
    return "\n".join(lines)


def _cli_version() -> str:
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("graphban-cli")
    except PackageNotFoundError:
        return ""


#: The paths whose change re-runs the rescan: every source the structured parsers read.
CI_PATHS = (".github/workflows/**", "**/CODEOWNERS", "**/.gitattributes", *(
    f"**/{x}" for x in LOCKFILES))


def ci_template(branch: str = "main", version: str | None = None) -> str:
    """The GitHub Actions workflow that runs the rescan when a cited source changes on the
    default branch. It files proposals and adopts nothing; an unread source fails the step."""
    v = _cli_version() if version is None else version
    pin = f"graphban-cli=={v}" if v else "graphban-cli"
    paths = "\n".join(f'      - "{p}"' for p in CI_PATHS)
    return f"""# Printed by `gban rules import --ci-template`{f' (graphban-cli {v})' if v else ''}.
# Re-print it after upgrading gban: it is versioned with the parsers whose findings it files.
#
# Runs the rules rescan when a source the import reads changes on `{branch}`. It FILES
# proposals and drift notices on the project's Rules page and ADOPTS NOTHING: a person adopts
# from the run's report. A source the run could not read (a token without permission, a path in
# another repo) makes the run unclean and this step fails; it is never reported as a pass.
#
# Set up once: repository variables GRAPHBAN_URL and GRAPHBAN_PROJECT, and a secret
# GRAPHBAN_SESSION holding the contents of ~/.graphban/session.json from `gban login`.
name: graphban rules rescan
on:
  push:
    branches: [{branch}]
    paths:
{paths}
  workflow_dispatch:
permissions:
  contents: read
jobs:
  rescan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install gban
        run: pipx install "{pin}"
      - name: Rescan the rules sources
        env:
          GRAPHBAN_URL: ${{{{ vars.GRAPHBAN_URL }}}}
          GRAPHBAN_PROJECT: ${{{{ vars.GRAPHBAN_PROJECT }}}}
          GRAPHBAN_HOME: ${{{{ runner.temp }}}}/graphban
          GRAPHBAN_SESSION: ${{{{ secrets.GRAPHBAN_SESSION }}}}
        run: |
          mkdir -p "$GRAPHBAN_HOME"
          (umask 077 && printf '%s' "$GRAPHBAN_SESSION" > "$GRAPHBAN_HOME/session.json")
          gban rules import --branch {branch}
"""
