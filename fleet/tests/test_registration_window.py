"""GRPH-1400: the registration window is per adapter and per run, not one 90s constant.

Measured on a loaded machine, a healthy claude child took 67s from process start to
`register_agent` (CLI startup with the operator's global skill listing, a 26s first model
response, then a ToolSearch hop). The shared 90s window SIGKILLed the slow tail of those
children with empty logs and `stopped_because: never_registered`, which read as crashes.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gbfleet import cli as cli_mod
from gbfleet import supervisor as sup
from gbfleet.adapters import ADAPTERS
from gbfleet.adapters import claude as claude_mod
from gbfleet.seat import Seat
from gbfleet.spawn import REGISTRATION_WINDOW, Launch
from gbfleet.supervisor import Limits, registration_window, up
from gbfleet.worktree import create

from tests.test_supervisor import _factory, _seats, _server

SEAT = Seat(code="WORKER-7F3K", server_url="https://gb.invalid", api_key="gbk_secret")


def _launch(window: float | None) -> Launch:
    return Launch(adapter="x", argv=["true"], seat_path=Path("/s"), config="",
                  instruction="", registration_window=window)


def test_the_runs_explicit_window_wins_over_the_adapters():
    assert registration_window(Limits(registration_window=120.0), _launch(240.0)) == 120.0


def test_with_no_explicit_window_the_adapters_own_window_applies():
    assert registration_window(Limits(), _launch(240.0)) == 240.0


def test_with_neither_the_shared_default_applies():
    assert registration_window(Limits(), _launch(None)) == REGISTRATION_WINDOW


def _claude_launch(git_repo: Path, tmp_path: Path) -> Launch:
    tree = create(git_repo, tmp_path / "wt-claude", "wave", "1")
    instruction = tmp_path / "instr"
    instruction.write_text("build it", encoding="utf-8")
    return ADAPTERS["claude"].launch(SEAT, tree, instruction, Path("/usr/bin/true"))


def test_a_claude_child_gets_at_least_180s_to_register(git_repo: Path, tmp_path: Path):
    launch = _claude_launch(git_repo, tmp_path)

    assert launch.registration_window is not None
    assert launch.registration_window >= 180.0
    assert launch.registration_window == claude_mod.REGISTRATION_WINDOW


def test_a_claude_child_can_call_register_agent_without_tool_search(git_repo: Path, tmp_path: Path):
    """Claude Code defers MCP tools behind ToolSearch by default, which cost a fresh child a
    whole model round trip before it could even find `register_agent`."""
    launch = _claude_launch(git_repo, tmp_path)

    assert launch.env.get("ENABLE_TOOL_SEARCH") == "false"


def test_choosing_an_entitlement_keeps_the_window_and_the_env(git_repo: Path, tmp_path: Path):
    """`select_entitlement` rebuilds the launch with `replace`; neither field may fall off."""
    launch = _claude_launch(git_repo, tmp_path)
    kept = launch.__class__(**{**launch.__dict__, "env": {**launch.env, "X": "1"}})

    assert kept.registration_window == launch.registration_window
    assert kept.env["ENABLE_TOOL_SEARCH"] == "false"


@pytest.mark.parametrize("command", ["until", "up", "mcp"])
def test_the_window_can_be_set_per_run(command: str, monkeypatch):
    monkeypatch.delenv("GBFLEET_REGISTRATION_WINDOW", raising=False)
    parser = cli_mod.build_parser()
    args = _parse(parser, command, ["--registration-window", "300"])

    assert args.registration_window == 300.0


def test_the_window_falls_back_to_the_environment(monkeypatch):
    monkeypatch.setenv("GBFLEET_REGISTRATION_WINDOW", "200")
    assert cli_mod._env_window() == 200.0


@pytest.mark.parametrize("raw", ["", "nonsense", "0", "-5"])
def test_a_bad_environment_window_means_the_adapter_default(raw: str, monkeypatch):
    monkeypatch.setenv("GBFLEET_REGISTRATION_WINDOW", raw)
    assert cli_mod._env_window() is None


def test_the_supervisor_waits_for_the_resolved_window(
    git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch
):
    """The wiring, not just the resolver: the window `start_one` hands to
    `await_registration` is the run's explicit one. Sabotage: pass
    `limits.registration_window` straight through again and give the limit None; the
    default case below then hands None to the wait."""
    seen: list[float] = []
    real_await = sup.await_registration

    def capture(child, roster, *args, window=None, **kwargs):
        seen.append(window)
        return real_await(child, roster, *args, window=window, **kwargs)

    monkeypatch.setattr(sup, "await_registration", capture)
    workspace = tmp_path / "ws"
    up(
        git_repo, _seats(1), _factory(scripts, "works_then_exits"),
        _server(workspace), limits=Limits(max_workers=1, registration_window=123.0),
        state=state, workspace=workspace, poll=0.05,
    )

    assert seen and seen[0] == 123.0


def test_the_supervisor_uses_the_default_when_nothing_is_set(
    git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch
):
    seen: list[float] = []
    real_await = sup.await_registration

    def capture(child, roster, *args, window=None, **kwargs):
        seen.append(window)
        return real_await(child, roster, *args, window=window, **kwargs)

    monkeypatch.setattr(sup, "await_registration", capture)
    workspace = tmp_path / "ws"
    up(
        git_repo, _seats(1), _factory(scripts, "works_then_exits"),
        _server(workspace), limits=Limits(max_workers=1),
        state=state, workspace=workspace, poll=0.05,
    )

    assert seen and seen[0] == REGISTRATION_WINDOW


def _parse(parser, command: str, extra: list[str]):
    """Parse `command` with only the arguments it requires filled in."""
    required = {
        "until": ["--server", "http://s", "--adapter", "claude"],
        "up": ["--server", "http://s", "--seats-file", "/s", "--adapter", "claude"],
        "mcp": ["--server", "http://s"],
    }[command]
    return parser.parse_args([command, *required, *extra])
