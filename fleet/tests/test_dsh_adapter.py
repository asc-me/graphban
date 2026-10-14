"""DeepSeek Harness (`dsh`). Facts were read off 0.2.0-rc.2, 2026-10-14, not recalled."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gbfleet.adapters import ADAPTERS, parse_version, resolve
from gbfleet.adapters.dsh import DEFAULT_MODEL, DEFAULT_PROVIDER, PERMISSION_MODE
from gbfleet.seat import Seat, YAML, UnrenderableSeat, write
from gbfleet.worktree import SEAT_FILES, create
from conftest import make_stub_binary

SEAT = Seat(code="WORKER-DSH1", server_url="https://gb.invalid", api_key="gbk_secret")


def _launch(git_repo: Path, tmp_path: Path, **kwargs):
    tree = create(git_repo, tmp_path / "dsh-wt", "wave", "1")
    instruction = tmp_path / "instr"
    instruction.write_text("build it", encoding="utf-8")
    return ADAPTERS["dsh"].launch(
        SEAT, tree, instruction, Path("/usr/bin/true"), **kwargs
    ), instruction, tree


def test_the_seat_is_a_private_temp_file_salvage_does_not_need(git_repo: Path, tmp_path: Path):
    launch, _, tree = _launch(git_repo, tmp_path)
    assert launch.seat_format == YAML
    assert launch.seat_path.suffix == ".yml"
    try:
        launch.seat_path.resolve().relative_to(tree.path.resolve())
    except ValueError:
        pass
    else:
        pytest.fail("dsh seat is inside the worktree; it would need a SEAT_FILES entry")
    assert not any("dsh" in entry for entry in SEAT_FILES)


def test_the_overlay_is_cordis_yaml_not_mcp_json(git_repo: Path, tmp_path: Path):
    """Measured: dump-config composed `--patch` as @deepseek-ai/dsh-mcp-client with url."""
    launch, _, _ = _launch(git_repo, tmp_path)
    path = write(tmp_path / "seat.yml", launch.config, YAML)
    text = path.read_text(encoding="utf-8")
    assert "mcpServers" not in text
    assert "name: '@deepseek-ai/dsh-mcp-client'" in text
    assert "transport: streamable-http" in text
    assert 'url: "https://gb.invalid/api/mcp"' in text
    assert '"X-API-Key": "gbk_secret"' in text
    assert "failOnStartupError: true" in text
    assert "serverName: graphban" in text
    assert f'provider: "{DEFAULT_PROVIDER}"' in text
    assert f'model: "{DEFAULT_MODEL}"' in text


def test_a_named_model_goes_in_the_overlay_not_on_argv(git_repo: Path, tmp_path: Path):
    """`--model` is unknown option. Measured: the overlay's agent-default-model is the flag."""
    launch, _, _ = _launch(git_repo, tmp_path, model="deepseek-flash")
    assert "--model" not in launch.argv
    assert launch.config["model"] == "deepseek-flash"
    assert launch.config["provider"] == DEFAULT_PROVIDER
    assert launch.model == "deepseek-flash"


def test_omitting_the_model_still_overlays_the_account_provider(
        git_repo: Path, tmp_path: Path):
    """Headless default deepseek-official exited 1 MISSING_CREDENTIAL. Desktop login works."""
    launch, _, _ = _launch(git_repo, tmp_path)
    assert launch.model == ""
    assert launch.config["provider"] == DEFAULT_PROVIDER
    assert launch.config["model"] == DEFAULT_MODEL
    assert "--model" not in launch.argv


def test_shared_http_servers_become_insert_entries(git_repo: Path, tmp_path: Path):
    tree = create(git_repo, tmp_path / "dsh-share", "wave", "1")
    instruction = tmp_path / "instr"
    instruction.write_text("x", encoding="utf-8")
    seat = Seat(
        code="WORKER-DSH1", server_url="https://gb.invalid", api_key="gbk_secret",
        shared={"context7": {"type": "http", "url": "https://docs.invalid/mcp",
                             "headers": {"Authorization": "Bearer x"}}},
    )
    launch = ADAPTERS["dsh"].launch(seat, tree, instruction, Path("/usr/bin/true"))
    text = write(tmp_path / "share.yml", launch.config, YAML).read_text(encoding="utf-8")
    assert "serverName: context7" in text
    assert 'url: "https://docs.invalid/mcp"' in text
    assert "id: mcp-context7" in text


