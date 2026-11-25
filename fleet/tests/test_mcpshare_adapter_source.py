"""`.gbfleet/servers` is checked against the harness that runs the children (GRPH-1038).

A grok wave used to refuse because `browser` was absent from `~/.claude.json`, while
`~/.grok/config.toml` already defined it. grok merges both files, so a name in either is
granted (the grok stanza winning) and a refusal names both. These tests set HOME to a temp dir and go
through `gbfleet until --adapter`, which is the call that picks the file. A correct
`source_for` that `_shared_servers` never asks would leave every one of them green.
"""
from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

import pytest

from gbfleet import cli
from gbfleet.seat import TOML, Seat, write as write_seat
from gbfleet.until import Report


SERVER = "browser"
FLAG = "docsbox"
GROK_COMMAND = "from-grok-config"
FLAG_URL = "http://docs-from-grok"
CLAUDE_URL = "http://from-claude-json"


def _home(tmp_path: Path, *, grok: str, claude: dict) -> Path:
    home = tmp_path / "home"
    (home / ".grok").mkdir(parents=True)
    (home / ".grok" / "config.toml").write_text(grok, encoding="utf-8")
    (home / ".claude.json").write_text(json.dumps(claude), encoding="utf-8")
    return home


def _repo(tmp_path: Path, names: str) -> Path:
    repo = tmp_path / "repo"
    (repo / ".gbfleet").mkdir(parents=True)
    (repo / ".gbfleet" / "servers").write_text(names, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, check=True)
    return repo


def _wave(monkeypatch, home: Path, repo: Path, adapter: str, extra: list[str] | None = None):
    """`gbfleet until --adapter`, with the vendor binary and the ledger stubbed out.

    The server check runs while the `shared=` argument is evaluated, before `run_until`.
    A refusal is SystemExit(2) and `run_until` is never called — that is "before spawn".
    """
    seen: dict = {"called": False, "shared": None}

    class FakeGB:
        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            pass

    def fake_run(*args, **kwargs):
        seen["called"] = True
        seen["shared"] = kwargs.get("shared")
        return Report(ok=True, reason="idle", exit=0)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("GBFLEET_API_KEY", "k")
    monkeypatch.setattr(cli, "Graphban", FakeGB)
    monkeypatch.setattr(cli, "run_until", fake_run)
    monkeypatch.setattr(cli, "make_adapter_factory", lambda *a, **k: object())

    argv = [
        "until", "--repo", str(repo),
        "--server", "http://gb.invalid", "--adapter", adapter,
        *(extra or []),
    ]
    return seen, argv


def _child_toml(tmp_path: Path, shared: dict) -> dict:
    """The seat file a grok child would load from this grant."""
    seat = Seat(code="C", server_url="http://gb.example", api_key="k", shared=shared)
    path = write_seat(tmp_path / "child" / "config.toml", seat.mcp_config(), TOML)
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_grok_grants_a_server_defined_only_in_the_grok_config(tmp_path, monkeypatch):
    """Sabotage: hard-code the source back to ~/.claude.json for every adapter.
    This test must fail — the name is absent from the Claude file, so the wave refuses
    before run_until, and the child never receives the server."""
    home = _home(
        tmp_path,
        grok=(
            f"[mcp_servers.{SERVER}]\n"
            f'command = "{GROK_COMMAND}"\n'
            'args = ["-y", "browser"]\n'
            "enabled = true\n"
            "\n"
            f"[mcp_servers.{FLAG}]\n"
            f'url = "{FLAG_URL}"\n'
            "enabled = true\n"
        ),
        claude={"mcpServers": {"gmail": {"type": "http", "url": "http://mail"}}},
    )
    repo = _repo(tmp_path, f"{SERVER}\n")
    seen, argv = _wave(monkeypatch, home, repo, "grok", ["--mcp-server", FLAG])

    assert cli.main(argv) == 0
    assert seen["called"], "the wave was refused before spawn"
    assert seen["shared"][SERVER]["command"] == GROK_COMMAND
    assert seen["shared"][FLAG]["url"] == FLAG_URL
    assert "gmail" not in seen["shared"]

    child = _child_toml(tmp_path, seen["shared"])
    assert child["mcp_servers"][SERVER]["command"] == GROK_COMMAND
    assert child["mcp_servers"][SERVER]["enabled"] is True
    assert child["mcp_servers"][FLAG]["url"] == FLAG_URL
    assert "gmail" not in child["mcp_servers"]


