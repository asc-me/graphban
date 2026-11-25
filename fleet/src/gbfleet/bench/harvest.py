"""`gbfleet bench harvest` / `adopt`: candidate tasks from the repo's merge history.

A candidate is a merge on the default branch whose added or modified test files fail at
the merge's first parent and pass at the merge, each 3 times. Harvest only reads git and
runs checks; it writes nothing to the server. `adopt` posts chosen candidates to the suite.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .suite import Check
from .validate import FAIL, PASS, component_dir, run_check, split_by_component

RUNS = 3

#: Mirror of `_TEST_RE` in backend/app/services/harness.py (PRD-41); gbfleet must not import
#: the backend. A test reads that file as text and asserts these are equal.
TEST_RE_SOURCE = (
    r"(^|/)(tests?|__tests__|spec)(/|$)|(^|/)test_[^/]+$|_test\.(py|ts|tsx|js)$"
    r"|\.test\.(ts|tsx|js)$|\.spec\.(ts|tsx|js)$"
)
_TEST_RE = re.compile(TEST_RE_SOURCE, re.I)
_SQUASH_RE = re.compile(r"\(#\d+\)\s*$")
_ITEM_RE = re.compile(r"\b[A-Z]{2,}-\d+\b")

#: Component kind -> test command. Python runs pytest on the files; JS the package
#: manager's runner on the files.
PY_COMMAND = "python -m pytest -q"
JS_COMMANDS = {"pnpm-lock.yaml": "pnpm test --", "yarn.lock": "yarn test"}
JS_DEFAULT = "npm test --"

DROPPED = ("flaky", "green_at_parent", "red_at_merge", "no_tests", "mutation_not_applicable",
           "no_spec")


@dataclass
class Candidate:
    id: str
    merge_sha: str
    base_sha: str
    title: str
    command: str
    tests: list[str]
    touchpoints: list[str]
    item: str = ""
    sabotage: bool = False
    committed_at: int = 0
    #: The merge commit body (a squash merge carries the PR body); the spec when no item is named.
    body: str = ""


@dataclass
class Dropped:
    merge_sha: str
    reason: str
    title: str = ""
    detail: str = ""


@dataclass
class Harvest:
    candidates: list[Candidate] = field(default_factory=list)
    dropped: list[Dropped] = field(default_factory=list)


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], check=False, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout


def parse_since(text: str) -> str:
    """`180d` / `4w` / `12h` to a git `--since` value; anything else passes through."""
    m = re.fullmatch(r"(\d+)([dwh])", text.strip())
    if not m:
        return text
    unit = {"d": "days", "w": "weeks", "h": "hours"}[m.group(2)]
    return f"{m.group(1)} {unit} ago"


def is_test_path(path: str) -> bool:
    return bool(_TEST_RE.search(path.replace("\\", "/")))


def command_for(repo: Path, tests: list[str]) -> tuple[str, list[str]]:
    """The component's test command and the test files it covers. Python wins a mixed merge.
    The interpreter and working directory are resolved at run time (`validate.resolve_check`);
    the JS lockfile is looked up in the tests' component directory."""
    py = [t for t in tests if t.endswith(".py")]
    if py:
        return PY_COMMAND, py
    js = [t for t in tests if t.endswith((".ts", ".tsx", ".js", ".jsx"))]
    cmd = JS_DEFAULT
    comp = repo / component_dir(repo, js)
    for lock, c in JS_COMMANDS.items():
        if (comp / lock).exists():
            cmd = c
    return cmd, js


