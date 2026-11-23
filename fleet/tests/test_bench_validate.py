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
