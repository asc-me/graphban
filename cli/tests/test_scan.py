"""`gban scan` (GRPH-1827): faked tool runs, fake server."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from gban import codemap, config, scan
from gban.cli import main
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
    def __init__(self, paths=("pkg/",)):
        self.paths = paths
        self.records: list[dict] = []

    def __call__(self, tool, args):
        if tool == "scans_due":
            return {"due": [{"component": "pkg", "path": p, "category": "security"} for p in self.paths]}
        assert tool == "record_scan"
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
    assert counts["incomplete"] == 3


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
    assert "4 tool_missing, 0 incomplete at " in capsys.readouterr().out
    assert os.environ["GBFLEET_API_KEY"] == "k"