def _merges(repo: Path, branch: str, since: str, limit: int) -> list[tuple[str, str, int, str]]:
    """First-parent commits of `branch`: two-parent merges, and single-parent commits whose
    subject ends in `(#<n>)` (squash merges). Anything else is not a merged change."""
    args = ["log", "--first-parent", "--format=%H%x1f%P%x1f%ct%x1f%s%x1f%b%x1e"]
    if since:
        args.append(f"--since={parse_since(since)}")
    args.append(branch)
    out = []
    for rec in _git(repo, *args).split("\x1e"):
        rec = rec.strip("\n")
        if not rec.strip():
            continue
        sha, parents, ct, subject, body = (rec.split("\x1f") + ["", "", "", "", ""])[:5]
        n = len(parents.split())
        if n == 2 or (n == 1 and _SQUASH_RE.search(subject)):
            out.append((sha.strip(), subject, int(ct or 0), body))
    return out[:limit] if limit else out


def _tally(repo: Path, ref: str, check: Check, overlay: str, runs: int) -> tuple[str, str]:
    """`pass` / `fail` when every run agrees, `flaky` when they split, else the
    inconclusive outcome (and its detail) of the first run that was one."""
    seen: list[str] = []
    for _ in range(runs):
        outcome, why = run_check(repo, ref, check, overlay=overlay)
        if outcome not in (PASS, FAIL):
            return outcome, why
        seen.append(outcome)
    if all(o == PASS for o in seen):
        return PASS, ""
    if all(o == FAIL for o in seen):
        return FAIL, ""
    return "flaky", f"{seen.count(PASS)} of {runs} runs passed"


def _judge(repo: Path, parent: str, sha: str, parts: list[Check], runs: int) -> tuple[str, str] | None:
    """None when every part is red at `parent` and green at `sha`; else (reason, detail)."""
    many = len(parts) > 1

    def where(c: Check) -> str:
        return f"{component_dir(repo, c.tests) or '.'}: " if many else ""

    for c in parts:
        at_parent, why = _tally(repo, parent, c, sha, runs)
        if at_parent == PASS:
            return "green_at_parent", where(c).rstrip(": ")
        if at_parent != FAIL:
            return at_parent, where(c) + why
    for c in parts:
        at_merge, why = _tally(repo, sha, c, "", runs)
        if at_merge == FAIL:
            return "red_at_merge", where(c).rstrip(": ")
        if at_merge != PASS:
            return at_merge, where(c) + why
    return None


def harvest(repo: Path, *, branch: str = "HEAD", since: str = "180d", limit: int = 50,
            runs: int = RUNS, red_sabotage: frozenset[str] = frozenset(),
            commands: dict[str, str] | None = None) -> Harvest:
    """Scan the first-parent merges of `branch`. `runs` is 3 in use; a test may lower it
    to prove that one run lets a flaky check through. `commands` overrides the test command
    by component kind (`py`, `js`)."""
    repo = Path(repo)
    result = Harvest()
    for sha, title, ct, body in _merges(repo, branch, since, limit):
        parents = _git(repo, "rev-list", "--parents", "-n", "1", sha).split()[1:]
        if not parents:
            continue
        parent = parents[0]
        changed = _git(repo, "diff", "--name-only", "--diff-filter=AM", parent, sha).splitlines()
        tests = [p for p in changed if is_test_path(p)]
        if not tests:
            result.dropped.append(Dropped(sha, "no_tests", title))
            continue
        cmd, files = command_for(repo, tests)
        if commands:
            cmd = commands.get("py" if cmd == PY_COMMAND else "js", cmd)
        if not files:
            result.dropped.append(Dropped(sha, "no_tests", title, "no runnable test files"))
            continue
        # One check per component: a merge whose tests span backend/ and fleet/ is a
        # candidate only when each component's tests are red at the parent and green at it.
        parts = split_by_component(repo, Check(cmd, tuple(files)))
        drop = _judge(repo, parent, sha, parts, runs)
        if drop:
            result.dropped.append(Dropped(sha, drop[0], title, drop[1]))
            continue
        m = _ITEM_RE.search(title + "\n" + body)
        item = m.group(0) if m else ""
        result.candidates.append(Candidate(
            id=f"h-{sha[:10]}", merge_sha=sha, base_sha=parent, title=title, command=cmd,
            tests=files, touchpoints=[p for p in changed if not is_test_path(p)], item=item,
            sabotage=bool(item and item in red_sabotage), committed_at=ct, body=body.strip()))
    # Red-sabotage items first, then the most recent.
    result.candidates.sort(key=lambda c: (not c.sabotage, -c.committed_at))
    return result


