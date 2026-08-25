"""S2 — a declared test command, checked before spawn (PRD-24 D3, D4, AC 3-4).

The refusals are the slice. A config that names a command which cannot run means the agent
could never verify its own work, so it must refuse at spawn — ten minutes of building to
discover it at the first `run_tests` is the outcome this prevents.

The other half is honesty about what was read: `ok` comes from the exit code and cannot be
wrong; the counts come from parsing a runner this module does not choose, so when they cannot
be read they are `None`, never `0`.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from gbagent import verify
from gbagent.config import CONFIG_NAME, ConfigRefused, VerifyConfig, load
from gbagent.workspace import ToolError


def _repo(tmp_path: Path, toml: str | None, *, script: str | None = None) -> Path:
    root = tmp_path / "repo"
    (root / "backend").mkdir(parents=True)
    if toml is not None:
        (root / CONFIG_NAME).write_text(toml, encoding="utf-8")
    if script is not None:
        p = root / "backend" / "runner.sh"
        p.write_text(script, encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return root


# ---- refusals happen BEFORE anything is spawned -------------------------------------


def test_no_config_refuses_and_says_why(tmp_path):
    with pytest.raises(ConfigRefused) as exc:
        load(_repo(tmp_path, None))

    assert CONFIG_NAME in str(exc.value)
    assert "declared, never guessed" in str(exc.value)


def test_malformed_toml_refuses(tmp_path):
    with pytest.raises(ConfigRefused) as exc:
        load(_repo(tmp_path, "[tests\ncommand = broken"))

    assert "not valid TOML" in str(exc.value)


def test_a_config_with_no_tests_table_refuses(tmp_path):
    with pytest.raises(ConfigRefused) as exc:
        load(_repo(tmp_path, '[other]\nx = 1\n'))

    assert "[tests]" in str(exc.value)


def test_a_tests_table_with_no_command_refuses(tmp_path):
    with pytest.raises(ConfigRefused) as exc:
        load(_repo(tmp_path, '[tests]\ncwd = "backend"\n'))

    assert "no `command`" in str(exc.value)


def test_an_empty_command_refuses(tmp_path):
    with pytest.raises(ConfigRefused) as exc:
        load(_repo(tmp_path, '[tests]\ncommand = ""\n'))

    assert "empty" in str(exc.value)


def test_a_command_naming_a_binary_that_is_not_on_path_refuses(tmp_path):
    with pytest.raises(ConfigRefused) as exc:
        load(_repo(tmp_path, '[tests]\ncommand = "definitely-not-a-real-binary-xyz -q"\n'))

    assert "not on PATH" in str(exc.value)


def test_a_command_naming_a_path_that_does_not_exist_refuses(tmp_path):
    with pytest.raises(ConfigRefused) as exc:
        load(_repo(tmp_path, '[tests]\ncommand = "./.venv/bin/python -m pytest"\n'))

    assert "does not exist" in str(exc.value)


def test_a_cwd_outside_the_worktree_refuses(tmp_path):
    """The config is not exempt from S1's boundary. Pointing the test run at `../../` would
    run somebody else's suite and report it as this agent's verification."""
    with pytest.raises(ConfigRefused) as exc:
        load(_repo(tmp_path, '[tests]\ncommand = "echo hi"\ncwd = "../.."\n'))

    assert "outside the worktree" in str(exc.value)


# ---- what this repository actually declares ------------------------------------------


def test_a_command_given_as_a_repo_relative_path_is_accepted(tmp_path):
    """THE CASE A `which`-ONLY CHECK WOULD HAVE BROKEN.

    This project's own loop is `./.venv/bin/python -m pytest -q`, and AGENTS.md says outright
    that pytest is NOT on the host PATH. A check that only consulted PATH would refuse the
    repository it was written for.
    """
    root = _repo(tmp_path, '[tests]\ncommand = "./runner.sh -q"\ncwd = "backend"\n',
                 script="#!/bin/sh\nexit 0\n")

    cfg = load(root)

    assert cfg.argv == ["./runner.sh", "-q"]
    assert cfg.cwd == (root / "backend").resolve()


def test_a_bare_name_is_looked_up_on_path(tmp_path):
    cfg = load(_repo(tmp_path, '[tests]\ncommand = "echo hello"\n'))

    assert cfg.argv[0] == "echo"


def test_a_command_may_be_given_as_a_list(tmp_path):
    cfg = load(_repo(tmp_path, '[tests]\ncommand = ["echo", "a b"]\n'))

    assert cfg.argv == ["echo", "a b"], "a list is taken as-is, not re-split"


def test_the_command_is_not_run_through_a_shell(tmp_path):
    """D4: there is no shell. A pipe becomes a literal argument rather than a second command."""
    cfg = load(_repo(tmp_path, '[tests]\ncommand = "echo a | rm -rf /"\n'))

    assert "|" in cfg.argv, cfg.argv
    assert cfg.argv[0] == "echo"


# ---- run_tests reports what it knows, and no more -------------------------------------


def _cfg(root: Path, script: str) -> VerifyConfig:
    p = root / "backend" / "r.sh"
    p.write_text(script, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return VerifyConfig(argv=[str(p)], cwd=(root / "backend").resolve(), source="r.sh")


def test_a_passing_run_is_ok(tmp_path):
    root = _repo(tmp_path, None)

    out = verify.run_tests(root, _cfg(root, "#!/bin/sh\necho '261 passed, 1 skipped in 51.86s'\n"))

    assert out["ok"] is True and out["exit_code"] == 0
    assert out["passed"] == 261


def test_a_failing_run_names_the_tests_and_returns_a_tail(tmp_path):
    """AC-4. The name is what the agent's next turn has to reference."""
    root = _repo(tmp_path, None)
    script = (
        "#!/bin/sh\n"
        "echo 'FAILED tests/test_a.py::test_boundary_refuses_symlink - AssertionError'\n"
        "echo 'FAILED tests/test_b.py::test_turn_budget_releases - AssertionError'\n"
        "echo '2 failed, 231 passed in 3.10s'\n"
        "exit 1\n"
    )

    out = verify.run_tests(root, _cfg(root, script))

    assert out["ok"] is False and out["exit_code"] == 1
    assert out["failed"] == 2 and out["passed"] == 231
    assert "tests/test_a.py::test_boundary_refuses_symlink" in out["failed_tests"]
    assert "AssertionError" in out["tail"]


