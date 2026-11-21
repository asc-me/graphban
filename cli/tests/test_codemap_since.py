"""`gban code map --since` — incremental refresh (GRPH-1820)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gban import codemap, config
from gban.cli import main
from test_codemap import FakeGraph, _git, _repo


class PinnedGraph(FakeGraph):
    """FakeGraph whose get_code_map reports the pinned revision like the server does."""

    def __call__(self, tool, args):
        out = super().__call__(tool, args)
        if tool == "get_code_map":
            known = {n.get("revision", "") for n in self.nodes.values()}
            known.discard("")
            unknown = any(not n.get("revision") for n in self.nodes.values())
            out["revision"] = next(iter(known)) if len(known) == 1 and not unknown else None
            out["revisions"] = {"distinct": len(known), "unknown_nodes": int(unknown)}
        return out


BIG = "".join(f"line {i} of a longer file\n" for i in range(40))


def _origin(tmp_path: Path):
    """work repo with an origin remote, pushed at rev A."""
    work = _repo(tmp_path / "work")
    (work / "big.py").write_text('"""Big."""\n' + BIG)
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "big")
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "origin", "main")
    return work


def _full(work: Path, graph: FakeGraph) -> str:
    rev = codemap.head(work)
    built = codemap.build(work)
    codemap.apply(graph, built, codemap.plan(built, codemap.existing_map(graph)))
    return rev


def _since(work, graph, extra=()):
    env = {config.URL_ENV: "http://example.invalid", "GBFLEET_API_KEY": "k"}
    import os
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    orig = codemap.mcp_caller
    codemap.mcp_caller = lambda client, project: graph
    try:
        return main(["--project", "p", "code", "map", "--since", *extra, "--repo", str(work)])
    finally:
        codemap.mcp_caller = orig
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _advance(work: Path) -> str:
    (work / "web/src/new.ts").write_text("// new file\nexport const n = 1\n")
    (work / "pkg/src/mypkg/b.py").write_text("x = 2\n")
    _git(work, "mv", "big.py", "pkg/src/mypkg/big2.py")
    (work / "pkg/src/mypkg/big2.py").write_text('"""Big."""\n' + BIG + "extra = 1\n")
    _git(work, "rm", "-q", "web/src/b.ts")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "B")
    _git(work, "push", "origin", "main")
    return codemap.head(work)


def test_incremental_add_modify_rename_delete(tmp_path, capsys):
    work = _origin(tmp_path)
    graph = PinnedGraph()
    _full(work, graph)
    graph.nodes["big.py"]["summary"] = "hand written big"
    graph.nodes["big.py"]["name"] = "Big"
    graph.nodes["pkg/src/mypkg/c.py"]["summary"] = "hand c"
    rev_b = _advance(work)
    before = {p: dict(n) for p, n in graph.nodes.items()}
    graph.calls.clear()
    assert _since(work, graph) == 0
    n = graph.nodes
    assert n["web/src/new.ts"]["summary"] == "new file"
    assert n["pkg/src/mypkg/b.py"]["content_hash"] == codemap.content_hash("x = 2\n")
    assert n["pkg/src/mypkg/big2.py"]["summary"] == "hand written big"
    assert n["pkg/src/mypkg/big2.py"]["name"] == "Big"
    assert n["big.py"]["fresh"] is False
    assert n["web/src/b.ts"]["fresh"] is False  # sabotage the delete path and this fails
    assert n["pkg/src/mypkg/c.py"]["summary"] == "hand c"
    assert n["pkg/src/mypkg/c.py"]["content_hash"] == before["pkg/src/mypkg/c.py"]["content_hash"]
    fresh = [p for p, v in n.items() if v["fresh"]]
    assert all(n[p]["revision"] == rev_b for p in fresh)
    # reads went through the object store: no full tracked-file listing of the working tree
    assert not any("prune" in a for _, a in graph.calls)
    # second run at the same target posts nothing
    graph.calls.clear()
    assert _since(work, graph) == 0
    assert "current at " + rev_b in capsys.readouterr().out
    assert [t for t, _ in graph.calls] == ["get_code_map"]


def test_dry_run_posts_nothing(tmp_path, capsys):
    work = _origin(tmp_path)
    graph = PinnedGraph()
    _full(work, graph)
    rev_b = _advance(work)
    graph.calls.clear()
    assert _since(work, graph, ["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert rev_b in out and "1 added" in out and "1 renamed" in out and "1 deleted" in out
    assert [t for t, _ in graph.calls] == ["get_code_map"]


def test_dirty_working_tree_never_leaks(tmp_path):
    work = _origin(tmp_path)
    graph = PinnedGraph()
    _full(work, graph)
    _advance(work)
    (work / "local_only.py").write_text("y = 1\n")
    _git(work, "add", "-A")
    assert _since(work, graph) == 0
    assert "local_only.py" not in graph.nodes


def test_unreachable_pin_falls_back_to_full_scan(tmp_path, capsys):
    work = _origin(tmp_path)
    graph = PinnedGraph()
    _full(work, graph)
    for n in graph.nodes.values():
        n["revision"] = "0" * 40
    _advance(work)
    assert _since(work, graph) == 0
    assert "missing or unreachable" in capsys.readouterr().out
    assert graph.nodes["web/src/b.ts"]["fresh"] is False


def test_non_ancestor_pin_falls_back(tmp_path, capsys):
    work = _origin(tmp_path)
    graph = PinnedGraph()
    _git(work, "checkout", "-q", "-b", "side")
    (work / "side.txt").write_text("s\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "side")
    side = codemap.head(work)
    _git(work, "checkout", "-q", "main")
    for p in ("README.md",):
        graph.nodes[p] = {"path": p, "summary": "x", "fresh": True, "revision": side}
    _advance(work)
    assert _since(work, graph) == 0
    assert "not an ancestor" in capsys.readouterr().out


def test_mixed_map_falls_back(tmp_path, capsys):
    work = _origin(tmp_path)
    graph = PinnedGraph()
    rev_a = _full(work, graph)
    graph.nodes["README.md"]["revision"] = "f" * 40
    _advance(work)
    assert _since(work, graph) == 0
    assert "mixed" in capsys.readouterr().out
    assert rev_a != codemap.head(work)


def test_failed_fetch_posts_nothing(tmp_path, capsys):
    work = _origin(tmp_path)
    graph = PinnedGraph()
    _full(work, graph)
    _git(work, "remote", "set-url", "origin", str(tmp_path / "nope.git"))
    graph.calls.clear()
    assert _since(work, graph) != 0
    assert "failed" in capsys.readouterr().err
    assert graph.calls == []
