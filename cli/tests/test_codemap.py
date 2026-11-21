"""`gban code map` — build and refresh the code graph (GRPH-1706)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from gban import codemap, config
from gban.cli import main

_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
                   check=True, capture_output=True, env={**os.environ, **_ENV})


def _repo(tmp_path: Path) -> Path:
    files = {
        "pkg/src/mypkg/__init__.py": '"""The package."""\n',
        "pkg/src/mypkg/a.py": '"""Module a."""\nfrom mypkg import b\nimport mypkg.c\n',
        "pkg/src/mypkg/b.py": "x = 1\n",
        "pkg/src/mypkg/c.py": "# the c module\n",
        "web/src/a.ts": "import { b } from './b'\nexport const a = b\n",
        "web/src/b.ts": "// the b helper\nexport const b = 1\n",
        "README.md": "# Hello\n",
        "uv.lock": "lock\n",
        "dist-release/x.py": "x = 1\n",
    }
    blobs = {"logo.png": b"\x89PNG\0\0"}
    for name, body in files.items():
        f = tmp_path / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(body.encode("utf-8"))  # bytes: no CRLF or cp1252 on Windows
    for name, data in blobs.items():
        (tmp_path / name).write_bytes(data)
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "core.autocrlf", "false")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    return tmp_path


class FakeGraph:
    """An in-memory stand-in for the server's get_code_map / describe_code."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.edges: set[tuple] = set()
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, tool: str, args: dict) -> dict:
        self.calls.append((tool, args))
        if tool == "get_code_map":
            return {"nodes": list(self.nodes.values())}
        for n in args.get("nodes", []):
            old = self.nodes.get(n["path"], {"path": n["path"], "summary": ""})
            self.nodes[n["path"]] = {**old, **n, "fresh": True,
                                     "revision": args.get("revision", "")}
        for e in args.get("edges", []):
            self.edges.add((e["src"], e["dst"], e["type"]))
        for p in args.get("stale", []):
            if p in self.nodes:
                self.nodes[p]["fresh"] = False
        return {"marked_stale": len(args.get("stale", []))}


def _run(repo: Path, graph: FakeGraph) -> dict:
    built = codemap.build(repo)
    planned = codemap.plan(built, codemap.existing_map(graph))
    codemap.apply(graph, built, planned)
    return planned


def test_fills_graph_at_head_and_skips_binaries(tmp_path):
    repo = _repo(tmp_path)
    graph = FakeGraph()
    _run(repo, graph)
    rev = codemap.head(repo)
    assert set(graph.nodes) == {
        "pkg/src/mypkg/__init__.py", "pkg/src/mypkg/a.py", "pkg/src/mypkg/b.py",
        "pkg/src/mypkg/c.py", "web/src/a.ts", "web/src/b.ts", "README.md"}
    assert all(n["revision"] == rev for n in graph.nodes.values())
    assert graph.nodes["README.md"]["summary"] == "Hello"
    assert graph.nodes["pkg/src/mypkg/a.py"]["summary"] == "Module a."
    assert graph.nodes["web/src/b.ts"]["summary"] == "the b helper"
    assert graph.nodes["README.md"]["content_hash"] == codemap.content_hash("# Hello\n")
    assert not any("prune" in args for _, args in graph.calls)


def test_python_and_ts_edges(tmp_path):
    graph = FakeGraph()
    _run(_repo(tmp_path), graph)
    assert ("pkg/src/mypkg/a.py", "pkg/src/mypkg/b.py", "imports") in graph.edges
    assert ("pkg/src/mypkg/a.py", "pkg/src/mypkg/c.py", "imports") in graph.edges
    assert ("web/src/a.ts", "web/src/b.ts", "imports") in graph.edges


def test_rerun_keeps_existing_summary_byte_for_byte(tmp_path):
    repo = _repo(tmp_path)
    graph = FakeGraph()
    graph.nodes["README.md"] = {"path": "README.md", "summary": "  hand written — keep  ",
                                "fresh": True}
    planned = _run(repo, graph)
    assert graph.nodes["README.md"]["summary"] == "  hand written — keep  "
    assert planned["kept"] == 1
    again = _run(repo, graph)
    assert again["kept"] == 5 and again["new"] == 0
    assert graph.nodes["README.md"]["summary"] == "  hand written — keep  "


def test_deleted_file_reads_stale_after_run(tmp_path):
    repo = _repo(tmp_path)
    graph = FakeGraph()
    _run(repo, graph)
    _git(repo, "rm", "-q", "web/src/b.ts")
    _git(repo, "commit", "-m", "drop b")
    planned = _run(repo, graph)
    assert planned["gone"] == ["web/src/b.ts"]
    assert graph.nodes["web/src/b.ts"]["fresh"] is False
    assert graph.nodes["web/src/a.ts"]["fresh"] is True


def test_dry_run_writes_nothing_and_prints_counts(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    graph = FakeGraph()
    monkeypatch.setattr(codemap, "mcp_caller", lambda client, project: graph)
    monkeypatch.setenv(config.URL_ENV, "http://example.invalid")
    monkeypatch.setenv("GBFLEET_API_KEY", "k")
    code = main(["--project", "p", "code", "map", "--dry-run", "--repo", str(repo)])
    out = capsys.readouterr().out
    assert code == 0
    assert codemap.head(repo) in out
    assert "10 tracked" in out and "7 to describe" in out and "0 existing" in out
    assert "7 new" in out and "0 gone" in out and "4 edges" in out
    assert [t for t, _ in graph.calls] == ["get_code_map"]


def test_directory_and_skipped_nodes_are_not_gone(tmp_path):
    repo = _repo(tmp_path)
    graph = FakeGraph()
    for path, kind in (("pkg/src/mypkg", "module"), ("pkg/src/", "module"),
                       ("uv.lock", "config"), ("logo.png", "file"),
                       ("dist-release/x.py", "file"), ("pkg/old", "module")):
        graph.nodes[path] = {"path": path, "kind": kind, "summary": "hand", "fresh": True}
    planned = _run(repo, graph)
    assert planned["gone"] == ["pkg/old"]
    for path in ("pkg/src/mypkg", "pkg/src/", "uv.lock", "logo.png", "dist-release/x.py"):
        assert graph.nodes[path]["fresh"] is True, path
    assert graph.nodes["pkg/old"]["fresh"] is False
