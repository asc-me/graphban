from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from gbfleet.bench import validate as V


def _git(repo: Path, *a: str) -> None:
    subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)


def _repo(tmp_path: Path, test_body: str) -> Path:
    tmp_path.mkdir(exist_ok=True)
    _git(tmp_path, "init", "-q")
    (tmp_path / "t_x.py").write_text(test_body)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")
    return tmp_path


def test_uv_fallback_installs_the_dev_extra(tmp_path):
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "uv.lock").write_text("")
    (tmp_path / "svc" / "pyproject.toml").write_text(
        '[project]\nname="x"\n[project.optional-dependencies]\ndev=["pytest"]\n')
    argv, cwd = V.resolve_check(tmp_path, tmp_path, V.Check("python -m pytest", ("svc/tests/test_a.py",)))
    assert argv[:6] == ["uv", "run", "--extra", "dev", "python", "-m"] and cwd == "svc"


def test_uv_fallback_without_dev_extra_adds_pytest(tmp_path):
    (tmp_path / "uv.lock").write_text("")
    (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\n')
    assert V._interpreter(tmp_path, "") == ["uv", "run", "--with", "pytest", "python"]


def test_missing_runner_module_is_unrunnable_not_red(tmp_path):
    repo = _repo(tmp_path, "def test_a():\n    pass\n")
    check = V.Check(f"{sys.executable} -m no_such_runner_mod", ("t_x.py",))
    outcome, why = V.run_check(repo, "HEAD", check)
    assert outcome == V.UNRUNNABLE and "no_such_runner_mod" in why


def test_real_red_test_is_still_fail_and_green_is_pass(tmp_path):
    cmd = f"{sys.executable} -m pytest -q"
    red = V.run_check(_repo(tmp_path / "r", "def test_a():\n    assert 0\n"), "HEAD", V.Check(cmd, ("t_x.py",)))
    green = V.run_check(_repo(tmp_path / "g", "def test_a():\n    pass\n"), "HEAD", V.Check(cmd, ("t_x.py",)))
    assert red[0] == V.FAIL and green[0] == V.PASS


def test_pytest_nothing_collected_is_unrunnable(tmp_path):
    repo = _repo(tmp_path, "x = 1\n")
    outcome, _ = V.run_check(repo, "HEAD", V.Check(f"{sys.executable} -m pytest -q", ("t_x.py",)))
    assert outcome == V.UNRUNNABLE


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    tmp_path.mkdir(exist_ok=True)
    _git(tmp_path, "init", "-q")
    for path, text in files.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")
    return tmp_path


PYTEST = f"{sys.executable} -m pytest -q -p no:cacheprovider"
OK_TEST = "def test_a():\n    pass\n"


def test_conftest_importing_a_missing_module_is_red_not_unrunnable(tmp_path):
    """A conftest that imports a module the tree lacks is a broken tree (pytest exit 4)."""
    repo = _tree(tmp_path, {"tests/conftest.py": "import newmod\n", "tests/test_a.py": OK_TEST})
    outcome, _ = V.run_check(repo, "HEAD", V.Check(PYTEST, ("tests/test_a.py",)))
    assert outcome == V.FAIL


def test_conftest_importing_broken_code_is_red_not_unrunnable(tmp_path):
    repo = _tree(tmp_path, {"app.py": "def (:\n", "tests/conftest.py": "import app\n",
                            "tests/test_a.py": OK_TEST})
    outcome, _ = V.run_check(repo, "HEAD", V.Check(PYTEST, ("tests/test_a.py",)))
    assert outcome == V.FAIL


def test_unittest_test_importing_a_missing_module_is_red(tmp_path):
    repo = _tree(tmp_path, {"t_x.py": "import unittest\nimport newmod\n"})
    outcome, _ = V.run_check(repo, "HEAD", V.Check(f"{sys.executable} -m unittest", ("t_x.py",)))
    assert outcome == V.FAIL


def test_js_check_with_no_node_modules_is_unrunnable(tmp_path):
    repo = _tree(tmp_path, {"web/package.json": "{}", "web/src/a.test.ts": "x\n"})
    outcome, why = V.run_check(repo, "HEAD", V.Check("npm test --", ("web/src/a.test.ts",)))
    assert outcome == V.UNRUNNABLE and "node_modules" in why


def test_js_check_borrows_the_repos_node_modules(tmp_path):
    repo = _tree(tmp_path / "r", {"web/package.json": "{}"})
    tree = _tree(tmp_path / "t", {"web/package.json": "{}"})
    (repo / "web" / "node_modules").mkdir()
    assert V._js_deps_missing(repo, tree, ["npm", "test"], "web") == ""
    assert (tree / "web" / "node_modules").resolve() == (repo / "web" / "node_modules").resolve()
    assert V._js_deps_missing(repo, tree, [sys.executable, "-m", "pytest"], "nope") == ""


TWO_COMPONENTS = {
    "a/pyproject.toml": "[project]\nname='a'\n", "a/amod.py": "def ok():\n    return True\n",
    "a/tests/test_a.py": "import amod\n\n\ndef test_a():\n    assert amod.ok()\n",
    "b/pyproject.toml": "[project]\nname='b'\n", "b/bmod.py": "def ok():\n    return True\n",
    "b/tests/test_b.py": "import bmod\n\n\ndef test_b():\n    assert bmod.ok()\n",
}


def test_tests_in_two_components_run_each_in_its_own(tmp_path):
    """From a/ alone, b/tests cannot import bmod: one run in one component is always red."""
    repo = _tree(tmp_path, TWO_COMPONENTS)
    check = V.Check(PYTEST, ("a/tests/test_a.py", "b/tests/test_b.py"))
    assert [p.tests for p in V.split_by_component(repo, check)] == [("a/tests/test_a.py",),
                                                                    ("b/tests/test_b.py",)]
    assert V.run_check(repo, "HEAD", check)[0] == V.PASS


def test_one_red_component_makes_the_whole_check_red(tmp_path):
    files = dict(TWO_COMPONENTS, **{"b/bmod.py": "def ok():\n    return False\n"})
    repo = _tree(tmp_path, files)
    check = V.Check(PYTEST, ("a/tests/test_a.py", "b/tests/test_b.py"))
    assert V.run_check(repo, "HEAD", check)[0] == V.FAIL
