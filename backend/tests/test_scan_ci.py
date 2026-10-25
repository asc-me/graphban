"""The CI `Security scans` job and its `scans_clean` attestation (GRPH-P57 S3, GRPH-1110).

Every test runs `scripts/scan_ci.py` against a real git repository in a temp dir with FAKE
scanner binaries on PATH — no network, no real pip-audit. The fakes print what the real tools
print (JSON on stdout, nonzero exit on findings) so the script's parsing is what is tested,
not a stub of it. Numbers in the test names are the PRD's acceptance criteria.
"""
from __future__ import annotations

import os
import pathlib
import stat
import subprocess
import sys
import textwrap

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import attest_ci  # noqa: E402
import scan_ci  # noqa: E402

WORKFLOW = REPO / ".github/workflows/ci.yml"

# ---- fakes ------------------------------------------------------------------------------

# Each fake logs `<name> <cwd> <argv...>` to $FAKE_LOG and obeys $FAKE_<TOOL>:
#   clean | findings | crash | garbage | empty | exit1-clean | hang | neterror
_FAKES = {
    "uv": r'''
        import sys, pathlib
        out = pathlib.Path(sys.argv[sys.argv.index("-o") + 1])
        out.write_text(pathlib.Path("uv.lock").read_text())
    ''',
    "pip-audit": r'''
        import json, os, pathlib, sys, time
        mode = os.environ.get("FAKE_PIP_AUDIT", "lock")
        if mode == "neterror":
            sys.stderr.write("requests.exceptions.ReadTimeout: pypi.org timed out\n"); sys.exit(1)
        if mode == "hang":
            time.sleep(30)
        if mode == "garbage":
            print("<html>proxy error</html>"); sys.exit(1)
        src = sys.argv[sys.argv.index("-r") + 1] if "-r" in sys.argv else "pyproject.toml"
        text = pathlib.Path(src).read_text()
        deps = []
        for line in text.splitlines():
            if "==" in line:
                name, ver = line.strip().split("==")
                vulns = ([{"id": "PYSEC-1", "fix_versions": ["9.9"], "description": "bad"}]
                         if "vulnerable" in name else [])
                deps.append({"name": name, "version": ver, "vulns": vulns})
        print(json.dumps({"dependencies": deps, "fixes": []}))
        sys.exit(1 if any(d["vulns"] for d in deps) else 0)
    ''',
    "bandit": r'''
        import json, os, sys
        mode = os.environ.get("FAKE_BANDIT", "clean")
        if mode == "crash":
            sys.stderr.write("Traceback: KeyError 'x'\n"); sys.exit(2)
        if mode == "empty":
            sys.exit(0)
        if mode == "garbage":
            print("not json"); sys.exit(0)
        results = []
        if mode == "findings":
            results = [{"test_id": "B602", "issue_text": "shell=True", "issue_severity": "HIGH",
                        "filename": "./app/x.py", "line_number": 3}]
        print(json.dumps({"results": results, "errors": [], "metrics": {}}))
        sys.exit(1 if results or mode == "exit1-clean" else 0)
    ''',
    "pnpm": r'''
        import json, os, sys
        mode = os.environ.get("FAKE_NPM_AUDIT", "clean")
        adv = {}
        if mode == "findings":
            adv = {"1": {"module_name": "lodash", "severity": "high", "title": "Prototype pollution"}}
        print(json.dumps({"actions": [], "advisories": adv, "metadata": {}}))
        sys.exit(1 if adv else 0)
    ''',
}