def store_path(repo: Path) -> Path:
    git_dir = _git(repo, "rev-parse", "--absolute-git-dir").strip()
    return Path(git_dir) / "gbfleet-bench-candidates.json"


def save(repo: Path, found: Harvest) -> Path:
    path = store_path(repo)
    path.write_text(json.dumps([asdict(c) for c in found.candidates], indent=2), encoding="utf-8")
    return path


def load_candidates(repo: Path) -> dict[str, Candidate]:
    path = store_path(repo)
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {r["id"]: Candidate(**r) for r in rows}


_ACCEPT_HEAD = re.compile(r"^#{1,6}\s*acceptance\b.*$", re.I | re.M)
_TRAILER = re.compile(r"^(co-authored-by|signed-off-by|generated with)\b.*$", re.I | re.M)


def _bullets(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        m = re.match(r"\s*(?:[-*]|\d+[.)])\s+(.*\S)", line)
        if m:
            out.append(m.group(1).strip())
    return out


def spec_from_item(item: dict) -> dict | None:
    """`{description, acceptance}` from a server item, or None when it has no description.
    The `## Acceptance` section's bullets are the acceptance; the description keeps the rest."""
    text = str(item.get("description") or "").strip()
    if not text:
        return None
    m = _ACCEPT_HEAD.search(text)
    if not m:
        return {"description": text, "acceptance": []}
    rest = text[m.end():]
    nxt = re.search(r"^#{1,6}\s", rest, re.M)
    section = rest[:nxt.start()] if nxt else rest
    head = (text[:m.start()] + (rest[nxt.start():] if nxt else "")).strip()
    return {"description": head or text, "acceptance": _bullets(section)}


def spec_from_body(body: str) -> dict | None:
    """The merge/PR body as the spec, minus commit trailers; None when nothing is left."""
    text = _TRAILER.sub("", body or "").strip()
    if not text:
        return None
    return {"description": text, "acceptance": _bullets(text) if _ACCEPT_HEAD.search(text) else []}


def resolve_spec(c: Candidate, fetch_item=None) -> dict | None:
    """The spec a child is given for `c`: the named item's (via `fetch_item(id) -> dict | None`),
    else the merge body. None means `no_spec`: the candidate must not be adopted."""
    if c.item and fetch_item is not None:
        try:
            item = fetch_item(c.item)
        except Exception:  # an unreadable item falls back to the body, never to a title guess
            item = None
        spec = spec_from_item(item) if isinstance(item, dict) else None
        if spec:
            return spec
    return spec_from_body(c.body)


def to_task(c: Candidate, capability: str = "B7", spec: dict | None = None) -> dict:
    """The suite-store task body for a candidate (a build task; the merge is the reference).
    `spec` is `resolve_spec`'s answer; without one the task falls back to the bare title."""
    n = len(c.touchpoints)
    spec = spec or {"description": c.title + (f" ({c.item})" if c.item else ""), "acceptance": []}
    # The check's test files are held out of the child's worktree, so they are not named here.
    acceptance = list(spec["acceptance"]) + [
        "The held-out tests for this change pass (their files are withheld from your worktree)"]
    return {
        "id": c.id, "kind": "build", "base_sha": c.base_sha, "reference": c.merge_sha,
        "title": c.title[:256], "description": spec["description"],
        "acceptance": acceptance,
        "touchpoints": c.touchpoints, "capabilities": [capability],
        "size_band": "S" if n <= 2 else "M" if n <= 6 else "L",
        "check": {"command": c.command, "tests": c.tests},
    }