def test_an_unreadable_failure_reports_None_rather_than_zero(tmp_path):
    """THE LIE THIS AVOIDS.

    A runner whose output this parser does not understand must not come back `failed: 0` on a
    run that failed — confident, structured and false is the worst possible answer. `ok` is the
    exit code and cannot be wrong; the counts say "I could not tell".
    """
    root = _repo(tmp_path, None)

    out = verify.run_tests(root, _cfg(root, "#!/bin/sh\necho 'kaboom, in a shape nobody parses'\nexit 3\n"))

    assert out["ok"] is False and out["exit_code"] == 3
    assert out["passed"] is None and out["failed"] is None
    assert out["failed_tests"] == []
    assert "kaboom" in out["tail"], "the tail is always there for what the parser could not read"


def test_a_long_run_is_truncated_to_a_bounded_tail(tmp_path):
    root = _repo(tmp_path, None)
    script = "#!/bin/sh\nfor i in $(seq 1 500); do echo \"line $i\"; done\nexit 1\n"

    out = verify.run_tests(root, _cfg(root, script))

    assert out["truncated"] is True
    assert len(out["tail"].splitlines()) == verify.TAIL_LINES
    assert "line 500" in out["tail"], "the tail is the END of the output, where the failure is"


def test_a_hanging_test_command_is_killed_and_says_nothing_is_known(tmp_path):
    """An unattended agent that hangs is worse than one that fails — D6's turn budget cannot
    help if a single turn never returns."""
    root = _repo(tmp_path, None)

    with pytest.raises(ToolError) as exc:
        verify.run_tests(root, _cfg(root, "#!/bin/sh\nsleep 30\n"), timeout=1)

    assert "Nothing is known" in str(exc.value)


# ---- git_diff -------------------------------------------------------------------------


def test_git_diff_reports_a_change_and_is_scoped_to_the_worktree(tmp_path):
    import subprocess

    root = _repo(tmp_path, None)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.io"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "a.txt").write_text("one\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "x"], cwd=root, check=True)
    (root / "a.txt").write_text("two\n")

    out = verify.git_diff(root)

    assert out["empty"] is False
    assert "-one" in out["diff"] and "+two" in out["diff"]


def test_git_diff_refuses_a_path_outside_the_worktree(tmp_path):
    from gbagent.workspace import OutsideWorktree

    with pytest.raises(OutsideWorktree):
        verify.git_diff(_repo(tmp_path, None), path="../..")


REPO = Path(__file__).resolve().parents[2]


def test_this_repository_declares_the_loop_agents_md_documents():
    """Dogfood, and environment-independent on purpose.

    This asserts the FILE, not the machine: that `.gbagent.toml` exists and names the command
    AGENTS.md documents. It catches the drift that matters — someone changing the loop in one
    place and not the other — and it runs everywhere, including a CI job that never builds a
    backend virtualenv.
    """
    import tomllib

    data = tomllib.loads((REPO / CONFIG_NAME).read_text(encoding="utf-8"))

    assert data["tests"]["command"] == "./.venv/bin/python -m pytest -q"
    assert data["tests"]["cwd"] == "backend"
    assert data["tests"]["command"] in (REPO / "AGENTS.md").read_text(encoding="utf-8"), \
        "the declared command and the documented loop have drifted apart"


def test_this_repository_loads_where_its_interpreter_is_installed():
    """The other half, and it can only run where the tree is actually built.

    The CI fleet job does not create `backend/.venv`, and `load` correctly REFUSES there —
    that refusal is the feature, not a failure. Skipping loudly rather than weakening the
    check: a suite that skips silently reads as green when it ran nothing (GRPH-432).
    """
    if not (REPO / "backend" / ".venv" / "bin" / "python").exists():
        pytest.skip("backend/.venv is not built here; the executable check has nothing to "
                    "find, and refusing is the correct behaviour")

    cfg = load(REPO)

    assert cfg.argv[:2] == ["./.venv/bin/python", "-m"], cfg.argv
    assert cfg.cwd.name == "backend"