@pytest.fixture()
def fakes(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    for name, body in _FAKES.items():
        script = bindir / name
        script.write_text(
            f"#!{sys.executable}\n"
            "import os, sys\n"
            "with open(os.environ['FAKE_LOG'], 'a') as fh:\n"
            "    fh.write(' '.join([os.path.basename(sys.argv[0]), os.getcwd()] + sys.argv[1:]) + '\\n')\n"
            + textwrap.dedent(body))
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(log))
    for k in ("GRAPHBAN_URL", "GRAPHBAN_GATE_KEY", "GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(k, raising=False)
    return log


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


def _write(repo, files):
    for path, text in files.items():
        p = repo / path
        if text is None:
            p.unlink()
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def _commit(repo, files, msg="change"):
    _write(repo, files)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


BASE_TREE = {
    "backend/pyproject.toml": "[project]\nname='b'\n",
    "backend/uv.lock": "fastapi==0.1\nvulnerable-dep==1.0\n",
    "backend/app/x.py": "x = 1\n",
    "backend/tests/test_x.py": "def test(): pass\n",
    "fleet/pyproject.toml": "[project]\nname='f'\n",
    "fleet/uv.lock": "httpx==1.0\n",
    "fleet/src/f.py": "f = 1\n",
    "cli/pyproject.toml": "[project]\nname='c'\ndependencies=['click==8.0']\nclick==8.0\n",
    "cli/c.py": "c = 1\n",
    "web/package.json": "{}\n",
    "web/pnpm-lock.yaml": "lockfileVersion: 9\n",
    "web/src/a.ts": "export {}\n",
    "scripts/foo.py": "pass\n",
    "docs/guide.md": "# g\n",
    "README.md": "# r\n",
    ".gitignore": "*.pyc\n",
}


class _Repo(type(pathlib.Path())):
    """The repository path, carrying the PR's base commit as `.base`."""
    base: str


@pytest.fixture()
def repo(tmp_path):
    r = _Repo(tmp_path / "repo")
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "t")
    tree = dict(BASE_TREE)
    # The base lockfile is clean; the "vulnerable pin" tests put it there on purpose.
    tree["backend/uv.lock"] = "fastapi==0.1\n"
    base = _commit(r, tree, "base")
    _git(r, "checkout", "-q", "-b", "feature")
    r.base = base
    return r


def _pr(repo, files, msg="pr"):
    """Commit `files` on the feature branch and return its head."""
    return _commit(repo, files, msg)


def _eval(repo, head, **kw):
    return scan_ci.evaluate(repo, base=repo.base, head=head, timeout=kw.pop("timeout", 20), **kw)


def _states(verdict):
    return {(r.tool, r.prefix): r.state for r in verdict.results}


# ---- constants agree with the server ----------------------------------------------------

def test_components_and_tools_match_the_server_constants():
    """The script repeats COMPONENT_PATHS (it runs on bare python3); drift would scan a set
    of components the Stack does not know, or skip one it does."""
    from app.services import scans as scans_svc

    assert set(scan_ci.COMPONENTS) == set(scans_svc.COMPONENT_PATHS.values())
    assert set(scan_ci.TOOLS) == set(scan_ci.COMPONENTS)
    six = {"pip-audit", "bandit", "npm-audit", "osv-scanner", "semgrep", "trivy"}
    for prefix, tools in scan_ci.TOOLS.items():
        assert tools, f"{prefix} has no security tool — it could never pass"
        # Only the six named tools count; the aliases `security`/`osv`/`safety` do not.
        assert set(tools) <= six, tools
        assert set(tools) <= scans_svc._SECURITY_SCANNERS
        assert set(tools) <= set(scan_ci.RUNNERS)


# ---- AC 3 / 4: the three failures, and exit codes are not verdicts -----------------------

def test_clean_tools_at_the_head_pass(repo, fakes):
    head = _pr(repo, {"backend/app/x.py": "x = 2\n"})
    v = _eval(repo, head)
    assert v.passed, v.detail
    assert _states(v) == {("pip-audit", "backend/"): "clean", ("bandit", "backend/"): "clean"}
    assert "touched: backend/" in v.detail


def test_findings_are_a_failed_check_naming_tool_and_prefix(repo, fakes, monkeypatch):
    monkeypatch.setenv("FAKE_BANDIT", "findings")
    head = _pr(repo, {"backend/app/x.py": "x = 2\n"})
    v = _eval(repo, head)
    assert not v.passed
    assert _states(v)[("bandit", "backend/")] == scan_ci.FAILED
    assert "failed check bandit backend/: 1 finding(s): B602" in v.detail


def test_no_output_is_never_run_not_clean(repo, fakes, monkeypatch):
    monkeypatch.setenv("FAKE_BANDIT", "empty")
    head = _pr(repo, {"backend/app/x.py": "x = 2\n"})
    v = _eval(repo, head)
    assert not v.passed
    assert "never-run bandit backend/: no output from this run" in v.detail


