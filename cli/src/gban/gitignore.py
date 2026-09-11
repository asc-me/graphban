"""Keep gban/gbfleet credentials out of `git add`.

The files this tool writes into a checkout — `.mcp.json`, Grok's project config, a
Cursor seat, a gbfleet enrolment instruction, a Swamp vault — are live keys. A warning
attached to writing them still writes them, and `git add .` does not read warnings.
So setup *writes the ignore lines*, and doctor *fails* if a file that exists would
still be committed. Asking git (`check-ignore`) rather than grepping `.gitignore`
is the load-bearing bit: an equivalent pattern (`.cursor/`, a user exclude file)
already does the job, and a string match would either duplicate it or miss it.

A path git already tracks is a different refusal. gitignore does not untrack, and
pretending it does is how a key that is already in the index keeps shipping.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

# Same three states as `gban.doctor`, inlined so this module cannot import doctor
# (doctor calls `check`, and a cycle here would make `gban doctor` fail to import).
PASS, FAIL, UNKNOWN = "PASS", "FAIL", "UNKNOWN"


def _line(side: str, status: str, name: str, detail: str = "") -> dict:
    return {"side": side, "status": status, "name": name, "detail": detail, "report": ""}


#: What we write. Concrete files gban and gbfleet actually emit, plus the two swamp
#: paths `docs/swamp.md` names. `.gbfleet-*` covers instruction, seat, probes and an
#: in-tree worktree pool without enumerating each one as it appears.
PATTERNS = (
    ".mcp.json",
    ".cursor/mcp.json",
    ".grok/config.toml",
    ".grok/mcp.json",
    ".gbfleet-*",
    ".swamp/",
    "graphban-swamp/",
)

#: What we ASK git about. A glob is not a path; these are the files that would be
#: committed. Directory patterns keep the trailing slash: `check-ignore .swamp`
#: does not honour `.swamp/`, but `check-ignore .swamp/` does. `.gbfleet-instruction`
#: is the representative of `.gbfleet-*` because it carries a live enrolment code.
PROBES = (
    ".mcp.json",
    ".cursor/mcp.json",
    ".grok/config.toml",
    ".grok/mcp.json",
    ".gbfleet-instruction",
    ".swamp/",
    "graphban-swamp/",
)

HEADER = "# Graphban — live keys and seats. Maintained by `gban setup`."


def is_repo(path: Path) -> bool:
    """`.git` is a directory in a clone and a file in a worktree."""
    return (path / ".git").exists()


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=10)


def usable(repo: Path) -> bool:
    """A `.git` marker is not a repository git will answer. Tests (and `--auto`)
    plant an empty `.git` directory as the marker; writing a gitignore that git
    then cannot honour would FAIL every one of them."""
    if not is_repo(repo):
        return False
    try:
        done = _git(repo, "rev-parse", "--is-inside-work-tree")
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0 and done.stdout.strip() == "true"


def tracked(repo: Path, rel: str) -> bool:
    try:
        done = _git(repo, "ls-files", "--error-unmatch", "--", rel)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def ignored(repo: Path, rel: str) -> bool:
    """Would `git add` skip this path? Tracked files are not ignored, even when a
    pattern matches — that is git's rule, and it is why a tracked credential is a
    FAIL rather than a write-to-gitignore."""
    try:
        done = _git(repo, "check-ignore", "-q", "--", rel)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _probe_for(pattern: str) -> str:
    if pattern == ".gbfleet-*":
        return ".gbfleet-instruction"
    return pattern


def missing(repo: Path) -> list[str]:
    """Patterns that would not actually ignore their probe path."""
    return [p for p in PATTERNS if not ignored(repo, _probe_for(p))]


def leaking(repo: Path) -> list[str]:
    """Probes that exist on disk or are tracked, and would still be committed."""
    out = []
    for rel in PROBES:
        disk = rel.rstrip("/")
        if tracked(repo, disk) or (repo / disk).exists():
            if not ignored(repo, rel):
                out.append(disk)
    return out


def ensure(repo: Path) -> list[dict]:
    """Append any missing patterns to `.gitignore`. Idempotent.

    Not a git repository: nothing to write (the caller already reports that). A
    pattern git already honours — however it is spelled — is left alone.
    """
    if not usable(repo):
        return []
    needed = missing(repo)
    if not needed:
        return [_line("config", PASS, "gitignore",
                      "credential paths are gitignored")]
    path = repo / ".gitignore"
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        block = "\n".join([HEADER, *needed]) + "\n"
        text = block if not existing else existing.rstrip("\n") + "\n\n" + block
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        return [_line("config", FAIL, "gitignore",
                      f"could not write {path}: {exc}. Refusing to leave a key "
                      "unignored — add the lines by hand, or use --scope user")]
    still = missing(repo)
    if still:
        return [_line("config", FAIL, "gitignore",
                      f"wrote {path} but git still would not ignore "
                      f"{', '.join(still)}")]
    return [_line("config", PASS, "gitignore",
                  f"added to {path}: {', '.join(needed)}")]


def check(repo: Path) -> list[dict]:
    """Doctor: fail if a credential file would be committed, not if the patterns
    are merely absent. Absence of the files is not a clean result either — that
    prints UNKNOWN, because 'nobody has written a key here yet' is not 'safe'.
    """
    if not usable(repo):
        return []
    live = leaking(repo)
    if live:
        tracked_ones = [p for p in live if tracked(repo, p)]
        if tracked_ones:
            return [_line("local", FAIL, "gitignore",
                          f"git tracks {', '.join(tracked_ones)} — gitignore will "
                          "not stop a commit. `git rm --cached` them, or use "
                          "--scope user")]
        return [_line("local", FAIL, "gitignore",
                      f"{', '.join(live)} is not gitignored and would be committed. "
                      "`gban setup` adds the lines")]
    uncovered = [p for p in PROBES if not ignored(repo, p)]
    if uncovered:
        return [_line("local", UNKNOWN, "gitignore",
                      "credential paths are not gitignored yet; `gban setup` adds "
                      "the lines so a later spawn cannot commit them")]
    return [_line("local", PASS, "gitignore", "credential paths are gitignored")]
