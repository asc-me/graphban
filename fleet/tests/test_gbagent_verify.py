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
import shlex
import stat
import sys
from pathlib import Path

import pytest

from gbagent import config, tools, verify
from gbagent.config import CONFIG_NAME, ConfigRefused, VerifyConfig, load
from gbagent.workspace import ToolError
from conftest import make_stub_script, stub_argv, stub_command  # noqa: E402


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
        load(_repo(tmp_path, '[tests]\ncommand = "git hi"\ncwd = "../.."\n'))

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
    # `git`, not `echo`. `echo` is a cmd BUILTIN on Windows, so `shutil.which` finds
    # nothing and the config is refused — for a reason that has nothing to do with
    # looking a bare name up on PATH, which is what this is about. The suite already
    # requires git on both platforms.
    cfg = load(_repo(tmp_path, '[tests]\ncommand = "git hello"\n'))

    assert cfg.argv[0] == "git"


def test_a_command_may_be_given_as_a_list(tmp_path):
    cfg = load(_repo(tmp_path, '[tests]\ncommand = ["git", "a b"]\n'))

    assert cfg.argv == ["git", "a b"], "a list is taken as-is, not re-split"


def test_the_command_is_not_run_through_a_shell(tmp_path):
    """D4: there is no shell. A pipe becomes a literal argument rather than a second command."""
    cfg = load(_repo(tmp_path, '[tests]\ncommand = "git a | rm -rf /"\n'))

    assert "|" in cfg.argv, cfg.argv
    assert cfg.argv[0] == "git"


# ---- run_tests reports what it knows, and no more -------------------------------------


def _setup_repo(tmp_path: Path, *stubs: dict, tests: str = "git hi") -> tuple[Path, list[str]]:
    """A repo whose `[setup].commands` are interpreter-run stubs.

    `touch` and `false` are not programs on Windows, and `[setup]` runs argv without a
    shell — so the POSIX spelling of "make a file" and "fail" simply had nothing to run.
    Each stub becomes `"<interpreter>" "<script>"`, quoted and forward-slashed because
    `shlex.split` treats a backslash as an escape (GRPH-589).

    Returns the repo root and the command strings, so a test can assert on what ran.
    """
    root = tmp_path / "repo"
    (root / "backend").mkdir(parents=True)
    commands = []
    for i, stub in enumerate(stubs):
        script = make_stub_script(root / f"setup{i}.py", **stub)
        commands.append(stub_command(script))
    # TOML LITERAL strings (single quotes): the commands contain double quotes around
    # the interpreter path, and wrapping those in a basic string produced `""C:/..."` —
    # malformed, which `prepare` reported as "no setup commands" rather than as a broken
    # config, so the test failed by finding nothing to run.
    listed = ", ".join(f"'{c}'" for c in commands)
    (root / CONFIG_NAME).write_text(
        f'[tests]\ncommand = "{tests}"\n\n[setup]\ncommands = [{listed}]\n',
        encoding="utf-8",
    )
    return root, commands


def _cfg(root: Path, **stub) -> VerifyConfig:
    """A test runner stand-in, run as `[interpreter, script]`.

    It used to be a `#!/bin/sh` file executed by path, which Windows cannot run at all —
    "not a valid Win32 application". What these stubs actually need is to print, exit,
    sleep and loop, and Python does all four on both platforms (GRPH-589).
    """
    p = make_stub_script(root / "backend" / "r.py", **stub)
    return VerifyConfig(argv=stub_argv(p), cwd=(root / "backend").resolve(), source="r.py")


def test_a_passing_run_is_ok(tmp_path):
    root = _repo(tmp_path, None)

    out = verify.run_tests(root, _cfg(root, prints=("261 passed, 1 skipped in 51.86s",)))

    assert out["ok"] is True and out["exit_code"] == 0
    assert out["passed"] == 261


def test_a_failing_run_names_the_tests_and_returns_a_tail(tmp_path):
    """AC-4. The name is what the agent's next turn has to reference."""
    root = _repo(tmp_path, None)
    out = verify.run_tests(root, _cfg(root, prints=(
        "FAILED tests/test_a.py::test_boundary_refuses_symlink - AssertionError",
        "FAILED tests/test_b.py::test_turn_budget_releases - AssertionError",
        "2 failed, 231 passed in 3.10s",
    ), exit_code=1))

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

    out = verify.run_tests(root, _cfg(root, prints=("kaboom, in a shape nobody parses",), exit_code=3))

    assert out["ok"] is False and out["exit_code"] == 3
    assert out["passed"] is None and out["failed"] is None
    assert out["failed_tests"] == []
    assert "kaboom" in out["tail"], "the tail is always there for what the parser could not read"