def test_a_missing_tool_is_never_run(repo, fakes, monkeypatch):
    monkeypatch.setitem(scan_ci.RUNNERS, "bandit", (
        lambda root, t: scan_ci._exec(["bandit-not-installed"], root, t), scan_ci._parse_bandit))
    head = _pr(repo, {"backend/app/x.py": "x = 2\n"})
    v = _eval(repo, head)
    assert _states(v)[("bandit", "backend/")] == scan_ci.NEVER_RUN
    assert "never-run bandit backend/: tool error: bandit exit not found" in v.detail


def test_exit_one_with_no_findings_is_clean(repo, fakes, monkeypatch):
    """AC4: parseable output with no findings is clean whatever the exit code."""
    monkeypatch.setenv("FAKE_BANDIT", "exit1-clean")
    head = _pr(repo, {"backend/app/x.py": "x = 2\n"})
    assert _eval(repo, head).passed


@pytest.mark.parametrize("mode,expect", [
    ("crash", "tool error: bandit exit 2: Traceback"),
    ("garbage", "tool error: bandit exit 0: unparseable output"),
])
def test_crash_and_unparseable_output_are_tool_errors(repo, fakes, monkeypatch, mode, expect):
    monkeypatch.setenv("FAKE_BANDIT", mode)
    head = _pr(repo, {"backend/app/x.py": "x = 2\n"})
    v = _eval(repo, head)
    assert _states(v)[("bandit", "backend/")] == scan_ci.NEVER_RUN
    assert f"never-run bandit backend/: {expect}" in v.detail


def test_a_pip_audit_network_timeout_is_never_run_not_a_failed_check(repo, fakes, monkeypatch):
    """AC4 sabotage: "nonzero means findings" would read this as a failed check."""
    monkeypatch.setenv("FAKE_PIP_AUDIT", "neterror")
    head = _pr(repo, {"backend/app/x.py": "x = 2\n"})
    v = _eval(repo, head)
    assert _states(v)[("pip-audit", "backend/")] == scan_ci.NEVER_RUN
    assert "tool error: pip-audit exit 1: requests.exceptions.ReadTimeout" in v.detail
    assert "failed check pip-audit" not in v.detail


def test_a_hung_tool_times_out_as_never_run(repo, fakes, monkeypatch):
    monkeypatch.setenv("FAKE_PIP_AUDIT", "hang")
    head = _pr(repo, {"backend/app/x.py": "x = 2\n"})
    v = scan_ci.evaluate(repo, base=repo.base, head=head, timeout=1)
    assert _states(v)[("pip-audit", "backend/")] == scan_ci.NEVER_RUN
    assert "tool error: pip-audit exit timeout" in v.detail


def test_a_checkout_that_is_not_the_head_is_stale(repo, fakes):
    """AC3: the PR's merge commit (GitHub's default checkout) is not the head."""
    head = _pr(repo, {"backend/app/x.py": "x = 2\n"})
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge", "feature")  # the "merge commit"
    v = _eval(repo, head)
    assert not v.passed
    assert set(_states(v).values()) == {scan_ci.STALE}
    assert f"stale bandit backend/: checked out {_git(repo, 'rev-parse', 'HEAD')[:12]}" \
        in v.detail
    assert not pathlib.Path(os.environ["FAKE_LOG"]).exists(), "a stale run scanned anyway"


def test_the_three_failure_details_are_distinct_strings(repo, fakes, monkeypatch):
    monkeypatch.setenv("FAKE_BANDIT", "findings")
    monkeypatch.setenv("FAKE_PIP_AUDIT", "neterror")
    head = _pr(repo, {"backend/app/x.py": "x = 2\n"})
    lines = {r.state: r.line() for r in _eval(repo, head).results}
    _git(repo, "checkout", "-q", "main")
    lines.update({r.state: r.line() for r in _eval(repo, head).results})
    assert set(lines) == {scan_ci.NEVER_RUN, scan_ci.FAILED, scan_ci.STALE}
    assert len(set(lines.values())) == 3
    assert all("backend/" in v for v in lines.values())


