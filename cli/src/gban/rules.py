"""`gban rules import`: open an import run over the repo (P92 D1-D5).

`gban rules import` ships the repo's structured sources (CODEOWNERS, workflows,
`.gitattributes`, generated markers) at HEAD to the server, whose tested parsers read them and
file through the pipeline; no model reads them. Prose is listed unread for the customer's agent.
On a project that already holds adopted rules the server files only differences and reports
drift as notices (`source gone`, `source moved`) it never acts on (GRPH-1953 / P92 D5). A source
the parsers could not read, or a refused finding, exits non-zero, so the CI template
(`--ci-template`) reports it as a failed step, not a pass. Prose awaiting the agent does not.

`gban rules import --prose`: open an import run for the customer's agent (GRPH-1952 / P92 D4).

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


#: The structured sources the server's parsers read (P92 D1). The CLI only ships their text.
CODEOWNERS_PATHS = ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS")
LOCKFILES = ("uv.lock", "poetry.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
             "Cargo.lock", "Gemfile.lock", "go.sum", "composer.lock")
GENERATED_MARK = r"do not edit|code generated|@generated|auto-?generated"
HEAD_LINES = 5
#: A source larger than this is sent as its first lines; the parsers read no further.
MAX_BYTES = 256 * 1024


def _is_structured(path: str) -> bool:
    return (path in CODEOWNERS_PATHS or path == ".gitattributes"
            or (path.startswith(".github/workflows/") and path.count("/") == 2
                and path.endswith((".yml", ".yaml"))))


def _show(repo: Path, commit: str, path: str) -> str:
    return _git(repo, "show", f"{commit}:{path}")


def structured_files(repo: Path, commit: str) -> dict[str, str]:
    """path -> text at `commit` for every structured source: CODEOWNERS, workflows,
    `.gitattributes`, lockfiles (by name only) and the first lines of each file whose text
    carries a generated marker. Raises `Unreadable`; never returns {} for a tree it could not
    list."""
    tree = [p for p in _git(repo, "ls-tree", "-r", "--name-only", "-z", commit).split("\0") if p]
    out: dict[str, str] = {}
    for p in tree:
        if _is_structured(p):
            out[p] = _show(repo, commit, p)[:MAX_BYTES]
        elif p.rsplit("/", 1)[-1] in LOCKFILES:
            out[p] = ""  # a lockfile is generated by its name; its text is not needed
    # `git grep` exits 1 when nothing matches, which is an answer, not a failure.
    try:
        done = subprocess.run(["git", "-C", str(repo), "grep", "-l", "-I", "-i", "-E",
                               GENERATED_MARK, commit], capture_output=True, text=True,
                              timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Unreadable(f"could not search {repo} for generated markers: {exc}") from exc
    if done.returncode not in (0, 1):
        raise Unreadable(f"could not search {repo} for generated markers: "
                         f"{(done.stderr or done.stdout).strip()[:300]}")
    for line in done.stdout.splitlines():
        p = line.split(":", 1)[1] if line.startswith(commit + ":") else line
        if p and p not in out:
            out[p] = "\n".join(_show(repo, commit, p).splitlines()[:HEAD_LINES])
    return out


def default_branch(repo: Path) -> str:
    """The branch whose protection the server reads: origin's HEAD, else `main`."""
    try:
        ref = _git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD").strip()
    except Unreadable:
        return "main"
    return ref.rsplit("/", 1)[-1] or "main"


def import_body(repo: Path) -> dict:
    """The whole import: structured sources for the server's parsers, prose listed unread for
    the customer's agent."""
    commit = head_commit(repo)
    return {"commit": commit, "branch": default_branch(repo),
            "files": structured_files(repo, commit),
            "sources_unread": [{"path": p, "reader": "agent"}
                               for p in prose_sources(repo, commit)]}


BUCKET_WORDS = {"proposed": "proposed", "external": "EXTERNAL", "gap": "guidance + gap",
                "notice": "notice"}


def import_summary(run: dict, prog: str = "gban") -> str:
    """The run's report in three buckets, unread sources first, refusals named."""
    unread = run.get("sources_unread") or []
    findings = run.get("findings") or []
    lines = [f"{prog}: import run {run.get('id')} at {str(run.get('commit'))[:12]}: "
             f"{len(findings)} findings, {len(unread)} unread"
             + ("" if run.get("clean") else " (not clean)")]
    lines += [f"     unread  {s.get('path')}: {s.get('reason') or ''}".rstrip(": ")
              for s in unread]
    for b in ("proposed", "external", "gap"):
        for f in (x for x in findings if x.get("bucket") == b):
            tag = BUCKET_WORDS[b]
            what = f.get("label") or (f.get("proposal") or {}).get("rule_id") or ""
            lines.append(f"     {tag:<14}  {f.get('stage')}: {what} — {f.get('quote')}"
                         + (f"  REFUSED: {f['refused']}" if f.get("refused") else ""))
    lines += [f"     {'notice':<14}  {f.get('label')}: {f.get('text')}"
              for f in findings if f.get("bucket") == "notice"]
    if any(s.get("reader") == "agent" for s in unread):
        lines += ["     prose is read by your own agent (any harness with the Graphban MCP "
                  "server):", f"       {invocation(run)}"]
    lines.append("     nothing is adopted here: a person adopts the batch from the run's report "
                 "on the Rules page; a notice is shown, never acted on.")
    if failed(run):
        lines.append("     NOT CLEAN: a source went unread or a finding was refused.")
    return "\n".join(lines)


def failed(run: dict) -> bool:
    """The step fails on a source the parsers could not read or a refused finding. Prose
    awaiting the customer's agent is listed, not a failure: no parser reads it."""
    return (any(s.get("reader") != "agent" for s in run.get("sources_unread") or [])
            or any(f.get("refused") for f in run.get("findings") or []))


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
