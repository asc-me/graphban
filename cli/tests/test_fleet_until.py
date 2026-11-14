"""GRPH-1656 — the command Triage's drain note shows must start a drain as copied."""
from __future__ import annotations

import shlex

import pytest

from gban import config
from gban.cli import main


@pytest.fixture()
def seen(tmp_path, monkeypatch):
    monkeypatch.setenv(config.HOME_ENV, str(tmp_path))
    monkeypatch.delenv("GBAN_ADAPTER", raising=False)
    monkeypatch.delenv("GBFLEET_API_KEY", raising=False)
    monkeypatch.setattr("gban.cli.doctor_mod.find_supervisor", lambda: "/usr/bin/gbfleet")
    out = {}

    class Done:
        returncode = 0

    monkeypatch.setattr("gban.cli.subprocess.run",
                        lambda argv, **kw: (out.update(argv=argv), Done())[1])
    return out


def test_drain_note_command_reaches_until_with_an_adapter(seen, monkeypatch):
    monkeypatch.setenv("GBAN_ADAPTER", "claude")
    cmd = "gban fleet until --prd P75W-P1"  # the string waits.drain_note emits
    assert main(shlex.split(cmd)[1:]) == 0
    argv = seen["argv"]
    assert argv[1] == "until" and argv[argv.index("--adapter") + 1] == "claude"
    assert "--prd" in argv


def test_adapter_falls_back_to_claude_on_path(seen, monkeypatch):
    monkeypatch.setattr("gban.cli.shutil.which", lambda n: "/bin/claude" if n == "claude" else None)
    assert main(["fleet", "until", "--prd", "X"]) == 0
    assert "claude" in seen["argv"]


def test_explicit_adapter_is_left_alone(seen, monkeypatch):
    monkeypatch.setenv("GBAN_ADAPTER", "claude")
    assert main(["fleet", "until", "--adapter", "grok", "--prd", "X"]) == 0
    assert seen["argv"].count("--adapter") == 1 and "grok" in seen["argv"]


def test_unknown_adapter_names_what_to_set(seen, monkeypatch, capsys):
    monkeypatch.setattr("gban.cli.shutil.which", lambda n: None)
    assert main(["fleet", "until", "--prd", "X"]) != 0
    err = capsys.readouterr().err
    assert "--adapter" in err and "GBAN_ADAPTER" in err
    assert "argv" not in seen