# ---- AC 6 / 7: whole roots, every mapped tool --------------------------------------------

def test_one_tool_failing_of_two_fails_the_component(repo, fakes, monkeypatch):
    monkeypatch.setenv("FAKE_BANDIT", "findings")
    head = _pr(repo, {"backend/app/x.py": "x = 2\n"})
    v = _eval(repo, head)
    assert _states(v)[("pip-audit", "backend/")] == scan_ci.CLEAN
    assert not v.passed


def test_tools_run_on_the_component_root_and_see_the_untouched_lockfile(repo, fakes):
    """AC7. Sabotage: pass the changed files to the tool, and the lockfile goes unseen."""
    _git(repo, "checkout", "-q", "main")
    repo.base = _commit(repo, {"backend/uv.lock": "fastapi==0.1\nvulnerable-dep==1.0\n"})
    _git(repo, "checkout", "-q", "-B", "feature")
    head = _pr(repo, {"backend/app/x.py": "x = 2\n"})  # does not touch the lockfile
    v = _eval(repo, head)
    assert _states(v)[("pip-audit", "backend/")] == scan_ci.FAILED, v.detail
    assert "vulnerable-dep 1.0: PYSEC-1" in v.detail
    calls = pathlib.Path(os.environ["FAKE_LOG"]).read_text().splitlines()
    bandit = next(c for c in calls if c.startswith("bandit "))
    assert bandit.split()[1] == str(repo / "backend")
    assert "app/x.py" not in bandit, "bandit was given the changed files, not the root"


def test_npm_audit_covers_web(repo, fakes, monkeypatch):
    monkeypatch.setenv("FAKE_NPM_AUDIT", "findings")
    head = _pr(repo, {"web/src/a.ts": "export const a = 1\n"})
    v = _eval(repo, head)
    assert _states(v) == {("npm-audit", "web/"): scan_ci.FAILED}
    assert "lodash: Prototype pollution" in v.detail


def test_a_component_without_a_lockfile_audits_its_manifest(repo, fakes):
    head = _pr(repo, {"cli/c.py": "c = 2\n"})
    v = _eval(repo, head)
    assert v.passed, v.detail
    assert _states(v) == {("pip-audit", "cli/"): "clean", ("bandit", "cli/"): "clean"}


# ---- AC 8 / 9 / 10: the touched set ------------------------------------------------------

def test_a_deletion_only_pr_still_scans(repo, fakes):
    """AC8 sabotage: count only added/modified files, and this PR skips the scan."""
    head = _pr(repo, {"backend/app/x.py": None})
    v = _eval(repo, head)
    assert ("pip-audit", "backend/") in _states(v)


def test_a_rename_touches_both_sides(repo, fakes):
    head = _pr(repo, {"fleet/src/f.py": None, "cli/f.py": "f = 1\n"})
    t = scan_ci.touched(scan_ci.changed_files(repo, repo.base, head))
    assert set(t.components) == {"fleet/", "cli/"}


def test_tests_only_touches_backend(repo, fakes):
    head = _pr(repo, {"backend/tests/test_x.py": "def test(): assert 1\n"})
    assert set(_eval(repo, head).touched.components) == {"backend/"}


@pytest.mark.parametrize("path", [".github/workflows/ci.yml", "scripts/foo.py",
                                  "docs/guide.md", "AGENTS.md"])
def test_paths_filter_mappings_touch_backend_and_say_why(repo, fakes, path):
    head = _pr(repo, {path: "changed\n"})
    v = _eval(repo, head)
    assert set(v.touched.components) == {"backend/"}
    assert f"backend/ (via {path})" in v.detail


def test_not_code_only_passes_with_touched_none(repo, fakes):
    head = _pr(repo, {"README.md": "# changed\n", ".gitignore": "*.log\n"})
    v = _eval(repo, head)
    assert v.passed, v.detail
    assert "touched: none (considered: " in v.detail
    assert not v.results


