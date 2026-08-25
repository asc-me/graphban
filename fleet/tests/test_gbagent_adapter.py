"""S5 — the first adapter that is ours (PRD-24 D8, AC-10).

Being first-party buys better flags, not authority. So most of this is the same set of
questions asked of `claude` and `grok` — does a credential reach argv, is the seat somewhere
salvage can commit, does an unsupported knob get refused rather than dropped — and the answers
have to be as good or better, not exempt.

The part that is genuinely different is the version. An exact pin catches the one mismatch that
can actually happen to a binary shipping in this same wheel: a `gbagent` from another install.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import gbfleet
from gbagent import cli, loop
from gbagent.cli import SeatUnreadable, read_seat
from gbfleet.adapters import (
    ADAPTERS,
    TuningUnsupported,
    Tuning,
    parse_version,
    resolve,
)
from gbfleet.seat import Seat
from gbfleet.worktree import SEAT_FILES, create

GBAGENT = ADAPTERS["gbagent"]
BINARY = Path(sys.executable).parent / "gbagent"


@pytest.fixture()
def tree(git_repo, tmp_path):
    return create(git_repo, tmp_path / "w1", "wave", "1")


@pytest.fixture()
def seat():
    return Seat(code="PLANNER-ABC123", server_url="https://graphban.example",
                api_key="gbk_live_secret")


def _launch(tree, seat, tmp_path, model: str = "", tuning: Tuning | None = None):
    instruction = tmp_path / "instruction.txt"
    instruction.write_text("register with PLANNER-ABC123\n")
    return GBAGENT.launch(seat, tree, instruction, BINARY, model=model, tuning=tuning)


# ---- the same questions the other adapters answer ---------------------------------------


def test_no_credential_reaches_argv(tree, seat, tmp_path):
    """argv is readable by every process on the machine. Declining to sandbox (PRD-22 D-k)
    is a different thing from publishing a live seat to `ps`."""
    launch = _launch(tree, seat, tmp_path)

    joined = " ".join(launch.argv)
    assert seat.api_key not in joined
    assert seat.code not in joined


def test_the_instruction_reaches_the_child_by_path(tree, seat, tmp_path):
    """It carries the enrolment code, so it goes by path — no argv, and no stdin pipe to
    manage either, because this is ours and could simply take a flag."""
    launch = _launch(tree, seat, tmp_path)

    assert "--instruction-file" in launch.argv
    assert launch.stdin_file is None


def test_the_seat_stays_out_of_the_worktree(tree, seat, tmp_path):
    """A credential that never enters the project directory cannot be committed by salvage,
    cannot show up in `git status`, and needs no entry in SEAT_FILES."""
    launch = _launch(tree, seat, tmp_path)

    with pytest.raises(ValueError):
        Path(launch.seat_path).resolve().relative_to(tree.path.resolve())


def test_no_seat_file_entry_was_needed_for_it():
    """The other half of the claim above, asserted rather than assumed: if the seat were in
    the worktree and SEAT_FILES did not know, salvage would commit a live credential."""
    assert not any("gbagent" in entry for entry in SEAT_FILES)


def test_the_model_is_carried_not_chosen(tree, seat, tmp_path):
    plain = _launch(tree, seat, tmp_path)
    named = _launch(tree, seat, tmp_path, model="qwen3-coder:30b")

    assert "--model" not in plain.argv, "the default path is byte-identical to no model"
    assert named.argv[named.argv.index("--model") + 1] == "qwen3-coder:30b"
    assert named.model == "qwen3-coder:30b", "carried on the record, not just on argv"


def test_the_budget_and_the_window_are_passed_through(tree, seat, tmp_path):
    launch = _launch(tree, seat, tmp_path, tuning=Tuning(turns="40", window="262144"))

    assert launch.argv[launch.argv.index("--turns") + 1] == "40"
    assert launch.argv[launch.argv.index("--window") + 1] == "262144"


def test_a_knob_this_adapter_does_not_have_is_refused_by_name():
    """Refused rather than ignored, for the reason `TuningUnsupported` exists: a caller must
    not be able to believe it asked for something it did not get."""
    with pytest.raises(TuningUnsupported) as exc:
        resolve("gbagent", binary=BINARY, tuning=Tuning(effort="high"))

    assert "effort" in str(exc.value)


def test_the_other_adapters_refuse_gbagents_knobs_too(tmp_path):
    """The new fields must not become knobs every vendor silently accepts. This is the half
    that would have broken quietly when `Tuning` grew.

    A stand-in binary reporting a version claude accepts, because `resolve` checks the
    version FIRST — handing it the real gbagent binary tests the version gate instead, which
    is what the first draft of this did.
    """
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\necho '2.1.233 (Claude Code)'\n", encoding="utf-8")
    fake.chmod(0o755)

    with pytest.raises(TuningUnsupported) as exc:
        resolve("claude", binary=fake, tuning=Tuning(turns="40"))

    assert "turns" in str(exc.value)


def test_a_new_tuning_field_is_covered_without_anyone_remembering():
    """`named()` reads the dataclass rather than a hand-written list. The list was written
    out twice when there were two fields, which is one place to forget a third — and
    forgetting means an unsupported knob is silently dropped instead of refused."""
    assert Tuning(turns="40").named() == {"turns"}
    assert Tuning(window="1").named() == {"window"}
    assert Tuning().named() == set()


# ---- the version is a pin ----------------------------------------------------------------


def test_the_pin_is_read_from_the_package_not_written_out():
    """A literal would refuse the next release the moment somebody bumped one file and not
    the other."""
    assert GBAGENT.support.exact == parse_version(gbfleet.__version__)
    assert GBAGENT.support.maximum is None


def test_the_matrix_calls_it_a_pin_rather_than_a_range():
    """AC-10. The matrix is what somebody reads before installing, and 'exactly 0.1.0' and
    '0.1.0 or newer' are different promises."""
    matrix = (Path(__file__).resolve().parents[2] / "docs" / "fleet-adapters.md").read_text()

    assert "exactly" in GBAGENT.support.describe()
    assert "a pin, not a range" in matrix


def test_a_range_adapter_still_describes_a_range():
    """The exact pin must not have changed what the other three say about themselves."""
    assert "exactly" not in ADAPTERS["claude"].support.describe()
    assert ADAPTERS["claude"].support.permits((2, 1, 233))
    assert not ADAPTERS["claude"].support.permits((3, 0))


# ---- exit codes ---------------------------------------------------------------------------


def test_exit_meaning_comes_from_the_loop_so_there_is_one_definition():
    """Two copies of what 75 means is one too many, and the supervisor is the one that would
    read the stale one."""
    for code in (0, 75, 70, 1):
        assert GBAGENT.exit_meaning(code) == loop.exit_meaning(code)


def test_the_supervisor_can_tell_surrender_from_a_crash():
    """AC-7's other half, at the surface the supervisor actually reads."""
    assert "stuck" in GBAGENT.exit_meaning(75)
    assert "released" in GBAGENT.exit_meaning(75)
    assert "crashed" in GBAGENT.exit_meaning(1)
    assert GBAGENT.exit_meaning(0) == "finished"


