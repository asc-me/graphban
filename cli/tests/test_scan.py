"""`gban scan` (GRPH-1827): faked tool runs, fake server."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from gban import codemap, config, scan
from gban.cli import main
from gban.client import TIMEOUT, Client, TimedOut
from test_codemap import _git


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/a.py").write_text("x = 1\n")
    (tmp_path / "pkg/pyproject.toml").write_text("[project]\nname='p'\n")
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    return tmp_path


class Server:
    def __init__(self, paths=("pkg/",), refuse=()):
        self.paths = paths
        self.refuse = set(refuse)  # scanner ids whose record the server refuses
        self.records: list[dict] = []
        self.timeouts: list[tuple] = []
        self.due_timeouts = 0

    def __call__(self, tool, args, timeout=None):
        self.timeouts.append((tool, timeout))
        if tool == "scans_due":
            if self.due_timeouts:
                self.due_timeouts -= 1
                raise TimedOut("could not reach: timed out", 120.0)
            return {"due": [{"component": "pkg", "path": p, "category": "security"} for p in self.paths]}
        assert tool == "record_scan"
        # Same rule as backend/app/services/scans.py _EMPTY_INCOMPLETE.
        if args["result"] == "incomplete" and not args.get("findings"):
            raise codemap.MapError("record_scan: result 'incomplete' requires findings; nothing was stored")
        if args["scanner_id"] in self.refuse:
            raise codemap.MapError("record_scan: refused")
        self.records.append(args)
        return {}


def _installed(monkeypatch, names):
    monkeypatch.setattr(scan.shutil, "which", lambda b: f"/bin/{b}" if b in names else None)


def _runner(outputs):
    def run(argv, cwd, timeout):
        val = outputs[argv[0]]
        if isinstance(val, BaseException):
            raise val
        return val
    return run


def _by(server):
    return {r["scanner_id"]: r for r in server.records}


def test_records_findings_at_sha_with_category(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _installed(monkeypatch, {"vulture", "pip-audit", "bandit", "radon"})
    srv = Server()
    run = _runner({
        "vulture": (3, "a.py:1: unused function 'f' (90% confidence)\n", ""),
        "pip-audit": (0, json.dumps({"dependencies": []}), ""),
        "bandit": (0, json.dumps({"results": []}), ""),
        "radon": (0, "{}", ""),
    })
    sha = codemap.head(repo)
    counts = scan.run_scan(repo, sha, srv, run=run)
    rec = _by(srv)
    assert counts["recorded"] == 4 and counts["sha"] == sha
    assert rec["vulture"]["category"] == "deadcode" and rec["vulture"]["scanned_sha"] == sha
    assert rec["vulture"]["result"] == "complete" and len(rec["vulture"]["findings"]) == 1
    assert rec["radon"]["category"] == "refactor"
    assert rec["bandit"]["category"] == "security" and rec["bandit"]["path"] == "pkg/"


def test_zero_findings_complete_is_clean(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _installed(monkeypatch, {"vulture"})
    srv = Server()
    scan.run_scan(repo, codemap.head(repo), srv, run=_runner({"vulture": (0, "", "")}))
    r = _by(srv)["vulture"]
    assert r["result"] == "complete" and r["findings"] == []


def test_missing_tool_is_tool_missing_never_clean(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _installed(monkeypatch, set())
    srv = Server()
    counts = scan.run_scan(repo, codemap.head(repo), srv, run=_runner({}))
    assert counts["tool_missing"] == counts["recorded"] > 0
    assert all(r["result"] == "tool_missing" for r in srv.records)
    assert not any(r["result"] == "complete" for r in srv.records)  # sabotage: clean-as-missing fails here


def test_crash_and_timeout_are_incomplete(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _installed(monkeypatch, {"vulture", "radon", "bandit"})
    srv = Server()
    run = _runner({
        "vulture": (2, "", "boom"),
        "radon": subprocess.TimeoutExpired("radon", 1),
        "bandit": (0, "not json", ""),
    })
    counts = scan.run_scan(repo, codemap.head(repo), srv, run=run)
    rec = _by(srv)
    assert {rec[k]["result"] for k in ("vulture", "radon", "bandit")} == {"incomplete"}
    assert counts["incomplete"] == 3 and counts["failed"] == 0
    for k in ("vulture", "radon", "bandit"):
        assert rec[k]["findings"] and k in rec[k]["findings"][0]["summary"]
        assert rec[k]["note"] in rec[k]["findings"][0]["summary"]


def test_refused_row_is_counted_and_the_run_carries_on(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _installed(monkeypatch, {"vulture", "radon", "bandit"})
    srv = Server(refuse={"bandit"})
    run = _runner({"vulture": (0, "", ""), "radon": (0, "{}", ""), "bandit": (0, '{"results": []}', "")})
    lines = []
    counts = scan.run_scan(repo, codemap.head(repo), srv, run=run, out=lines.append)
    assert counts["failed"] == 1 and counts["recorded"] == 3
    assert {r["scanner_id"] for r in srv.records} == {"vulture", "radon", "pip-audit"}
    assert any("bandit" in ln and "failed" in ln for ln in lines)


def test_later_failure_does_not_unrecord_earlier(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _installed(monkeypatch, {"vulture", "radon"})
    srv = Server()
    run = _runner({"vulture": (0, "", ""), "radon": (9, "", "x")})
    scan.run_scan(repo, codemap.head(repo), srv, run=run)
    assert _by(srv)["vulture"]["result"] == "complete"
    assert _by(srv)["radon"]["result"] == "incomplete"


def test_knip_rollup_and_missing_without_node_modules(tmp_path):
    out = json.dumps({"issues": [{"file": "a.ts", "exports": [{"name": "a"}, {"name": "b"}], "types": [{"name": "T"}]}]})
    found = scan.parse_knip(out, 1)
    kinds = {f.get("kind"): f.get("count") for f in found}
    assert kinds == {"unused_export": 2, "unused_type": 1}
    sc = next(s for s in scan.SCANNERS if s.id == "knip")
    assert scan.resolve_argv(sc, tmp_path) is None


def test_worktree_removed_after_failure(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _installed(monkeypatch, {"vulture"})
    seen = []

    def run(argv, cwd, timeout):
        seen.append(cwd)
        raise RuntimeError("fatal")

    with pytest.raises(RuntimeError):
        scan.run_scan(repo, codemap.head(repo), Server(), run=run)
    assert seen and not seen[0].exists()
    listing = subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True).stdout
    assert len(listing.strip().splitlines()) == 1


def test_dry_run_records_nothing(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    _installed(monkeypatch, {"vulture"})
    srv = Server()

    def run(*a):
        raise AssertionError("dry run must not execute scanners")

    counts = scan.run_scan(repo, codemap.head(repo), srv, dry_run=True, run=run)
    assert srv.records == [] and counts["dry_run"]
    out = capsys.readouterr().out
    assert "vulture\tinstalled" in out and "radon\tnot installed" in out


def test_cli_summary_line(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    _installed(monkeypatch, set())
    srv = Server()
    monkeypatch.setenv(config.URL_ENV, "http://example.invalid")
    monkeypatch.setenv("GBFLEET_API_KEY", "k")
    monkeypatch.setattr(codemap, "mcp_caller", lambda client, project: srv)
    assert main(["--project", "p", "scan", "--sha", codemap.head(repo), "--repo", str(repo)]) == 0
    assert "4 tool_missing, 0 incomplete, 0 failed at " in capsys.readouterr().out
    assert os.environ["GBFLEET_API_KEY"] == "k"


def _pip_audit():
    return next(s for s in scan.SCANNERS if s.id == "pip-audit")


def test_pip_audit_argv_audits_the_component_not_gbans_env(tmp_path, monkeypatch):
    _installed(monkeypatch, {"pip-audit", "uv"})
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "pyproject.toml").write_text("[project]\nname='p'\n")
    argv = scan.resolve_argv(_pip_audit(), plain)
    assert argv[0] == "pip-audit" and argv[-1] == "."  # local-project mode, never bare

    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "pyproject.toml").write_text("[project]\nname='p'\n")
    (locked / "uv.lock").write_text("version = 1\n")
    argv = scan.resolve_argv(_pip_audit(), locked)
    assert argv[-4:-1] == ["--disable-pip", "--no-deps", "-r"] and "." not in argv

    _installed(monkeypatch, {"pip-audit"})  # uv.lock but no uv: cannot audit the lockfile
    assert scan.resolve_argv(_pip_audit(), locked) is None


def test_pip_audit_uv_lock_runs_export_then_audits_it(tmp_path, monkeypatch):
    _installed(monkeypatch, {"pip-audit", "uv"})
    comp = tmp_path / "c"
    comp.mkdir()
    (comp / "uv.lock").write_text("version = 1\n")
    calls = []

    def run(argv, cwd, timeout):
        calls.append(argv)
        if argv[0] == "uv":
            Path(argv[argv.index("-o") + 1]).write_text("requests==2.0\n")
            return 0, "", ""
        reqs = argv[argv.index("-r") + 1]
        assert Path(reqs).read_text() == "requests==2.0\n"
        return 0, json.dumps({"dependencies": []}), ""

    status, out = scan.execute(_pip_audit(), comp, run, 5)
    assert status == "complete" and out.findings == []
    assert calls[0][:3] == ["uv", "export", "--frozen"] and "--no-emit-project" in calls[0]
    assert calls[1][0] == "pip-audit"


def test_pip_audit_failed_export_is_incomplete_not_clean(tmp_path, monkeypatch):
    _installed(monkeypatch, {"pip-audit", "uv"})
    comp = tmp_path / "c"
    comp.mkdir()
    (comp / "uv.lock").write_text("version = 1\n")
    status, out = scan.execute(_pip_audit(), comp, _runner({"uv": (2, "", "lock stale")}), 5)
    assert status == "incomplete" and "uv export failed" in out.note


def test_pnpm_audit_uses_ci_scanner_id():
    assert any(s.id == "npm-audit" and s.category == "security" for s in scan.SCANNERS)


def test_server_calls_use_long_timeouts(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _installed(monkeypatch, {"vulture"})
    srv = Server()
    scan.run_scan(repo, codemap.head(repo), srv, run=_runner({"vulture": (0, "", "")}))
    due = [t for tool, t in srv.timeouts if tool == "scans_due"]
    assert due and all(t >= 120 for t in due)
    assert all(t is not None for _, t in srv.timeouts)


def test_scans_due_timeout_is_retried_once(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _installed(monkeypatch, {"vulture"})
    srv = Server()
    srv.due_timeouts = 1
    counts = scan.run_scan(repo, codemap.head(repo), srv, run=_runner({"vulture": (0, "", "")}))
    assert counts["recorded"] > 0 and srv.records
    assert [t for t, _ in srv.timeouts].count("scans_due") == 2


def test_two_scans_due_timeouts_fail_naming_the_call(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    srv = Server()
    srv.due_timeouts = 2
    with pytest.raises(scan.ScanError, match=r"scans_due timed out after 120s"):
        scan.run_scan(repo, codemap.head(repo), srv)
    assert srv.records == []


def test_client_default_timeout_is_30_and_override_is_per_call(monkeypatch):
    seen = []

    class R:
        status = 200
        def read(self): return b"{}"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake(request, timeout):
        seen.append(timeout)
        return R()

    monkeypatch.setattr("gban.client.urllib.request.urlopen", fake)
    c = Client("http://x")
    c.call("GET", "/a")
    c.call("GET", "/b", timeout=120.0)
    c.call("GET", "/c")
    assert TIMEOUT == 30.0 and seen == [30.0, 120.0, 30.0]


def _js_repo(tmp_path: Path) -> Path:
    (tmp_path / "web").mkdir()
    (tmp_path / "web/package.json").write_text("{}\n")
    (tmp_path / "web/a.ts").write_text("export const x = 1\n")
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    return tmp_path


_LIZARD_HIT = '"30","12","200","2","35","big@5-40@./src/a.ts","./src/a.ts","big","big(a)","5","40"\n'
_LIZARD_LOW = '"3","2","20","0","3","ok@1-3@./src/a.ts","./src/a.ts","ok","ok()","1","3"\n'


def _js_scan(tmp_path, monkeypatch, installed, outputs):
    repo = _js_repo(tmp_path)
    _installed(monkeypatch, installed)
    srv = Server(paths=("web/",))
    scan.run_scan(repo, codemap.head(repo), srv, run=_runner(outputs))
    return srv


def test_js_component_gets_complexity_finding(tmp_path, monkeypatch):
    srv = _js_scan(tmp_path, monkeypatch, {"lizard"}, {"lizard": (0, _LIZARD_HIT + _LIZARD_LOW, "")})
    r = _by(srv)["complexity"]
    assert r["category"] == "refactor" and r["path"] == "web/" and r["result"] == "complete"
    assert len(r["findings"]) == 1 and r["findings"][0]["path"] == "./src/a.ts:5"


def test_js_complexity_no_findings_is_clean(tmp_path, monkeypatch):
    srv = _js_scan(tmp_path, monkeypatch, {"lizard"}, {"lizard": (0, _LIZARD_LOW, "")})
    r = _by(srv)["complexity"]
    assert r["result"] == "complete" and r["findings"] == []


def test_js_complexity_missing_lizard_is_tool_missing(tmp_path, monkeypatch):
    srv = _js_scan(tmp_path, monkeypatch, set(), {})
    assert _by(srv)["complexity"]["result"] == "tool_missing"


@pytest.mark.parametrize("res", [(2, "", "boom"), (0, "garbage not csv\n", "")])
def test_js_complexity_bad_run_is_incomplete(tmp_path, monkeypatch, res):
    srv = _js_scan(tmp_path, monkeypatch, {"lizard"}, {"lizard": res})
    assert _by(srv)["complexity"]["result"] == "incomplete"


def test_dry_run_lists_complexity(tmp_path, monkeypatch, capsys):
    repo = _js_repo(tmp_path)
    _installed(monkeypatch, set())
    scan.run_scan(repo, codemap.head(repo), Server(paths=("web/",)), dry_run=True)
    assert "complexity\tnot installed" in capsys.readouterr().out
