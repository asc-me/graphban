"""H5 and C1 ignore compiled bytecode a child's `git add -A` sweeps in (GRPH-1889)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gbfleet.bench import run as run_mod
from gbfleet.bench import suite as suite_mod
from gbfleet.bench.template import fixture


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    return fixture.build(tmp_path_factory.mktemp("fx") / "repo")


def _sh(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={**fixture._ENV, "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"})


def _child(repo: Path, tmp_path: Path, *, with_module=True) -> str:
    tree = tmp_path / "wt"
    _sh(repo, "worktree", "add", "-q", "-b", "child", str(tree), "root")
    if with_module:
        (tree / "pkg/m2.py").write_text(fixture.BUILD[2][3])
    (tree / "tests/test_m2.py").write_text(fixture._test_file("m2", "clamp", fixture.BUILD[2][4]))
    for d in ("pkg", "tests"):
        (tree / d / "__pycache__").mkdir()
        (tree / d / "__pycache__/m2.cpython-315.pyc").write_bytes(b"\0")
    _sh(tree, "add", "-A", "-f")
    _sh(tree, "commit", "-q", "-m", "child")
    return "child"


def _task():
    tpl = suite_mod.load(suite_mod.template_manifest())
    task = next(t for t in tpl.tasks if t.id == "build-clamp")
    return task


def test_fixture_root_ignores_bytecode(repo):
    out = subprocess.run(["git", "-C", str(repo), "show", "root:.gitignore"], capture_output=True, text=True)
    assert "__pycache__/" in out.stdout and "*.pyc" in out.stdout


def test_h5_and_c1_ignore_committed_bytecode(repo, tmp_path):
    branch = _child(repo, tmp_path)
    task = _task()
    leaves = run_mod.leaf_verdicts(task, repo, branch, "check_green")
    assert leaves["H5"]["verdict"] == "pass", leaves["H5"]
    assert leaves["C1"]["verdict"] == "pass", leaves["C1"]


def test_work_paths_filter(repo):
    assert run_mod.work_paths(["a.py", "pkg/__pycache__/a.cpython-3.pyc", "x.pyo", "t.pyc"]) == ["a.py"]