def test_an_unmapped_file_fails_as_never_run_naming_it(repo, fakes):
    """AC9 sabotage: let unmapped files touch nothing, and a new rust/ passes unscanned."""
    head = _pr(repo, {"rust/src/main.rs": "fn main() {}\n", "README.md": "x\n"})
    v = _eval(repo, head)
    assert not v.passed
    assert "never-run: no component covers rust/src/main.rs" in v.detail


def test_a_diff_that_cannot_be_computed_is_its_own_never_run(repo, fakes):
    v = scan_ci.evaluate(repo, base="0" * 40, head=_git(repo, "rev-parse", "HEAD"))
    assert not v.passed
    assert "never-run: diff not computed: git merge-base" in v.detail
    assert "(shallow clone? needs fetch-depth: 0)" in v.detail
    assert "no output from this run" not in v.detail


def test_a_rebase_recomputes_the_touched_set(repo, fakes):
    """AC10 sabotage: reuse the first run's set, and the rebased-in component goes unscanned."""
    first = _pr(repo, {"backend/app/x.py": "x = 2\n"})
    assert set(_eval(repo, first).touched.components) == {"backend/"}
    _git(repo, "reset", "-q", "--hard", repo.base)
    second = _pr(repo, {"web/src/a.ts": "export const b = 1\n"})
    v = _eval(repo, second)
    assert set(v.touched.components) == {"web/"}
    assert ("npm-audit", "web/") in _states(v)


# ---- AC 2 / 5: attests only from this process, posts head_commit, rows are side effects --

@pytest.fixture()
def ledger(monkeypatch):
    calls: list[tuple[str, dict]] = []
    failing: set[str] = set()

    def call_tool(url, key, name, arguments, *, timeout=15.0):
        calls.append((name, arguments))
        if name in failing:
            raise RuntimeError(f"{name} refused")

    monkeypatch.setenv("GRAPHBAN_URL", "https://example.invalid")
    monkeypatch.setenv("GRAPHBAN_GATE_KEY", "gb_sk_x")
    monkeypatch.setattr(attest_ci, "call_tool", call_tool)
    return calls, failing


def _main(repo, head, *extra):
    return scan_ci.main(["--repo", str(repo), "--base", repo.base, "--head", head,
                         "--branch", "gb/x", "--text", "Attests: GRPH-1110", *extra])


def _receipts(calls):
    return [a for n, a in calls if n == "update_item"]


def test_a_passing_run_attests_scans_clean_alone_with_head_commit(repo, fakes, ledger):
    calls, _ = ledger
    head = _pr(repo, {"backend/app/x.py": "x = 2\n"})
    assert _main(repo, head) == 0
    (upd,) = _receipts(calls)
    assert upd["head_commit"] == head
    (receipt,) = upd["evidence"]
    assert receipt["commit"] == head
    assert [p["name"] for p in receipt["predicates"]] == ["scans_clean"]
    assert receipt["predicates"][0]["passed"] is True


def test_a_failing_run_still_posts_head_commit_and_a_failing_predicate(repo, fakes, ledger,
                                                                         monkeypatch):
    calls, _ = ledger
    monkeypatch.setenv("FAKE_BANDIT", "findings")
    head = _pr(repo, {"backend/app/x.py": "x = 2\n"})
    assert _main(repo, head) == 1
    (upd,) = _receipts(calls)
    assert upd["head_commit"] == head
    pred = upd["evidence"][0]["predicates"][0]
    assert pred == {"name": "scans_clean", "passed": False, "detail": pred["detail"]}
    assert "failed check bandit backend/" in pred["detail"]


def test_the_script_never_reads_the_ledger(repo, fakes, ledger, monkeypatch):
    """AC2 sabotage: attest whenever the live cell is clean_at_sha. Nothing here asks — so an
    empty pip-audit row some other credential posted cannot make a run with no output pass."""
    calls, _ = ledger
    monkeypatch.setenv("FAKE_PIP_AUDIT", "neterror")
    head = _pr(repo, {"backend/app/x.py": "x = 2\n"})
    _main(repo, head)
    assert {n for n, _ in calls} <= {"record_scan", "update_item"}
    assert _receipts(calls)[0]["evidence"][0]["predicates"][0]["passed"] is False