def test_a_long_run_is_truncated_to_a_bounded_tail(tmp_path):
    root = _repo(tmp_path, None)
    out = verify.run_tests(root, _cfg(root, numbered_lines=500, exit_code=1))

    assert out["truncated"] is True
    assert len(out["tail"].splitlines()) == verify.TAIL_LINES
    assert "line 500" in out["tail"], "the tail is the END of the output, where the failure is"


def test_a_hanging_test_command_is_killed_and_says_nothing_is_known(tmp_path):
    """An unattended agent that hangs is worse than one that fails — D6's turn budget cannot
    help if a single turn never returns."""
    root = _repo(tmp_path, None)

    with pytest.raises(ToolError) as exc:
        verify.run_tests(root, _cfg(root, sleep=30), timeout=1)

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


def test_this_repository_declares_how_a_fresh_worktree_is_built():
    """The drift that would put us straight back where GRPH-502 started.

    `[tests].command` names an interpreter inside `backend/.venv`, and `[setup]` is what
    creates it. If somebody moves the venv in one of those and not the other, a fresh worktree
    builds something the test command cannot find — and the symptom is `load` refusing a
    repository that looks perfectly built to a human standing in the primary checkout.

    Asserts the FILE, not the machine: running `uv venv` here would make the suite take a
    minute to prove something a string comparison proves exactly.
    """
    import tomllib

    data = tomllib.loads((REPO / CONFIG_NAME).read_text(encoding="utf-8"))
    commands = data["setup"]["commands"]

    assert isinstance(commands, list), "a list, because D4 means there is no shell"
    assert any("uv venv" in c for c in commands), "nothing here creates the interpreter"

    # The path the test command needs, resolved the way `load` resolves it: relative to
    # `[tests].cwd`. `./.venv/bin/python` under `backend` is `backend/.venv`.
    venv = (Path(data["tests"]["cwd"]) / data["tests"]["command"].split()[0]).parts[:2]
    needed = "/".join(venv)

    # AGAINST THE COMMAND THAT CREATES IT, not the list as a whole. `any(needed in c ...)` was
    # satisfied by the INSTALL line whatever the creating line did, so every divergence the
    # docstring above describes passed: measured, `uv venv` pointed at `backend/venv`,
    # `backend/.venv2` and `.venv` all went green, and the last two do not even share a prefix
    # with the needed path. Deletion failed; divergence did not, and they are different faults.
    #
    # Same bare-`any()`-over-a-collection weakness that has bitten twice in this repository
    # already — GRPH-479 counted a name across a whole file, GRPH-426 matched a domain that
    # appeared elsewhere in the same section. Both were fixed the same way: pin the specific
    # thing, not a substring somewhere nearby.
    creator = next(c for c in commands if "uv venv" in c)
    assert creator.split()[-1] == needed, (
        f"[setup] creates {creator.split()[-1]!r} but [tests].command needs {needed!r} — a "
        "fresh worktree would build an interpreter the test command cannot find, and the "
        "primary checkout would look perfectly fine"
    )

    # The install line names the interpreter INDEPENDENTLY, so it can drift from the creating
    # line too — a tree that builds the venv and then installs into a different one.
    #
    # Only where `--python` names a PATH. `uv venv --python 3.12` uses the same flag for a
    # version selector, and checking that one asserts "3.12 is inside backend/.venv" — which
    # is how this test failed on its first run, against a config that was entirely correct.
    for command in commands:
        parts = command.split()
        if "--python" not in parts:
            continue
        target = parts[parts.index("--python") + 1]
        if "/" not in target:
            continue                      # a version, not an interpreter path
        assert target.startswith(f"{needed}/"), (
            f"a [setup] command installs into {target!r}, which is not inside the {needed!r} "
            "that [tests].command needs"
        )


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


# ---- git_diff and the files write_file makes (GRPH-488) ---------------------------------
#
# `git diff` reports tracked, unstaged changes only. Everything this agent CREATES is
# untracked, so the tool whose description says "Show what you have changed in the worktree
# so far" could not see the output of the tool the agent uses most.
#
# The shipped test modified a committed file, which is the one case a plain diff handles.