def test_the_other_adapters_still_say_nothing_they_cannot_know():
    """A vendor that never documented an exit code must not inherit ours."""
    assert "stuck" not in ADAPTERS["grok"].exit_meaning(75)


# ---- the binary the adapter resolves ------------------------------------------------------


def test_the_console_script_reports_the_version_the_pin_expects():
    result = subprocess.run([str(BINARY), "--version"], capture_output=True, text=True,
                            timeout=30)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"gbagent {gbfleet.__version__}"


def test_models_cannot_be_asked_when_no_endpoint_is_configured(monkeypatch):
    """None and an empty set are different answers. Refusing every model because we could
    not look would break a working setup over a missing environment variable."""
    monkeypatch.delenv("GBAGENT_BASE_URL", raising=False)

    assert GBAGENT.known_models(BINARY) is None


def test_a_bare_invocation_says_what_is_wrong(tmp_path):
    result = subprocess.run([str(BINARY)], capture_output=True, text=True, timeout=30)

    assert result.returncode != 0
    assert "no command given" in result.stderr


def test_run_refuses_rather_than_starting_with_no_item(tmp_path, monkeypatch):
    """A child that starts, achieves nothing and exits 0 is the failure PRD-22 keeps naming.
    This one names the slice that owns the gap instead."""
    (tmp_path / "backend").mkdir()
    (tmp_path / ".gbagent.toml").write_text('[tests]\ncommand = "echo hi"\n')

    result = subprocess.run(
        [str(BINARY), "run", "--worktree", str(tmp_path), "--mcp-config", "/nope.json",
         "--model", "m", "--turns", "5", "--window", "1000",
         "--base-url", "http://model.invalid/v1"],
        capture_output=True, text=True, timeout=60,
    )

    assert result.returncode == 78
    assert "cannot claim its own work yet" in result.stderr
    assert "GRPH-492" in result.stderr, "say which slice owns it"


