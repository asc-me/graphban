from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from gbfleet.bench import suite as suite_mod
from gbfleet.bench import validate as validate_mod
from gbfleet.bench.suite import CAPABILITY_LEAVES, SuiteError, parse
from gbfleet.cli import main

HEAD = '[suite]\nid = "s"\nversion = "1.0.0"\n'


def build_task(*, nocheck: bool = False, **over) -> str:
    row = {"id": "t1", "kind": "build", "base_sha": "HEAD", "reference": "ref",
           "size_band": "S", "capabilities": ["B7"]}
    row.update(over)
    out = "[[task]]\n"
    for k, v in row.items():
        if v is None:
            continue
        out += f"{k} = {v!r}\n".replace("'", '"')
    out += '[task.brief]\ntitle = "t"\ndescription = "d"\nacceptance = ["a"]\n'
    if row["kind"] == "build" and not nocheck:
        out += '[task.check]\ncommand = "python -m pytest -q"\ntests = ["test_x.py"]\n'
    return out


def test_valid_manifest_loads():
    s = parse(HEAD + build_task())
    assert s.tasks[0].id == "t1" and s.tasks[0].check.tests == ("test_x.py",)


def test_refuses_build_without_base_sha():
    with pytest.raises(SuiteError, match=r"t1.*base_sha"):
        parse(HEAD + build_task(base_sha=None))


def test_refuses_build_without_check():
    with pytest.raises(SuiteError, match=r"t1.*check"):
        parse(HEAD + build_task(nocheck=True))


def test_refuses_review_without_expect():
    with pytest.raises(SuiteError, match=r"t1.*expect"):
        parse(HEAD + build_task(kind="review", ref="r"))


def test_refuses_unknown_capability():
    with pytest.raises(SuiteError, match=r"t1.*capabilities.*Z9"):
        parse(HEAD + build_task(capabilities=["Z9"]))


def test_leaves_match_backend_source():
    src = (Path(__file__).resolve().parents[2] / "backend/app/services/harness.py").read_text()
    block = re.search(r"CAPABILITY_LEAVES[^=]*=\s*\((.*?)\)", src, re.DOTALL).group(1)
    assert tuple(re.findall(r'"([A-Z]\d)"', block)) == CAPABILITY_LEAVES


def git(repo: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@t")
    git(r, "config", "user.name", "t")
    (r / "mod.py").write_text("def f():\n    return 0\n")
    (r / "test_x.py").write_text("from mod import f\n\n\ndef test_x():\n    assert f() == 1\n")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "base")
    base = git(r, "rev-parse", "HEAD")
    (r / "mod.py").write_text("def f():\n    return 1\n")
    git(r, "commit", "-qam", "fix")
    return r, base, git(r, "rev-parse", "HEAD")


PYTEST = f"{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider"


def manifest(tmp_path: Path, repo_info, *, base_is_green=False, command=PYTEST) -> Path:
    _, base, fixed = repo_info
    sha = fixed if base_is_green else base
    p = tmp_path / "suite.toml"
    # sys.executable, not a bare `python`: the host PATH may carry no python at all.
    p.write_text(HEAD + build_task(base_sha=sha, reference=fixed).replace(
        "python -m pytest -q", command))
    return p


def test_validate_ok(tmp_path, repo, capsys):
    p = manifest(tmp_path, repo)
    assert main(["bench", "validate", str(p), "--repo", str(repo[0])]) == 0
    assert "t1: ok" in capsys.readouterr().out


def test_validate_green_at_base_fails(tmp_path, repo, capsys):
    p = manifest(tmp_path, repo, base_is_green=True)
    assert main(["bench", "validate", str(p), "--repo", str(repo[0])]) == 1
    assert "t1: green_at_base" in capsys.readouterr().out


def test_sabotage_skipping_base_run_is_caught(tmp_path, repo):
    """Skipping the at-base run lets the green-at-base task pass; the test must notice."""
    p = manifest(tmp_path, repo, base_is_green=True)
    s = suite_mod.load(p)
    sabotaged = validate_mod.validate(s, repo[0], skip_base=True)
    assert all(v.ok for v in sabotaged)  # the sabotage hides the defect...
    honest = validate_mod.validate(s, repo[0])
    assert [v.status for v in honest] == ["green_at_base"]  # ...which the real run reports


def test_unrunnable_check_is_not_discriminating(tmp_path, repo, capsys):
    """A command that cannot start is its own status, never a red check at base."""
    p = manifest(tmp_path, repo, command="gbfleet-no-such-binary-xyz")
    assert main(["bench", "validate", str(p), "--repo", str(repo[0])]) == 1
    assert "t1: check_unrunnable" in capsys.readouterr().out