def test_a_failed_scan_row_post_does_not_change_the_attestation(repo, fakes, ledger, capsys):
    calls, failing = ledger
    head = _pr(repo, {"backend/app/x.py": "x = 2\n"})
    failing.add("record_scan")
    assert _main(repo, head) == 0
    assert "scan row not recorded" in capsys.readouterr().out
    assert _receipts(calls)[0]["evidence"][0]["predicates"][0]["passed"] is True


def test_rows_carry_category_and_the_component_root_and_come_before_the_attestation(
        repo, fakes, ledger, monkeypatch):
    """Explicit category (D5's 422 is never met), recorded at the root (a nested row would not
    cover the component), and recorded first: a run that dies after its rows leaves no
    attestation, which the merge reads as "CI has not attested"."""
    calls, _ = ledger
    monkeypatch.setenv("FAKE_BANDIT", "crash")
    head = _pr(repo, {"backend/app/x.py": "x = 2\n"})
    _main(repo, head)
    names = [n for n, _ in calls]
    assert names == ["record_scan", "update_item"]  # bandit never ran: no row for it
    row = calls[0][1]
    assert row == {**row, "scanner_id": "pip-audit", "path": "backend/",
                   "scanned_sha": head, "category": "security", "findings": []}


def test_open_work_stale_graph_and_failing_tests_are_not_inputs(repo, fakes):
    """AC5: only the security tools run. A failing pytest gates through suite_green."""
    head = _pr(repo, {"backend/app/x.py": "x = 2\n"})
    v = _eval(repo, head)
    assert v.passed
    assert {r.tool for r in v.results} <= {"pip-audit", "bandit", "npm-audit"}


def test_the_job_summary_carries_the_detail(repo, fakes, tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("FAKE_BANDIT", "findings")
    head = _pr(repo, {"backend/app/x.py": "x = 2\n"})
    _main(repo, head)
    text = summary.read_text()
    assert "scans_clean` FAILED" in text
    assert "failed check bandit backend/" in text


def test_a_failing_scans_clean_does_not_void_suite_green(repo, fakes, ledger, monkeypatch):
    """Sabotage: put both predicates on one receipt, and a feature PR with a findings-bearing
    dependency loses `suite_green` — `valid_attestations` admits a receipt only when EVERY
    predicate on it passed."""
    from app.services import items as items_svc

    calls, _ = ledger
    monkeypatch.setenv("FAKE_BANDIT", "findings")
    head = _pr(repo, {"backend/app/x.py": "x = 2\n"})
    _main(repo, head)
    scans = _receipts(calls)[0]["evidence"][0]
    suite = attest_ci.attestation(commit=head, branch="gb/x")
    stored = items_svc.normalize_evidence([suite, scans])
    assert "suite_green" in items_svc.attested_predicates(stored, commit=head)
    assert "scans_clean" not in items_svc.attested_predicates(stored, commit=head)
    assert len(scans["predicates"]) == 1


# ---- AC 11: the job --------------------------------------------------------------------

def _jobs():
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def test_the_security_scans_job_is_not_part_of_the_required_check():
    """Sabotage: make it required, and a feature PR with a findings-bearing dependency is
    blocked at the forge — the gate belongs to the fleet merge into the release ref."""
    jobs = _jobs()
    job = jobs["security-scans"]
    assert job["name"] == "Security scans"
    assert "security-scans" not in jobs["ci"]["needs"]
    assert "needs" not in job or "ci" not in job["needs"]


def test_the_job_scans_the_head_with_full_history_and_cancels_older_runs():
    job = _jobs()["security-scans"]
    checkout = next(s for s in job["steps"] if str(s.get("uses", "")).startswith(
        "actions/checkout"))
    assert checkout["with"]["fetch-depth"] == 0
    assert checkout["with"]["ref"] == "${{ github.event.pull_request.head.sha }}"
    assert job["concurrency"]["cancel-in-progress"] is True
    assert "pull_request.number" in job["concurrency"]["group"]
    step = next(s for s in job["steps"] if "scan_ci.py" in (s.get("run") or ""))
    assert step["env"]["HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"
    assert step["env"]["BASE_SHA"] == "${{ github.event.pull_request.base.sha }}"
    assert "graphban-ledger" in job["runs-on"]