def test_run_refuses_a_repository_it_cannot_verify(tmp_path):
    """D3, reaching the CLI: no `.gbagent.toml`, no spawn."""
    result = subprocess.run(
        [str(BINARY), "run", "--worktree", str(tmp_path), "--mcp-config", "/nope.json",
         "--model", "m", "--turns", "5", "--window", "1000",
         "--base-url", "http://model.invalid/v1"],
        capture_output=True, text=True, timeout=60,
    )

    assert result.returncode == 78
    assert ".gbagent.toml" in result.stderr


@pytest.mark.parametrize("omitted", ["--turns", "--window", "--model", "--worktree"])
def test_the_things_the_loop_refuses_to_guess_have_no_defaults_here_either(tmp_path, omitted):
    """`loop.run` refuses to guess the budget or the window. A CLI default puts the guess back.

    Each flag is omitted ON ITS OWN and the error must name THAT flag as required. Omitting
    both and asserting the names appear in stderr is what this did first, and it passed with
    a default in place — argparse prints the usage line on any error, and the usage line
    contains every flag whether it is required or not.
    """
    argv = {"--worktree": str(tmp_path), "--mcp-config": "/nope.json", "--model": "m",
            "--turns": "5", "--window": "1000"}
    argv.pop(omitted)
    flat = [item for pair in argv.items() for item in pair]

    result = subprocess.run([str(BINARY), "run", *flat], capture_output=True, text=True,
                            timeout=60)

    assert result.returncode == 2
    required = [l for l in result.stderr.splitlines() if "required" in l]
    assert required, f"argparse did not report a missing argument for {omitted}"
    assert omitted in required[0], f"{omitted} is not required: {required[0]}"


# ---- reading the seat the supervisor wrote ------------------------------------------------


def test_the_seat_file_is_read_back_into_a_base_url_and_a_key(tmp_path, seat):
    """`mcp_config` writes the /api/mcp endpoint and `Graphban` appends it again, so the
    suffix comes off here. Left on, every call would go to /api/mcp/api/mcp."""
    path = tmp_path / "seat.json"
    path.write_text(json.dumps(seat.mcp_config()))

    base_url, api_key = read_seat(path)

    assert base_url == "https://graphban.example"
    assert api_key == "gbk_live_secret"


@pytest.mark.parametrize("body", [
    "not json at all",
    '{"mcpServers": {}}',
    '{"mcpServers": {"graphban": {"url": "https://x"}}}',
    '{"mcpServers": {"graphban": {"url": "", "headers": {"X-API-Key": "k"}}}}',
])
def test_an_unusable_seat_refuses_before_a_single_turn_is_spent(tmp_path, body):
    """An agent that starts, cannot reach the server, and burns 40 turns discovering it is
    the expensive shape of the mistake `config.load` refuses at spawn."""
    path = tmp_path / "seat.json"
    path.write_text(body)

    with pytest.raises(SeatUnreadable):
        read_seat(path)


def test_the_system_prompt_tells_it_what_it_cannot_do(tmp_path):
    """Weak on its own — a prompt is the weakest guard there is — but a model that does not
    know there is no shell spends turns discovering it."""
    assert "worktree root" in cli.SYSTEM
    assert "no shell" in cli.SYSTEM
