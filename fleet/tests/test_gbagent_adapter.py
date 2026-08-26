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


def test_with_no_item_the_model_is_told_to_claim_one(tmp_path):
    """AC-5. Through S5 this refused with exit 78, because `claim_next` was not in
    `WORKER_TOOLS` and an agent that starts, achieves nothing and exits 0 is the failure
    PRD-22 keeps naming. S7 wired the claim, so the refusal became the instruction."""
    from gbagent.cli import assignment_for

    claim = assignment_for("")

    assert "claim_next" in claim
    assert "wait_seconds=0" in claim, "waiting is how a worker becomes an idle process"
    assert "not a failure" in claim, "D-c: exiting on empty is the normal end of a run"


def test_with_an_item_the_model_is_told_not_to_claim_anything_else(tmp_path):
    from gbagent.cli import assignment_for

    assert "GRPH-1" in assignment_for("GRPH-1")
    assert "claim_next" not in assignment_for("GRPH-1")


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


# ---- registering, which is how a child gets onto the roster at all (GRPH-503) --------------
#
# PRD-24 G4 says gbagent "spawns as an adapter under PRD-22's supervisor". It could not:
# there was no `register_agent` call anywhere in the package. `await_registration` polls the
# roster for an agent whose worktree matches and kills the child when none appears, blaming
# the ADAPTER — the misattribution PRD-22 S2 exists to prevent. Every walk invoked the CLI
# directly with --agent-id, so the argv was verified and the thing the argv is FOR was not.


def test_the_enrolment_code_is_read_out_of_the_instruction_the_supervisor_wrote():
    from gbfleet.seat import Seat, instruction_for

    seat = Seat(code="WORKER-7F3K2Q", server_url="https://x", api_key="k")
    written = instruction_for(seat, Path("/tmp/wt"), "wave/1")

    assert cli.enrolment_code(written) == "WORKER-7F3K2Q"


def test_the_parse_is_pinned_against_the_instruction_it_parses():
    """THE DRIFT THAT WOULD BE INVISIBLE. `seat.INSTRUCTION` is prose, and reword it and this
    regex silently stops matching — producing a child that never registers, which looks
    exactly like a broken adapter. A test that only checked a hand-written sample string
    would keep passing through that change."""
    from gbfleet import seat as seat_mod

    assert "enrolment_code=" in seat_mod.INSTRUCTION, (
        "the instruction no longer names the code the way cli.ENROLMENT looks for it"
    )
    rendered = seat_mod.INSTRUCTION.format(code="ABC-123", worktree="/w", branch="b")
    assert cli.enrolment_code(rendered) == "ABC-123"


def test_an_instruction_with_no_code_yields_nothing_rather_than_a_guess():
    assert cli.enrolment_code("just do the work") == ""
    assert cli.enrolment_code("") == ""


def test_the_adapter_passes_the_branch_so_the_roster_row_names_the_diff(tree, seat, tmp_path):
    """`await_registration` matches on worktree; the branch goes with it because a roster row
    naming neither cannot answer "where is the work"."""
    launch = _launch(tree, seat, tmp_path)

    assert launch.argv[launch.argv.index("--branch") + 1] == tree.branch


def test_register_agent_is_in_the_worker_set_and_grants_nothing():
    """It gets the child onto the roster. The SEAT decides the role, and the server refuses
    whatever the credential may not do regardless of what this set says."""
    from gbagent.coord import WORKER_TOOLS

    assert "register_agent" in WORKER_TOOLS
    assert not {"sign_off", "bounce", "mint_enrolment", "assign_role"} & WORKER_TOOLS


def test_a_run_with_no_code_and_no_agent_id_refuses_rather_than_going_unregistered(tmp_path):
    """An unregistered child is one the supervisor kills for looking like a broken adapter.
    Refusing here names which it actually was."""
    (tmp_path / "backend").mkdir()
    (tmp_path / ".gbagent.toml").write_text('[tests]\ncommand = "echo hi"\n')
    (tmp_path / "instr.txt").write_text("no seat in here at all\n")
    seat_file = tmp_path / "seat.json"
    seat_file.write_text(json.dumps({"mcpServers": {"graphban": {
        "url": "https://graphban.invalid/api/mcp", "headers": {"X-API-Key": "k"}}}}))

    result = subprocess.run(
        [str(BINARY), "run", "--worktree", str(tmp_path), "--mcp-config", str(seat_file),
         "--instruction-file", str(tmp_path / "instr.txt"),
         "--model", "m", "--turns", "5", "--window", "1000",
         "--base-url", "http://model.invalid/v1"],
        capture_output=True, text=True, timeout=60,
    )

    assert result.returncode == 78
    assert "no enrolment code" in result.stderr
    assert "broken adapter" in result.stderr, "say which failure this actually is"