def _repo_with_history(tmp_path: Path) -> Path:
    """A worktree with one committed file, so 'tracked' and 'untracked' both exist."""
    import subprocess

    root = _repo(tmp_path, None)
    for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "t@t.io"],
                ["git", "config", "user.name", "t"]):
        subprocess.run(cmd, cwd=root, check=True)
    (root / "existing.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
    return root


def test_git_diff_sees_a_file_the_agent_just_created(tmp_path):
    """THE DEFECT. write_file creates untracked files; a plain `git diff` cannot see them,
    so an agent that had just written three modules was told it had changed nothing."""
    root = _repo_with_history(tmp_path)
    tools.write_file(root, "app/brand_new.py", "def feature():\n    return 42\n")

    out = verify.git_diff(root)

    assert out["empty"] is False, "a newly written file left the diff empty"
    assert "brand_new.py" in out["diff"]


def test_a_new_file_is_not_lost_beside_an_edited_one(tmp_path):
    """THE WORSE CASE, and the reason this is a defect rather than a gap. With both kinds of
    change the answer was no longer empty — it was a confident, well-formed diff that
    silently omitted the new module. Obvious wrongness is survivable; plausible wrongness is
    what gets believed."""
    root = _repo_with_history(tmp_path)
    tools.write_file(root, "app/brand_new.py", "def feature():\n    return 42\n")
    tools.edit_file(root, "existing.py", "x = 1", "x = 2")

    diff = verify.git_diff(root)["diff"]

    assert "existing.py" in diff, "the tracked edit vanished"
    assert "brand_new.py" in diff, "the new file was silently omitted from a diff that looked complete"


def test_git_diff_stages_nothing_for_commit(tmp_path):
    """`git add -N` records intent, not content. If that were ever to become a real `add`,
    the agent would be quietly staging its own work and D9's salvage — which commits the
    dirty worktree onto the child's branch — would start committing a curated subset."""
    import subprocess

    root = _repo_with_history(tmp_path)
    tools.write_file(root, "app/brand_new.py", "x = 1\n")

    verify.git_diff(root)

    staged = subprocess.run(["git", "diff", "--cached", "--name-only"],
                            cwd=root, capture_output=True, text=True).stdout.strip()
    assert staged == "", f"git_diff staged something for commit: {staged!r}"


def test_git_diff_on_a_clean_tree_is_still_empty(tmp_path):
    """The control. Without it, 'always report something' satisfies every assertion above
    and the tool stops being able to say the agent has done nothing yet — which is a real
    answer and a useful one."""
    root = _repo_with_history(tmp_path)

    out = verify.git_diff(root)

    assert out["empty"] is True and out["lines"] == 0


def test_git_diff_outside_a_git_repo_still_answers(tmp_path):
    """The intent-to-add is best-effort. A worktree that is not a repository must not turn
    into a raised error just because the new first step had nothing to talk to."""
    root = _repo(tmp_path, None)

    with pytest.raises(ToolError) as exc:
        verify.git_diff(root)

    assert "git diff failed" in str(exc.value), "the failure should come from the diff, not the add"


# ---- [setup]: building the tree before anything checks it (GRPH-502) -----------------------
#
# FOUND BY THE S7 WALK. `.gbagent.toml` declares `./.venv/bin/python -m pytest -q`, and a fresh
# `git worktree` — what PRD-22 hands every fleet child — has no `backend/.venv`. `load` refused
# a repository that was merely unbuilt, and nothing anywhere built it. The walk symlinked a venv
# in, which was the workaround that named the gap.


def test_a_repository_declaring_no_setup_needs_none(tmp_path):
    """Absent `[setup]` is legal and is not an absence reading as clean: if the tree really did
    need building, `load`'s executable check refuses on the next line."""
    assert config.prepare(_repo(tmp_path, '[tests]\ncommand = "git hi"\n')) == []


def test_setup_commands_run_in_the_worktree(tmp_path):
    root, commands = _setup_repo(tmp_path, {"touch": ("built.txt",)})

    ran = config.prepare(root)

    # Side effect, not a shlex round-trip of prepare's `' '.join(argv)`. That join
    # re-splits `C:\Program Files\...` into two tokens even when setup succeeded
    # (built.txt existed, assertion False) — GRPH-589 bounce.
    assert (root / "built.txt").exists(), "it ran somewhere else"
    assert len(ran) == len(commands)


def test_setup_runs_every_command_in_order(tmp_path):
    root, _ = _setup_repo(tmp_path, {"touch": ("one",)}, {"touch": ("two",)})

    config.prepare(root)

    assert (root / "one").exists() and (root / "two").exists()


def test_a_setup_that_fails_refuses_the_spawn(tmp_path):
    """The whole point. An agent in a tree that did not build cannot verify anything, and
    discovering that at the first `run_tests` costs the run."""
    root, _ = _setup_repo(tmp_path, {"exit_code": 1})

    with pytest.raises(config.SetupFailed) as exc:
        config.prepare(root)

    assert "did not build" in str(exc.value)


def test_a_failing_setup_stops_before_the_commands_after_it(tmp_path):
    """Continuing past a failed build would run the rest against a tree that is not there."""
    root, _ = _setup_repo(tmp_path, {"exit_code": 1}, {"touch": ("after.txt",)})

    with pytest.raises(config.SetupFailed):
        config.prepare(root)

    assert not (root / "after.txt").exists()


def test_a_setup_naming_a_missing_program_says_so(tmp_path):
    root = _repo(tmp_path, '[tests]\ncommand = "git hi"\n'
                           '\n[setup]\ncommands = ["definitely-not-a-real-binary-xyz"]\n')

    with pytest.raises(config.SetupFailed) as exc:
        config.prepare(root)

    assert "not installed here" in str(exc.value)


def test_setup_is_a_list_because_there_is_no_shell(tmp_path):
    """D4: `a && b` as one string would make `&&` a literal argument to `a`."""
    root = _repo(tmp_path, '[tests]\ncommand = "git hi"\n'
                           '\n[setup]\ncommands = "touch one && touch two"\n')

    with pytest.raises(config.SetupFailed) as exc:
        config.prepare(root)

    assert "list" in str(exc.value)


def test_setup_refusal_is_a_ConfigRefused_so_every_caller_already_handles_it(tmp_path):
    """A second exception type would mean every `except ConfigRefused` site had to learn about
    it, and the consequence is identical: no spawn."""
    assert issubclass(config.SetupFailed, ConfigRefused)


def test_prepare_leaves_a_missing_config_for_load_to_refuse(tmp_path):
    """Two refusals for one fault would be two messages to keep honest. `load` names the
    missing file better than `prepare` could."""
    root = _repo(tmp_path, None)

    assert config.prepare(root) == []

    with pytest.raises(ConfigRefused) as exc:
        load(root)

    assert CONFIG_NAME in str(exc.value)


# ---- counts come from the summary line, not from anywhere (GRPH-532) ----------------------


def _counts(out: str) -> tuple[int | None, int | None]:
    from gbagent.verify import _read_counts

    passed, failed, _ = _read_counts(out)
    return passed, failed


def test_a_count_printed_after_the_summary_does_not_become_the_answer():
    """`out` is stdout and stderr CONCATENATED, so every line of stderr lands after every
    line of stdout. Reading counts from anywhere made the last of those the answer.

    A teardown log, a coverage total, or any atexit write matching `N passed` used to win.
    Measured before the fix: this exact input reported 99 passed on a run where three did —
    the "confident, structured and false" answer `_read_counts` exists to refuse.
    """
    assert _counts("=== 1 failed, 3 passed in 0.5s ===\nteardown log: 99 passed\n") == (3, 1)
    assert _counts("=== 2 failed, 10 passed in 1.2s ===\nTOTAL 340 passed\n") == (10, 2)


def test_a_count_printed_before_the_summary_does_not_become_the_answer_either():
    """The other direction, and it is why this anchors on the summary rather than taking the
    FIRST match. Preferring the first would fix the case above and break every runner whose
    summary is not the first count-shaped thing it prints — a warnings block, a previous
    invocation, a captured log. Both ends have to be excluded by the same rule.
    """
    assert _counts("warnings: 77 passed earlier\n=== 1 failed, 3 passed in 0.5s ===\n") == (3, 1)


def test_the_last_summary_wins_when_a_run_prints_more_than_one():
    """A re-run or a nested invocation prints two. The final tally describes this run."""
    out = "=== 5 failed, 1 passed in 0.2s ===\n…retrying…\n=== 0 failed, 6 passed in 0.4s ===\n"
    assert _counts(out) == (6, 0)


def test_a_runner_with_no_summary_line_still_reports_what_it_can():
    """The fallback, and the reason it exists. jest prints `Tests: 3 passed, 3 total` with no
    duration on that line, so requiring a summary would report None for a run that plainly
    passed. Where the old whole-output read was the only option, it is still what happens.
    """
    assert _counts("Tests:       3 passed, 3 total\n") == (3, None)


def test_output_nobody_can_read_is_still_None_rather_than_zero():
    """The property the summary anchoring must not cost. A run whose output means nothing
    reports nothing — `failed: 0` on an unreadable run is the original sin of this module.
    """
    assert _counts("Segmentation fault\n") == (None, None)


def test_the_summary_regex_is_actually_consulted():
    """_SUMMARY was dead code for the whole life of this module — defined, never referenced,
    and the exact regex the fix needed. A test that only checked counts would pass against a
    reimplementation that ignored it again, so this pins the wiring.
    """
    import inspect

    from gbagent import verify

    assert "_SUMMARY" in inspect.getsource(verify._read_counts), (
        "_read_counts no longer consults _SUMMARY — if the anchoring moved, move this with it"
    )


def test_run_tests_hands_concatenated_output_to_read_counts():
    """The CALL, not the callee. The trailing-count tests above drive `_read_counts`
    directly; the agent sees `run_tests`. Independently inlining last-wins whole-output
    parse here left those tests green and shipped the original defect (GRPH-532 bounce).
    """
    import inspect

    src = inspect.getsource(verify.run_tests)
    assert "_read_counts(out)" in src, (
        "run_tests no longer hands concatenated stdout+stderr to _read_counts — the "
        "trailing-count tests call the helper directly and would stay green"
    )


def test_run_tests_does_not_take_counts_from_trailing_stderr(tmp_path):
    """Drive the surface the agent sees: stdout is a pytest summary, stderr is a
    teardown log matching `N passed`. `run_tests` concatenates them, so a last-wins
    parse of the whole output reports 99. The count must come from the summary.
    """
    root = _repo(tmp_path, None)
    script = root / "backend" / "r.py"
    script.write_text(
        "import sys\n"
        "print('=== 1 failed, 3 passed in 0.5s ===', flush=True)\n"
        "sys.stderr.write('teardown log: 99 passed\\n')\n"
        "sys.stderr.flush()\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    cfg = VerifyConfig(
        argv=stub_argv(script), cwd=(root / "backend").resolve(), source="r.py",
    )

    out = verify.run_tests(root, cfg)

    assert out["passed"] == 3 and out["failed"] == 1, (
        f"run_tests reported passed={out['passed']} failed={out['failed']} — trailing "
        "stderr poisoned the count the agent sees. Pinning _read_counts is not enough; "
        "this is the CALL."
    )


def test_a_setup_command_survives_a_space_in_the_interpreter_path(tmp_path):
    """THE CALL (GRPH-589 bounce). The previous version put the space in the SCRIPT
    path and never called prepare. stub_command quotes because [setup] is
    shlex-split, and the commonest Windows Python lives under
    `C:\\Program Files\\...`.
    """
    interp_dir = tmp_path / "Program Files" / "Python"
    interp_dir.mkdir(parents=True)
    # A copy of the interpreter binary fails (rpath / libpython). A wrapper in the
    # spaced directory is argv[0] with a space; it delegates to the real python.
    if os.name == "nt":
        interp = interp_dir / "python.cmd"
        interp.write_text(f'@echo off\r\n"{sys.executable}" %*\r\n', encoding="utf-8")
    else:
        interp = interp_dir / "python"
        interp.write_text(
            f"#!{sys.executable}\nimport runpy, sys\nrunpy.run_path(sys.argv[1])\n",
            encoding="utf-8",
        )
        interp.chmod(interp.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    root = tmp_path / "repo"
    root.mkdir()
    script = make_stub_script(root / "s.py", touch=("made.txt",))
    cmd = stub_command(script, interpreter=interp)
    (root / CONFIG_NAME).write_text(
        '[tests]\ncommand = "git hi"\n\n'
        f"[setup]\ncommands = ['{cmd}']\n",
        encoding="utf-8",
    )

    config.prepare(root)

    assert (root / "made.txt").exists(), (
        "prepare did not run the setup command whose interpreter path contains a space"
    )
