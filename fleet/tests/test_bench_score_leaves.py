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


def _claiming(leaf):
    import dataclasses
    return dataclasses.replace(_task(), capabilities=(leaf,))


@pytest.mark.parametrize("outcome,verdict", [("check_green", "pass"), ("check_red", "fail"), ("no_branch", "fail")])
def test_b7_scored_from_the_check(repo, outcome, verdict):
    out = run_mod.leaf_verdicts(_claiming("B7"), repo, "root", outcome)
    assert out["B7"]["verdict"] == verdict, out["B7"]


def test_b7_excluded_with_reason_when_check_not_evaluated(repo):
    out = run_mod.leaf_verdicts(_claiming("B7"), repo, "root", "harness_error")
    assert out["B7"]["verdict"] == "excluded" and out["B7"]["reason"]


@pytest.mark.parametrize("outcome", ["check_green", "check_red", "no_branch"])
@pytest.mark.parametrize("leaf", suite_mod.CAPABILITY_LEAVES)
def test_every_claimable_leaf_gets_an_entry(repo, leaf, outcome):
    """Ratchet (GRPH-1902): every leaf a task may claim comes back scored, or excluded for a reason
    that leaf's own handling states. An unhandled leaf raises, so it fails here, not silently."""
    out = run_mod.leaf_verdicts(_claiming(leaf), repo, "root", outcome)
    assert leaf in out
    entry = out[leaf]
    if entry["verdict"] == "excluded":
        assert entry["reason"], entry
        if leaf not in run_mod.UNSCORED_LEAVES:
            assert entry["reason"] != f"no predicate scores {leaf}", entry


def test_scored_leaves_are_not_also_listed_unscored():
    handled = set(run_mod.CHECK_LEAVES) | {"E1", "E2", "H5", "C1"}
    assert not handled & set(run_mod.UNSCORED_LEAVES)
    assert handled | set(run_mod.UNSCORED_LEAVES) == set(suite_mod.CAPABILITY_LEAVES)


def test_a_leaf_no_predicate_handles_raises(repo, monkeypatch):
    monkeypatch.setattr(run_mod, "CHECK_LEAVES", ("A1", "A3", "A5", "C2"))
    with pytest.raises(ValueError, match="B7"):
        run_mod.leaf_verdicts(_claiming("B7"), repo, "root", "check_green")