def _client(handler):
    import httpx
    from gbagent.coord import WORKER_TOOLS
    from gbfleet.client import Graphban
    return Graphban("http://graphban.invalid", "k", allowed=WORKER_TOOLS,
                    transport=httpx.MockTransport(handler))


def test_the_id_the_server_returned_is_the_one_the_run_uses():
    """The wiring, not the call. A helper that built its own connection could only be checked
    by reading the source, which is how a registered id gets quietly ignored."""
    import httpx
    sent: list = []

    def handler(request):
        body = json.loads(request.content)
        sent.append(body["params"]["arguments"])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
            "structuredContent": {"agent_id": "GRPH-A99", "active_role": "worker"}}})

    agent_id, role = cli.register(_client(handler), code="WORKER-1", model="qwen",
                                  worktree="/w", branch="wave/1")

    assert agent_id == "GRPH-A99" and role == "worker"
    assert sent[0]["enrolment_code"] == "WORKER-1"
    assert sent[0]["worktree"] == "/w", "await_registration matches on this"
    assert sent[0]["branch"] == "wave/1"
    assert sent[0]["capabilities"]["vendor"] == "gbagent", "vendor drives review diversity"
    assert sent[0]["capabilities"]["model"] == "qwen"


def test_a_registration_the_server_refuses_is_not_papered_over():
    import httpx

    def handler(request):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {
            "isError": True,
            "structuredContent": {"error": {"code": "conflict", "message": "seat spent"}}}})

    with pytest.raises(cli.NotRegistered) as exc:
        cli.register(_client(handler), code="WORKER-1", model="m", worktree="/w", branch="b")

    assert "seat spent" in str(exc.value)


def test_a_registration_returning_no_id_is_refused_rather_than_used_empty():
    """An empty agent_id would reach `claim_next` as a caller nobody can identify."""
    import httpx

    def handler(request):
        body = json.loads(request.content)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                         "result": {"structuredContent": {"active_role": "worker"}}})

    with pytest.raises(cli.NotRegistered):
        cli.register(_client(handler), code="W", model="m", worktree="/w", branch="b")


def test_the_registration_sentence_is_not_given_to_the_model():
    """FOUND BY THE FIRST SUPERVISOR-SPAWNED BUILD, and it cost that run everything.

    `spawn` writes one instruction for every adapter, opening with "Register with
    `register_agent`". A vendor harness registers by being prompted to; gbagent registers in
    `_run` before the model exists, and `register_agent` is deliberately not advertised. So the
    model's first instruction was to call a tool it does not have. Thirty turns, nothing
    claimed.
    """
    from gbfleet.seat import Seat, instruction_for

    written = instruction_for(Seat(code="W-1", server_url="https://x", api_key="k"),
                              Path("/w"), "b")

    task = cli.task_from(written)

    assert "register_agent" not in task
    assert "W-1" not in task, "the seat is spent; it has no business in the model's context"


def test_everything_else_in_the_instruction_survives():
    """The rest is exactly right for this agent, and dropping it would lose D-b and D-c."""
    from gbfleet.seat import Seat, instruction_for

    task = cli.task_from(instruction_for(
        Seat(code="W-1", server_url="https://x", api_key="k"), Path("/w"), "b"))

    assert "SEPARATE PROCESS" in task, "D-b: it must not declare parentage"
    assert "parent_agent_id" in task
    assert "EXIT when there is nothing to claim" in task, "D-c: exiting on empty is normal"


def test_the_code_is_still_read_from_what_the_supervisor_WROTE():
    """The strip happens for the model, not for the harness — read the code from the original
    or the child registers with nothing."""
    from gbfleet.seat import Seat, instruction_for

    written = instruction_for(Seat(code="W-XYZ", server_url="https://x", api_key="k"),
                              Path("/w"), "b")

    assert cli.enrolment_code(written) == "W-XYZ"
    assert cli.enrolment_code(cli.task_from(written)) == "", "stripped, as it should be"


def test_the_cli_actually_passes_a_trace_to_the_loop():
    """THE WIRING, and it is the part that silently did not happen.

    `_trace` existed, `loop.run` accepted a `trace`, every unit test passed — and the spawned
    child still wrote no trace, because the call site was never updated. A helper nobody calls
    is indistinguishable from one that does not exist, and only a real spawn showed it.
    """
    import inspect

    source = inspect.getsource(cli._run)

    assert "trace=_trace" in source, "loop.run is called without a trace"