def test_the_instruction_arrives_on_stdin_not_argv(git_repo: Path, tmp_path: Path):
    launch, instruction, _ = _launch(git_repo, tmp_path)
    assert launch.stdin_file == instruction
    assert "--print" not in launch.argv
    assert launch.argv[1:3] == ["--profile", "headless"]
    assert "--patch" in launch.argv
    assert "--json" in launch.argv
    joined = " ".join(launch.argv)
    assert SEAT.code not in joined
    assert SEAT.api_key not in joined
    assert launch.env["DSH_PERMISSION_MODE"] == PERMISSION_MODE


def test_result_facts_reads_step_end_usage():
    stdout = "\n".join([
        json.dumps({"type": "session", "sessionId": "session-1", "cwd": "/tmp"}),
        json.dumps({"type": "status", "phase": "turn_start", "turn": 1}),
        json.dumps({"type": "status", "phase": "step_start", "turn": 1, "step": 1}),
        json.dumps({"type": "text", "text": "pong"}),
        json.dumps({
            "type": "status", "phase": "step_end", "turn": 1, "step": 1,
            "usage": {"inputTokens": 6338, "outputTokens": 3, "totalTokens": 6341},
        }),
        json.dumps({"type": "status", "phase": "turn_end", "turn": 1,
                    "reason": {"kind": "completed"}}),
        json.dumps({"type": "final", "text": "pong"}),
    ])
    facts = ADAPTERS["dsh"].result_facts(stdout)
    assert facts == {"tokens_in": 6338, "tokens_out": 3, "turns_used": 1}


def test_result_facts_does_not_invent_zero_when_usage_is_absent():
    """MISSING_CREDENTIAL step_end had no usage key. Zero would read as a free run."""
    stdout = json.dumps({"type": "status", "phase": "step_end", "turn": 1, "step": 1})
    facts = ADAPTERS["dsh"].result_facts(stdout)
    assert "tokens_in" not in facts
    assert "tokens_out" not in facts
    assert facts == {"turns_used": 1}


def test_result_facts_empty_on_garbage():
    assert ADAPTERS["dsh"].result_facts("") == {}
    assert ADAPTERS["dsh"].result_facts("not json") == {}
    assert ADAPTERS["dsh"].result_facts("{") == {}


def test_a_credential_with_a_quote_in_it_is_escaped(tmp_path: Path):
    nasty = Seat(
        code="c",
        server_url='https://example.test/a"b\\c',
        api_key='gb_sk_"quote\\back',
    )
    config = ADAPTERS["dsh"].seat_config(nasty)
    path = write(tmp_path / "nasty.yml", config, YAML)
    text = path.read_text(encoding="utf-8")
    assert r'url: "https://example.test/a\"b\\c/api/mcp"' in text
    assert r'"X-API-Key": "gb_sk_\"quote\\back"' in text


def test_an_unknown_overlay_field_refuses_rather_than_vanishing(tmp_path: Path):
    config = SEAT.mcp_config()
    config["oauth"] = {"client_id": "x"}
    with pytest.raises(UnrenderableSeat, match="oauth"):
        write(tmp_path / "seat.yml", config, YAML)


def test_version_0_2_0_rc_2_is_inside_the_range(tmp_path: Path):
    fake = make_stub_binary(tmp_path / "dsh", prints="0.2.0-rc.2")
    found = resolve("dsh", binary=fake)
    assert found.version == "0.2.0-rc.2"
    assert parse_version(found.version) == (0, 2, 0)


def test_a_real_dsh_binary_resolves_when_installed():
    import shutil
    binary = shutil.which("dsh")
    app = Path(
        "/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh"
    )
    path = binary or (str(app) if app.is_file() else None)
    if path is None:
        pytest.skip("dsh is not installed here")
    found = resolve("dsh", binary=path)
    assert found.adapter.name == "dsh"
    assert parse_version(found.version) >= (0, 2)