def test_grok_grants_a_server_defined_only_in_claude_json(tmp_path, monkeypatch):
    """grok 1.0.25+ merges ~/.claude.json too, and that cannot be switched off. This
    repo's required `context7` lives only there. Sabotage: read only the grok config
    for grok; this test must fail with a refusal before spawn."""
    home = _home(
        tmp_path,
        grok="[mcp_servers.other]\ncommand = \"something-else\"\n",
        claude={"mcpServers": {SERVER: {"type": "http", "url": CLAUDE_URL}}},
    )
    repo = _repo(tmp_path, f"{SERVER}\n")
    seen, argv = _wave(monkeypatch, home, repo, "grok")

    assert cli.main(argv) == 0
    assert seen["called"], "the wave was refused before spawn"
    assert seen["shared"][SERVER]["url"] == CLAUDE_URL
    assert "other" not in seen["shared"]

    child = _child_toml(tmp_path, seen["shared"])
    assert child["mcp_servers"][SERVER]["url"] == CLAUDE_URL


def test_grok_prefers_the_grok_stanza_when_both_files_define_the_name(tmp_path, monkeypatch):
    home = _home(
        tmp_path,
        grok=f"[mcp_servers.{SERVER}]\ncommand = \"{GROK_COMMAND}\"\nenabled = true\n",
        claude={"mcpServers": {SERVER: {"type": "http", "url": CLAUDE_URL}}},
    )
    repo = _repo(tmp_path, f"{SERVER}\n")
    seen, argv = _wave(monkeypatch, home, repo, "grok")

    assert cli.main(argv) == 0
    assert seen["shared"][SERVER] == {"command": GROK_COMMAND}


def test_grok_grants_from_claude_json_when_there_is_no_grok_config(tmp_path, monkeypatch):
    """No ~/.grok/config.toml at all: grok loads nothing from it, and still merges Claude's."""
    home = _home(
        tmp_path, grok="",
        claude={"mcpServers": {SERVER: {"type": "http", "url": CLAUDE_URL}}},
    )
    (home / ".grok" / "config.toml").unlink()
    repo = _repo(tmp_path, f"{SERVER}\n")
    seen, argv = _wave(monkeypatch, home, repo, "grok")

    assert cli.main(argv) == 0
    assert seen["shared"][SERVER]["url"] == CLAUDE_URL


def test_grok_refuses_a_name_in_neither_file_and_names_both(tmp_path, monkeypatch, capsys):
    home = _home(
        tmp_path,
        grok="[mcp_servers.other]\ncommand = \"something-else\"\n",
        claude={"mcpServers": {"gmail": {"type": "http", "url": "http://mail"}}},
    )
    repo = _repo(tmp_path, f"{SERVER}\n")
    seen, argv = _wave(monkeypatch, home, repo, "grok")

    with pytest.raises(SystemExit) as exc:
        cli.main(argv)

    err = capsys.readouterr().err
    assert exc.value.code == 2
    assert seen["called"] is False
    assert str(home / ".grok" / "config.toml") in err
    assert str(home / ".claude.json") in err
    assert SERVER in err
    assert "gmail" not in err and "other" not in err
    assert "Refusing rather than spawning without it" in err


def test_claude_still_refuses_a_name_absent_from_claude_json(tmp_path, monkeypatch, capsys):
    """No behaviour change for claude. The name is present in the grok file, so a check
    that followed the grok path would grant it and this would not refuse."""
    home = _home(
        tmp_path,
        grok=f"[mcp_servers.{SERVER}]\ncommand = \"{GROK_COMMAND}\"\n",
        claude={"mcpServers": {"gmail": {"type": "http", "url": "http://mail"}}},
    )
    repo = _repo(tmp_path, f"{SERVER}\n")
    seen, argv = _wave(monkeypatch, home, repo, "claude")
    claude_path = str(home / ".claude.json")

    with pytest.raises(SystemExit) as exc:
        cli.main(argv)

    err = capsys.readouterr().err
    assert exc.value.code == 2
    assert seen["called"] is False
    assert claude_path in err
    assert SERVER in err
    assert "not in" in err
    assert "Refusing rather than spawning without it" in err
    assert str(home / ".grok" / "config.toml") not in err


def test_an_adapter_with_no_known_config_still_reads_claude_json(tmp_path, monkeypatch):
    """Today's file, for a harness this module does not know. Absent from the grok
    config on purpose: switching every adapter to that file would grant nothing here."""
    home = _home(
        tmp_path,
        grok="[mcp_servers.other]\ncommand = \"not-browser\"\n",
        claude={"mcpServers": {SERVER: {"type": "http", "url": CLAUDE_URL}}},
    )
    repo = _repo(tmp_path, f"{SERVER}\n")
    seen, argv = _wave(monkeypatch, home, repo, "qwen-code")

    assert cli.main(argv) == 0
    assert seen["called"] is True
    assert seen["shared"][SERVER]["url"] == CLAUDE_URL
