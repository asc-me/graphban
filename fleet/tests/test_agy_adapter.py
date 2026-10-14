"""Antigravity CLI (`agy`). Facts were read off 1.3.5, 2026-10-13, not recalled."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gbfleet.adapters import ADAPTERS, AdapterError, Tuning, parse_version, resolve
from gbfleet.adapters.agy import EFFORTS
from gbfleet.seat import Seat
from gbfleet.worktree import SEAT_FILES, create
from conftest import make_stub_binary

SEAT = Seat(code="WORKER-AGY1", server_url="https://gb.invalid", api_key="gbk_secret")


def _launch(git_repo: Path, tmp_path: Path, **kwargs):
    tree = create(git_repo, tmp_path / "agy-wt", "wave", "1")
    instruction = tmp_path / "instr"
    instruction.write_text("build it", encoding="utf-8")
    return ADAPTERS["agy"].launch(
        SEAT, tree, instruction, Path("/usr/bin/true"), **kwargs
    ), instruction, tree


def test_the_seat_is_the_workspace_mcp_config_salvage_knows(git_repo: Path, tmp_path: Path):
    launch, _, tree = _launch(git_repo, tmp_path)
    relative = launch.seat_path.resolve().relative_to(tree.path.resolve()).as_posix()
    assert relative == ".agents/mcp_config.json"
    assert relative in SEAT_FILES


def test_the_seat_uses_serverUrl_not_url(git_repo: Path, tmp_path: Path):
    """Measured: `agy mcp add` wrote serverUrl; docs say url/httpUrl are not supported."""
    launch, _, _ = _launch(git_repo, tmp_path)
    graphban = launch.config["mcpServers"]["graphban"]
    assert "url" not in graphban
    assert "httpUrl" not in graphban
    assert graphban["serverUrl"] == "https://gb.invalid/api/mcp"
    assert graphban["headers"]["X-API-Key"] == "gbk_secret"
    assert graphban["disabled"] is False


def test_shared_http_servers_are_rewritten_too(git_repo: Path, tmp_path: Path):
    tree = create(git_repo, tmp_path / "agy-share", "wave", "1")
    instruction = tmp_path / "instr"
    instruction.write_text("x", encoding="utf-8")
    seat = Seat(
        code="WORKER-AGY1", server_url="https://gb.invalid", api_key="gbk_secret",
        shared={"context7": {"type": "http", "url": "https://docs.invalid/mcp",
                             "headers": {"Authorization": "Bearer x"}}},
    )
    launch = ADAPTERS["agy"].launch(seat, tree, instruction, Path("/usr/bin/true"))
    ctx = launch.config["mcpServers"]["context7"]
    assert ctx["serverUrl"] == "https://docs.invalid/mcp"
    assert "url" not in ctx


def test_the_instruction_arrives_on_stdin_and_print_is_not_on_argv(
        git_repo: Path, tmp_path: Path):
    """`--print` takes the next token as the prompt. Measured: it ate `--output-format`."""
    launch, instruction, _ = _launch(git_repo, tmp_path)
    assert launch.stdin_file == instruction
    assert "--print" not in launch.argv
    assert "-p" not in launch.argv
    assert "--dangerously-skip-permissions" in launch.argv
    assert launch.argv[launch.argv.index("--output-format") + 1] == "json"
    joined = " ".join(launch.argv)
    assert SEAT.code not in joined
    assert SEAT.api_key not in joined


def test_debug_is_log_file(git_repo: Path, tmp_path: Path):
    debug = tmp_path / "agy.log"
    launch, _, _ = _launch(git_repo, tmp_path, debug_file=debug)
    assert "--log-file" in launch.argv
    assert launch.argv[launch.argv.index("--log-file") + 1] == str(debug)


def test_result_facts_reads_the_json_object():
    stdout = json.dumps({
        "status": "SUCCESS",
        "response": "hi\n",
        "num_turns": 1,
        "usage": {"input_tokens": 11779, "output_tokens": 69, "thinking_tokens": 68,
                  "total_tokens": 11848},
    })
    facts = ADAPTERS["agy"].result_facts(stdout)
    assert facts == {"tokens_in": 11779, "tokens_out": 69, "turns_used": 1}


def test_result_facts_empty_on_garbage():
    assert ADAPTERS["agy"].result_facts("") == {}
    assert ADAPTERS["agy"].result_facts("not json") == {}
    assert ADAPTERS["agy"].result_facts("{") == {}


def test_an_unknown_effort_is_refused_before_spawn():
    with pytest.raises(AdapterError, match="low, medium, high, xhigh, max"):
        ADAPTERS["agy"].check_tuning(Tuning(effort="bogus"))
    ADAPTERS["agy"].check_tuning(Tuning(effort="high"))
    assert set(EFFORTS) == {"low", "medium", "high", "xhigh", "max"}


def test_version_1_3_5_is_inside_the_range(tmp_path: Path):
    fake = make_stub_binary(tmp_path / "agy", prints="1.3.5")
    found = resolve("agy", binary=fake)
    assert found.version == "1.3.5"
    assert parse_version(found.version) == (1, 3, 5)


def test_a_real_agy_binary_resolves_when_installed():
    import shutil
    binary = shutil.which("agy")
    if binary is None:
        pytest.skip("agy is not installed here")
    found = resolve("agy")
    assert found.adapter.name == "agy"
    assert parse_version(found.version) >= (1, 3)
    known = found.adapter.known_models(found.binary)
    assert known is None or "gemini-3.8-flash-high" in known or len(known) > 0
