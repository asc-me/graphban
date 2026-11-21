"""`gban scan` — run the installed deterministic scanners at a sha and record them (GRPH-1827).

Nothing ran the scanners the server maps (vulture/knip -> deadcode, radon -> refactor, pip-audit/
bandit/pnpm audit -> security), so their Stack cells sat never_scanned or stale. This runs them in
a temporary detached worktree at the target sha and posts one `record_scan` per component x scanner.

A scanner that is not installed is `tool_missing`; one that crashes, times out or prints output we
cannot read is `incomplete`. Only a scanner that finished is `complete`, and only a finished scanner
with no findings is clean. Every row is independent: a later failure never rolls back an earlier row.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from gban import codemap

DEFAULT_TIMEOUT = 600
MAX_FINDINGS = 200

# Runner: (argv, cwd, timeout) -> (returncode, stdout, stderr). Raises OSError / TimeoutExpired.
Runner = Callable[[list[str], Path, int], "tuple[int, str, str]"]


class ScanError(codemap.MapError):
    """The step itself failed (fetch, worktree, server)."""


def run_process(argv: list[str], cwd: Path, timeout: int) -> tuple[int, str, str]:
    proc = subprocess.run(argv, cwd=str(cwd), capture_output=True, timeout=timeout, check=False)
    return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")


@dataclass
class Outcome:
    result: str  # complete | incomplete
    findings: list[dict] = field(default_factory=list)
    note: str = ""


class Unreadable(Exception):
    """Output that cannot be told apart from a crash."""


def _json(out: str):
    try:
        return json.loads(out)
    except ValueError as exc:
        raise Unreadable(f"output is not JSON: {exc}") from exc


def _cap(findings: list[dict]) -> list[dict]:
    return findings[:MAX_FINDINGS]


def parse_pip_audit(out: str, rc: int) -> list[dict]:
    data = _json(out)
    deps = data.get("dependencies", []) if isinstance(data, dict) else data
    found = []
    for dep in deps or []:
        for vuln in dep.get("vulns") or []:
            fixes = vuln.get("fix_versions") or []
            row = {"summary": f"{dep.get('name')} {dep.get('version')}: {vuln.get('id')}",
                   "advisory_id": vuln.get("id"), "dependency": "direct", "severity": "high"}
            if fixes:
                row["fixed_version"] = fixes[0]
            found.append(row)
    return _cap(found)


def parse_bandit(out: str, rc: int) -> list[dict]:
    data = _json(out)
    found = []
    for r in data.get("results", []):
        found.append({"summary": f"{r.get('test_id')}: {r.get('issue_text')}",
                      "path": f"{r.get('filename')}:{r.get('line_number')}",
                      "severity": str(r.get("issue_severity", "low")).lower()})
    return _cap(found)


def parse_pnpm_audit(out: str, rc: int) -> list[dict]:
    data = _json(out)
    advisories = data.get("advisories")
    if not isinstance(advisories, dict):
        raise Unreadable("no advisories object")
    found = []
    for key, adv in advisories.items():
        row = {"summary": f"{adv.get('module_name')}: {adv.get('title')}",
               "advisory_id": str(adv.get("github_advisory_id") or adv.get("id") or key),
               "severity": str(adv.get("severity", "low")).lower(),
               "dependency": "direct"}
        patched = adv.get("patched_versions")
        if patched and patched != "<0.0.0":
            row["fixed_version"] = patched
        found.append(row)
    return _cap(found)


_VULTURE = re.compile(r"^(?P<file>.+?):(?P<line>\d+): (?P<what>.+?) \((?P<conf>\d+)% confidence\)\s*$")


def parse_vulture(out: str, rc: int) -> list[dict]:
    found = []
    for line in out.splitlines():
        m = _VULTURE.match(line.strip())
        if m:
            found.append({"summary": m["what"], "path": f"{m['file']}:{m['line']}", "severity": "low"})
        elif line.strip():
            raise Unreadable(f"unexpected vulture line: {line.strip()[:120]}")
    return _cap(found)


def parse_knip(out: str, rc: int) -> list[dict]:
    data = _json(out)
    issues = data.get("issues") if isinstance(data, dict) else None
    if issues is None:
        raise Unreadable("no issues list")
    exports = types = 0
    found = []
    for issue in issues:
        path = issue.get("file", "")
        exports += len(issue.get("exports") or [])
        types += len(issue.get("types") or [])
        if issue.get("files"):
            found.append({"summary": "unused file", "path": path, "severity": "low"})
        for dep in issue.get("unlisted") or issue.get("dependencies") or []:
            name = dep.get("name") if isinstance(dep, dict) else str(dep)
            found.append({"summary": f"undeclared or unused dependency {name}", "path": path,
                          "severity": "low"})
    if exports:
        found.append({"summary": f"{exports} unused exports", "kind": "unused_export",
                      "count": exports, "severity": "info"})
    if types:
        found.append({"summary": f"{types} unused types", "kind": "unused_type",
                      "count": types, "severity": "info"})
    return _cap(found)


def parse_radon(out: str, rc: int) -> list[dict]:
    data = _json(out)
    found = []
    for path, blocks in data.items():
        if not isinstance(blocks, list):
            raise Unreadable(f"radon error for {path}")
        for b in blocks:
            found.append({"summary": f"{b.get('name')}: complexity {b.get('complexity')} (rank {b.get('rank')})",
                          "path": f"{path}:{b.get('lineno')}", "severity": "med"})
    return _cap(found)


@dataclass(frozen=True)
class Scanner:
    id: str
    category: str
    kind: str  # python | pnpm | js
    argv: tuple[str, ...]
    binary: str
    ok_codes: tuple[int, ...]
    parse: Callable[[str, int], list[dict]]


SCANNERS = (
    Scanner("pip-audit", "security", "python", ("pip-audit", "-f", "json", "--progress-spinner", "off"),
            "pip-audit", (0, 1), parse_pip_audit),
    Scanner("bandit", "security", "python", ("bandit", "-r", ".", "-f", "json", "-q"),
            "bandit", (0, 1), parse_bandit),
    # Same id as scripts/scan_ci.py records for pnpm audit, so CI and nightly rows share a history.
    Scanner("npm-audit", "security", "pnpm", ("pnpm", "audit", "--json"), "pnpm", (0, 1), parse_pnpm_audit),
    Scanner("vulture", "deadcode", "python", ("vulture", ".", "--min-confidence", "80"),
            "vulture", (0, 3), parse_vulture),
    Scanner("knip", "deadcode", "js", ("knip", "--reporter", "json"), "knip", (0, 1), parse_knip),
    Scanner("radon", "refactor", "python", ("radon", "cc", "-j", "-n", "C", "."),
            "radon", (0, ), parse_radon),
)


def component_kinds(root: Path, path: str) -> set[str]:
    """Which scanner kinds apply to a component directory."""
    comp = root / path
    kinds: set[str] = set()
    if not comp.is_dir():
        return kinds
    if (comp / "pyproject.toml").exists() or (comp / "setup.py").exists() \
            or any(comp.glob("requirements*.txt")) or any(comp.rglob("*.py")):
        kinds.add("python")
    if (comp / "package.json").exists():
        kinds.add("js")
        if (comp / "pnpm-lock.yaml").exists() or (root / "pnpm-lock.yaml").exists():
            kinds.add("pnpm")
    return kinds


def resolve_argv(scanner: Scanner, comp: Path) -> list[str] | None:
    """The command to run, or None when the scanner is not installed."""
    if scanner.id == "knip":
        local = comp / "node_modules" / ".bin" / "knip"
        if local.exists():
            return [str(local), *scanner.argv[1:]]
        return None  # needs node_modules; absent -> tool_missing
    if shutil.which(scanner.binary) is None:
        return None
    argv = list(scanner.argv)
    if scanner.id == "pip-audit":
        # A bare `pip-audit` audits the environment it is installed in (gban's own venv), not the
        # component: a clean row from it would describe the wrong thing. Mirror scripts/scan_ci.py.
        if uses_uv_lock(comp):
            if shutil.which("uv") is None:
                return None  # cannot export the lockfile -> tool_missing, never a bare audit
            argv += ["--disable-pip", "--no-deps", "-r", UV_EXPORT_PLACEHOLDER]
        else:
            argv.append(".")  # local-project mode reads the component's manifest
    return argv


UV_EXPORT_PLACEHOLDER = "<uv.lock export>"


def uses_uv_lock(comp: Path) -> bool:
    return (comp / "uv.lock").is_file()


def uv_export_argv(out: Path) -> list[str]:
    return ["uv", "export", "--frozen", "--no-hashes", "--no-emit-project",
            "--format", "requirements-txt", "-o", str(out)]


def plan(root: Path, components: list[dict]) -> list[dict]:
    """One row per component x applicable scanner: {path, component, scanner, installed}."""
    rows = []
    for comp in components:
        path = comp["path"]
        kinds = component_kinds(root, path)
        for sc in SCANNERS:
            if sc.kind in kinds:
                rows.append({"component": comp.get("component") or path, "path": path,
                             "scanner": sc, "installed": resolve_argv(sc, root / path) is not None})
    return rows


def execute(sc: Scanner, comp: Path, run: Runner, timeout: int) -> tuple[str, Outcome | None]:
    """('tool_missing'|'complete'|'incomplete', outcome). Never clean unless it finished."""
    argv = resolve_argv(sc, comp)
    if argv is None:
        return "tool_missing", None
    if UV_EXPORT_PLACEHOLDER in argv:
        with tempfile.TemporaryDirectory(prefix="gban-scan-reqs-") as tmp:
            reqs = Path(tmp) / "requirements.txt"
            try:
                rc, out, err = run(uv_export_argv(reqs), comp, timeout)
            except subprocess.TimeoutExpired:
                return "incomplete", Outcome("incomplete", note=f"uv export timed out after {timeout}s")
            except FileNotFoundError:
                return "tool_missing", None
            except OSError as exc:
                return "incomplete", Outcome("incomplete", note=f"uv export could not run: {exc}")
            if rc != 0 or not reqs.is_file():
                return "incomplete", Outcome(
                    "incomplete", note=f"uv export failed ({rc}): {(err or out).strip()[:200]}")
            argv = [str(reqs) if a == UV_EXPORT_PLACEHOLDER else a for a in argv]
            return _finish(sc, argv, comp, run, timeout)
    return _finish(sc, argv, comp, run, timeout)


def _finish(sc: Scanner, argv: list[str], comp: Path, run: Runner, timeout: int) -> tuple[str, Outcome | None]:
    try:
        rc, out, err = run(argv, comp, timeout)
    except subprocess.TimeoutExpired:
        return "incomplete", Outcome("incomplete", note=f"{sc.id} timed out after {timeout}s")
    except FileNotFoundError:
        return "tool_missing", None
    except OSError as exc:
        return "incomplete", Outcome("incomplete", note=f"{sc.id} could not run: {exc}")
    if rc not in sc.ok_codes:
        return "incomplete", Outcome("incomplete", note=f"{sc.id} exited {rc}: {(err or out).strip()[:200]}")
    try:
        findings = sc.parse(out, rc)
    except (Unreadable, AttributeError, TypeError, KeyError) as exc:
        return "incomplete", Outcome("incomplete", note=f"{sc.id} output unreadable: {exc}")
    return "complete", Outcome("complete", findings)


class Worktree:
    """A detached worktree at `sha`, removed on exit (also on failure)."""

    def __init__(self, repo: Path, sha: str):
        self.repo, self.sha, self.path = repo, sha, None

    def __enter__(self) -> Path:
        base = tempfile.mkdtemp(prefix="gban-scan-")
        self.path = Path(base) / "wt"
        try:
            codemap._git(self.repo, "worktree", "add", "--detach", str(self.path), self.sha)
        except codemap.MapError as exc:
            shutil.rmtree(base, ignore_errors=True)
            self.path = None
            raise ScanError(f"worktree: {exc}") from exc
        return self.path

    def __exit__(self, *exc) -> None:
        if self.path is None:
            return
        try:
            codemap._git(self.repo, "worktree", "remove", "--force", str(self.path))
        except codemap.MapError:
            pass
        shutil.rmtree(self.path.parent, ignore_errors=True)
        try:
            codemap._git(self.repo, "worktree", "prune")
        except codemap.MapError:
            pass


def components_from_server(call, sha: str) -> list[dict]:
    """Distinct Stack component paths, from the cells the server says it keys on."""
    due = call("scans_due", {"head_sha": sha})
    seen: dict[str, dict] = {}
    for row in due.get("due", []):
        if row.get("path"):
            seen.setdefault(row["path"], {"component": row.get("component"), "path": row["path"]})
    return list(seen.values())


def record(call, sha: str, row: dict, status: str, outcome: Outcome | None) -> None:
    sc: Scanner = row["scanner"]
    args = {"scanner_id": sc.id, "path": row["path"], "scanned_sha": sha, "category": sc.category,
            "result": status}
    if status == "complete":
        args["findings"] = outcome.findings
    elif outcome is not None:
        args["findings"] = outcome.findings
        args["note"] = outcome.note
    call("record_scan", args)


def run_scan(repo: Path, sha: str | None, call, *, dry_run: bool = False, run: Runner = run_process,
             timeout: int = DEFAULT_TIMEOUT, out: Callable[[str], None] = print) -> dict:
    """Plan and run every scanner at `sha`. Returns counts; raises ScanError/MapError on step failure."""
    if sha:
        target = codemap.head(repo, sha)
    else:
        _, target = codemap.default_target(repo)
    try:
        components = components_from_server(call, target)
    except codemap.MapError as exc:
        raise ScanError(f"server: {exc}") from exc
    counts = {"recorded": 0, "tool_missing": 0, "incomplete": 0, "sha": target}
    with Worktree(repo, target) as root:
        rows = plan(root, components)
        if dry_run:
            for r in rows:
                out(f"{r['path']}\t{r['scanner'].id}\t{'installed' if r['installed'] else 'not installed'}")
            return {**counts, "dry_run": True}
        for r in rows:
            status, outcome = execute(r["scanner"], root / r["path"], run, timeout)
            try:
                record(call, target, r, status, outcome)
            except codemap.MapError as exc:
                raise ScanError(f"server: {exc}") from exc
            counts["recorded"] += 1
            if status in ("tool_missing", "incomplete"):
                counts[status] += 1
    return counts
