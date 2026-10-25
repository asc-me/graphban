#!/usr/bin/env python3
"""Run the security scanners at a pull request's head and attest `scans_clean` (GRPH-P57 S3).

A release merge (GRPH-P57 D6) requires a passing `scans_clean` naming the PR head. This is
the only writer of that predicate, and it attests ONLY from output this process produced:

- it recomputes which components the diff touches, every run, from
  `git diff --name-status <merge-base>..<head>` — nothing is cached from an earlier run, so a
  rebase that brings a component in brings its scan in too;
- it runs each mapped tool on the WHOLE component root, never on the changed files, so a
  vulnerable pin in an untouched lockfile is still seen;
- it asks every tool for JSON and parses it. Exit codes are never read as the verdict:
  `pip-audit`, `bandit` and `npm audit` all exit nonzero when they find something, and a
  network timeout exits nonzero too.

Every required (tool, component) lands in exactly one of four states, and the three failures
are different strings because they ask different people for different things:

- **never-run** — no output, a crash, a timeout, unparseable output, a diff that could not be
  computed, or a file no component covers. "Run it again" or "map the file".
- **failed check** — parseable output with findings. "Fix the code".
- **stale** — the checkout is not `pull_request.head.sha` (GitHub's default PR checkout is the
  merge commit). "This run scanned something else".
- **clean** — parseable output, no findings.

`scans_clean` passes only when every required tool on every touched component is clean. It is
posted on its OWN receipt (`attest_ci.receipt`), never beside `suite_green`, and with
`head_commit` on every run, pass or fail, so an older pass stops covering a head that failed.

The scan rows are also recorded (`record_scan`, explicit `category`) so the Stack overlay can
show them. The attestation never depends on those rows: a failed post is logged and changes
nothing, and nothing here ever READS the ledger's scan cells — a clean row some other
credential wrote is not evidence this run checked anything.

    GRAPHBAN_URL=... GRAPHBAN_GATE_KEY=... python scripts/scan_ci.py \
        --base "$BASE_SHA" --head "$HEAD_SHA" --branch "$REF" --text "$PR_TITLE $PR_BODY"
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import urllib.error
from dataclasses import dataclass, field

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import attest_ci  # noqa: E402

PREDICATE = "scans_clean"

#: Component prefixes — `COMPONENT_PATHS` in backend/app/services/scans.py. Repeated rather
#: than imported because this runs on a bare `python3` with no backend installed;
#: `test_scan_ci` asserts the two agree.
COMPONENTS = ("web/", "backend/", "fleet/", "cli/")

#: Which security tools run on which component (PRD D3). Only these run — not all six. A new
#: component or tool is a code change here, never a config file: an editable list is how a
#: tool that checks nothing gets declared as security.
TOOLS: dict[str, tuple[str, ...]] = {
    "backend/": ("pip-audit", "bandit"),
    "fleet/": ("pip-audit", "bandit"),
    "cli/": ("pip-audit", "bandit"),
    "web/": ("npm-audit",),
}

#: Files outside the four prefixes that touch a component, mirroring CI's `paths-filter`
#: (`.github/workflows/ci.yml`, the `backend` filter). Shared with the tests on purpose.
MAPPED: tuple[tuple[str, str], ...] = (
    (".github/workflows/ci.yml", "backend/"),
    ("scripts/**", "backend/"),
    ("docs/**", "backend/"),
    ("AGENTS.md", "backend/"),
)

#: Files that touch nothing. Anything else outside the prefixes is never-run "no component
#: covers <path>", so a new code directory blocks the release until it is mapped instead of
#: passing unscanned. Hardcoded beside `TOOLS`, for the same reason.
NOT_CODE: tuple[str, ...] = (
    "*.md",  # outside docs/ — `docs/**` and AGENTS.md are matched by MAPPED first
    ".gitignore", "*/.gitignore",
    ".gitattributes", "*/.gitattributes",
    ".editorconfig",
    "LICENSE", "LICENSE.*", "COPYING", "NOTICE",
)

#: Seconds before a tool counts as hung. A timeout is never-run, never clean.
TOOL_TIMEOUT = 900.0

NEVER_RUN, FAILED, STALE, CLEAN = "never-run", "failed check", "stale", "clean"


class DiffError(RuntimeError):
    """The touched set could not be computed. Never-run, with its own detail."""


@dataclass
class Touched:
    #: prefix -> files that made it count (a file outside the prefix shows as the reason).
    components: dict[str, list[str]] = field(default_factory=dict)
    #: Files outside every prefix that no mapping and no not-code rule covers.
    uncovered: list[str] = field(default_factory=list)
    #: Every file considered, in diff order.
    considered: list[str] = field(default_factory=list)

    def describe(self) -> str:
        if not self.components:
            return f"none (considered: {', '.join(self.considered) or 'no files'})"
        parts = []
        for prefix in COMPONENTS:
            files = self.components.get(prefix)
            if files is None:
                continue
            via = [f for f in files if not f.startswith(prefix)]
            direct = [f for f in files if f.startswith(prefix)]
            parts.append(prefix if direct or not via else f"{prefix} (via {', '.join(via)})")
        return ", ".join(parts)


@dataclass
class Result:
    tool: str
    prefix: str
    state: str
    reason: str = ""
    findings: list[dict] = field(default_factory=list)

    def line(self) -> str:
        head = " ".join(x for x in (self.state, self.tool, self.prefix) if x)
        return f"{head}: {self.reason}" if self.reason else head


# ---- the diff ---------------------------------------------------------------------------

def _git(repo: pathlib.Path, *args: str) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                             timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise DiffError(f"git {' '.join(args)}: {e}") from e
    if out.returncode != 0:
        raise DiffError(f"git {' '.join(args)}: {_tail(out.stderr or out.stdout)}")
    return out.stdout


def changed_files(repo: pathlib.Path, base: str, head: str) -> list[str]:
    """Every path the diff names, both sides of a rename or copy, deletions included.

    `--name-status` rather than `--name-only` because a rename's OLD side is a real change to
    the component it left — and a deletion-only PR (a removed lockfile pin, a removed check) is
    a change that still needs the component scanned.
    """
    if not base or not head:
        raise DiffError("no base or head commit given")
    merge_base = _git(repo, "merge-base", base, head).strip()
    raw = _git(repo, "diff", "--name-status", "-M", "-z", f"{merge_base}..{head}")
    fields = raw.split("\0")
    out: list[str] = []
    i = 0
    while i < len(fields):
        status = fields[i]
        if not status:
            i += 1
            continue
        n = 2 if status[0] in "RC" else 1
        for path in fields[i + 1:i + 1 + n]:
            if path and path not in out:
                out.append(path)
        i += 1 + n
    return out


def touched(files: list[str]) -> Touched:
    """Which components `files` touch, and why. Prefix match, no exclusion for tests."""
    t = Touched(considered=list(files))
    for path in files:
        prefix = next((p for p in COMPONENTS if path.startswith(p)), None)
        if prefix is None:
            prefix = next((p for pat, p in MAPPED if fnmatch.fnmatch(path, pat)), None)
        if prefix is not None:
            t.components.setdefault(prefix, []).append(path)
        elif not any(fnmatch.fnmatch(path, pat) for pat in NOT_CODE):
            t.uncovered.append(path)
    return t


# ---- the tools --------------------------------------------------------------------------

def _tail(text: str, limit: int = 300) -> str:
    flat = " ".join((text or "").split())
    return flat[-limit:] if flat else "(no output)"


@dataclass
class Run:
    code: int | str  # exit status, or "timeout" / "not found"
    stdout: str
    stderr: str


def _exec(argv: list[str], cwd: pathlib.Path, timeout: float) -> Run:
    try:
        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return Run("not found", "", f"{argv[0]} is not on PATH")
    except subprocess.TimeoutExpired as e:
        err = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        return Run("timeout", "", f"no result after {timeout:.0f}s {err}")
    return Run(p.returncode, p.stdout, p.stderr)


def _pip_audit(root: pathlib.Path, timeout: float) -> Run:
    """Audit the component's pinned dependency set.

    With a `uv.lock`, that is the lockfile exported to requirements — the set that actually
    installs, transitive pins included. Without one, pip-audit's local-project mode reads the
    manifest. Either way it is the whole component, never a file list from the diff.
    """
    if (root / "uv.lock").is_file():
        with tempfile.TemporaryDirectory() as tmp:
            reqs = pathlib.Path(tmp) / "requirements.txt"
            export = _exec(["uv", "export", "--frozen", "--no-hashes", "--no-emit-project",
                            "--format", "requirements-txt", "-o", str(reqs)], root, timeout)
            if export.code != 0 or not reqs.is_file():
                return Run(export.code if export.code != 0 else 1, "",
                           f"uv export failed: {export.stderr or export.stdout}")
            return _exec(["pip-audit", "-f", "json", "--progress-spinner", "off",
                          "--disable-pip", "--no-deps", "-r", str(reqs)], root, timeout)
    return _exec(["pip-audit", "-f", "json", "--progress-spinner", "off", "."], root, timeout)


def _bandit(root: pathlib.Path, timeout: float) -> Run:
    argv = ["bandit", "-r", ".", "-f", "json", "-q"]
    pyproject = root / "pyproject.toml"
    if pyproject.is_file() and "[tool.bandit" in pyproject.read_text(encoding="utf-8"):
        argv[1:1] = ["-c", "pyproject.toml"]
    return _exec(argv, root, timeout)


def _npm_audit(root: pathlib.Path, timeout: float) -> Run:
    # The lockfile decides the client: `npm audit` cannot read a pnpm lockfile.
    client = "pnpm" if (root / "pnpm-lock.yaml").is_file() else "npm"
    return _exec([client, "audit", "--json"], root, timeout)


def _parse_pip_audit(data: object) -> list[dict]:
    deps = data.get("dependencies") if isinstance(data, dict) else data
    if not isinstance(deps, list):
        raise ValueError("no `dependencies` list")
    out = []
    for d in deps:
        if not isinstance(d, dict):
            raise ValueError("dependency entry is not an object")
        for v in d.get("vulns") or []:
            fix = ", ".join(v.get("fix_versions") or []) or "no fix"
            out.append({"severity": "high", "path": "",
                        "summary": f"{d.get('name')} {d.get('version')}: {v.get('id')} "
                                   f"(fix: {fix})"})
    return out


def _parse_bandit(data: object) -> list[dict]:
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError("no `results` list")
    out = [{"severity": str(r.get("issue_severity") or "medium").lower(),
            "path": str(r.get("filename") or "").removeprefix("./"),
            "summary": f"{r.get('test_id')}: {r.get('issue_text')} "
                       f"({str(r.get('filename') or '').removeprefix('./')}:"
                       f"{r.get('line_number')})"}
           for r in data["results"]]
    if not out and data.get("errors"):
        # Files bandit could not read were not scanned. That is not clean.
        raise ValueError(f"bandit could not scan {len(data['errors'])} file(s): "
                         f"{data['errors'][0]}")
    return out


def _parse_npm_audit(data: object) -> list[dict]:
    if not isinstance(data, dict):
        raise ValueError("not an object")
    if "error" in data:
        raise ValueError(f"audit error: {data['error']}")
    if isinstance(data.get("advisories"), dict):  # pnpm / npm v6
        items = [(a.get("module_name"), a.get("severity"), a.get("title"))
                 for a in data["advisories"].values()]
    elif isinstance(data.get("vulnerabilities"), dict):  # npm v7+
        items = [(name, v.get("severity"), "vulnerable")
                 for name, v in data["vulnerabilities"].items()]
    else:
        raise ValueError("neither `advisories` nor `vulnerabilities`")
    return [{"severity": str(sev or "medium"), "path": "",
             "summary": f"{name}: {title} ({sev})"} for name, sev, title in items]


RUNNERS = {
    "pip-audit": (_pip_audit, _parse_pip_audit),
    "bandit": (_bandit, _parse_bandit),
    "npm-audit": (_npm_audit, _parse_npm_audit),
}


def scan(tool: str, prefix: str, repo: pathlib.Path, *, timeout: float = TOOL_TIMEOUT) -> Result:
    """Run one tool on one component root and classify its OWN output. Exit code is context."""
    run, parse = RUNNERS[tool]
    root = repo / prefix
    if not root.is_dir():
        # A deletion-only PR that removed the whole component: nothing left to scan is not a
        # clean scan of something.
        return Result(tool, prefix, NEVER_RUN, f"tool error: {tool} exit n/a: "
                                                f"{prefix} does not exist at this head")
    r = run(root, timeout)
    if not (r.stdout or "").strip():
        if r.code == 0:
            return Result(tool, prefix, NEVER_RUN, "no output from this run")
        return Result(tool, prefix, NEVER_RUN,
                      f"tool error: {tool} exit {r.code}: {_tail(r.stderr)}")
    try:
        findings = parse(json.loads(r.stdout))
    except (ValueError, TypeError, AttributeError) as e:
        return Result(tool, prefix, NEVER_RUN,
                      f"tool error: {tool} exit {r.code}: unparseable output ({e}): "
                      f"{_tail(r.stderr or r.stdout)}")
    if findings:
        shown = "; ".join(f["summary"] for f in findings[:3])
        more = f" (+{len(findings) - 3} more)" if len(findings) > 3 else ""
        return Result(tool, prefix, FAILED, f"{len(findings)} finding(s): {shown}{more}",
                      findings)
    return Result(tool, prefix, CLEAN)


# ---- the verdict ------------------------------------------------------------------------

@dataclass
class Verdict:
    passed: bool
    detail: str
    results: list[Result]
    touched: Touched | None


def _scan_touched(repo: pathlib.Path, t: Touched, head: str, timeout: float,
                  what: str) -> list[Result]:
    """Every required tool on every component in `t`, or stale if HEAD is not `head`."""
    results: list[Result] = []
    try:
        actual = _git(repo, "rev-parse", "HEAD").strip()
    except DiffError as e:
        actual = f"unknown ({e})"
    for prefix in COMPONENTS:
        if prefix not in t.components:
            continue
        for tool in TOOLS[prefix]:
            if actual != head:
                results.append(Result(tool, prefix, STALE,
                                      f"checked out {actual[:12]}, {what} is {head[:12]}"))
            else:
                results.append(scan(tool, prefix, repo, timeout=timeout))
    if actual != head and not any(r.state == STALE for r in results):
        # Nothing touched needed a tool, but this run still did not look at the head.
        results.append(Result("", "", STALE, f"checked out {actual[:12]}, {what} is "
                                             f"{head[:12]}"))
    return results


def _verdict(results: list[Result], t: Touched | None, head: str) -> Verdict:
    failures = [r for r in results if r.state != CLEAN]
    passed = not failures
    ran = [f"{r.tool} {r.prefix} {r.state}" for r in results if r.tool and r.state != STALE]
    parts = [f"scans_clean {'passed' if passed else 'FAILED'} at {head[:12] or '?'}",
             f"touched: {t.describe() if t else 'unknown'}",
             f"ran: {', '.join(ran) or 'nothing'}"]
    if failures:
        parts.append("failures: " + "; ".join(r.line() for r in failures))
    return Verdict(passed, ". ".join(parts), results, t)


def evaluate(repo: pathlib.Path, *, base: str, head: str,
             timeout: float = TOOL_TIMEOUT) -> Verdict:
    """Everything this run checked, at `head`. Reads nothing but git and its own tools."""
    results: list[Result] = []
    t: Touched | None = None
    try:
        t = touched(changed_files(repo, base, head))
    except DiffError as e:
        results.append(Result("", "", NEVER_RUN,
                              f"diff not computed: {e} (shallow clone? needs fetch-depth: 0)"))

    if t is not None:
        for path in t.uncovered:
            results.append(Result("", "", NEVER_RUN, f"no component covers {path}"))
        results.extend(_scan_touched(repo, t, head, timeout, "pull request head"))
    return _verdict(results, t, head)


def evaluate_tree(repo: pathlib.Path, *, head: str,
                  timeout: float = TOOL_TIMEOUT) -> Verdict:
    """`scans_clean` for a whole tree at `head`: every component, no diff (GRPH-1114).

    A release ships every component, so the touched set is all of them rather than a diff
    against some base. `repo` must be checked out at `head` — the release script hands this a
    detached worktree of the exact commit it packs — and anything else is stale, as in
    `evaluate`.
    """
    t = Touched(components={p: [p] for p in COMPONENTS}, considered=list(COMPONENTS))
    return _verdict(_scan_touched(repo, t, head, timeout, "release commit"), t, head)


def _scan_rows(verdict: Verdict, head: str) -> list[dict]:
    """`record_scan` arguments for what this run actually measured. Never-run and stale
    results record nothing — absence is the honest row for a scan that did not happen."""
    return [{"scanner_id": r.tool, "path": r.prefix, "scanned_sha": head,
             "category": "security", "findings": r.findings,
             "note": f"Security scans job ({r.state})"}
            for r in verdict.results if r.tool and r.state in (CLEAN, FAILED)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="", help="pull_request.base.sha")
    ap.add_argument("--head", required=True, help="pull_request.head.sha")
    ap.add_argument("--branch", default="")
    ap.add_argument("--text", default="", help="PR title/body/branch to scan for item keys")
    ap.add_argument("--run-url", default="")
    # Every re-run of a run shares its run_id; the attempt is what keeps a manual re-run's
    # receipt from being dropped as identical to an earlier one (GRPH-1113).
    ap.add_argument("--run-attempt", default=os.environ.get("GITHUB_RUN_ATTEMPT", ""),
                    help="github.run_attempt; defaults to $GITHUB_RUN_ATTEMPT")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--timeout", type=float, default=TOOL_TIMEOUT)
    args = ap.parse_args(argv)

    verdict = evaluate(pathlib.Path(args.repo).resolve(), base=args.base, head=args.head,
                       timeout=args.timeout)
    print(verdict.detail)
    summary = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"## Security scans: `{PREDICATE}` "
                     f"{'passed' if verdict.passed else 'FAILED'}\n\n")
            fh.write(f"{verdict.detail}\n\n")
            for r in verdict.results:
                fh.write(f"- {r.line()}\n")

    url = os.environ.get("GRAPHBAN_URL", "").strip()
    key = os.environ.get("GRAPHBAN_GATE_KEY", "").strip()
    keys = attest_ci.item_keys(args.text, args.branch)
    if not url or not key:
        print("::warning title=scans_clean not attested::GRAPHBAN_URL or GRAPHBAN_GATE_KEY is "
              "not set, so this scan attested nothing.")
    elif not keys:
        print("no Graphban item key in the branch or PR text — nothing to attest")
    else:
        # Rows first, for the overlay. A failed post is logged and changes nothing: the
        # attestation stands on this process's output, not on a row.
        for row in _scan_rows(verdict, args.head):
            try:
                attest_ci.call_tool(url, key, "record_scan", row)
            except (urllib.error.URLError, RuntimeError, ValueError) as e:
                print(f"::warning title=scan row not recorded::{row['scanner_id']} "
                      f"{row['path']}: {e}")
        receipt = attest_ci.receipt(
            commit=args.head, branch=args.branch, run_url=args.run_url,
            run_attempt=args.run_attempt, matched=attest_ci.matched_by(args.text),
            predicate={"name": PREDICATE, "passed": verdict.passed,
                       "detail": verdict.detail})
        errors = []
        for item_key in keys:
            try:
                # `head_commit` on every run, pass or fail: an older pass must stop covering
                # a head that has since failed.
                attest_ci.call_tool(url, key, "update_item",
                                    {"id": item_key, "head_commit": args.head,
                                     "evidence": [receipt]})
                print(f"{item_key}: {PREDICATE} "
                      f"{'passed' if verdict.passed else 'failed'} at {args.head[:12]}")
            except attest_ci.ItemMissing as e:
                print(f"::warning title=scans_clean not attested::{e}")
            except (urllib.error.URLError, RuntimeError, ValueError) as e:
                errors.append(f"{item_key}: {e}")
        for e in errors:
            print(f"::error title=scans_clean attestation failed::{e}")
        if errors:
            return 1

    if not verdict.passed:
        print(f"::error title=scans_clean failed::{verdict.detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
