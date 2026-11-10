"""Machine reports from `gban doctor` / `gban setup` (GRPH-1442)."""
from __future__ import annotations

import pytest

from gban import config, doctor, machine
from gban.cli import main
from gban.client import NoSession, Unreachable


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(config.HOME_ENV, str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_PATH", str(tmp_path / "claude.json"))
    monkeypatch.setenv("GROK_CONFIG_PATH", str(tmp_path / "grok" / "config.toml"))
    monkeypatch.chdir(tmp_path)
    for var in (config.URL_ENV, config.PROJECT_ENV, config.API_KEY_ENV, doctor.SUPERVISOR_KEY_ENV):
        monkeypatch.delenv(var, raising=False)
    config.save_settings(url="http://gb.invalid", project="core")
    return tmp_path


class _Rec:
    def __init__(self):
        self.calls = []

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        return {}


def _doctor(monkeypatch, lines, code, extra=()):
    monkeypatch.setattr("gban.cli.doctor_mod.run", lambda *a, **k: (list(lines), code))
    monkeypatch.setattr("gban.cli.doctor_mod.resolve_supervisor_key", lambda *a, **k: "")
    # The hourly pack poll is its own line (test_playbooks); these tests are about the report.
    monkeypatch.setattr("gban.cli._poll", lambda args: None)
    return main(["doctor", *extra])


def test_machine_id_is_stable_and_wiping_home_makes_a_new_one(home):
    first = machine.machine_id()
    assert first == machine.machine_id()
    (home / machine.MACHINE_FILE).unlink()
    assert machine.machine_id() != first


def test_doctor_posts_its_lines_with_the_machine_id(home, monkeypatch):
    rec = _Rec()
    monkeypatch.setattr("gban.machine.authenticated", lambda url, act="": rec)
    code = _doctor(monkeypatch, [doctor._line("ledger", "PASS", "server", "ok"),
                                 doctor._line("local", "FAIL", "gbfleet", "bad")], 1)
    assert code == 1
    (method, path, body), = rec.calls
    assert (method, path) == ("POST", "/api/machines/report")
    assert body["machine_id"] == machine.machine_id() and body["project"] == "core"
    assert [l["status"] for l in body["lines"]] == ["PASS", "FAIL"]


def test_no_report_sends_nothing(home, monkeypatch):
    rec = _Rec()
    monkeypatch.setattr("gban.machine.authenticated", lambda url, act="": rec)
    assert _doctor(monkeypatch, [doctor._line("ledger", "PASS", "server")], 0, ["--no-report"]) == 0
    assert rec.calls == []


@pytest.mark.parametrize("error", [Unreachable("could not reach http://gb.invalid: down"),
                                   NoSession("no stored session")])
def test_an_undeliverable_report_prints_unknown_and_keeps_the_exit_code(home, monkeypatch, capsys, error):
    """Sabotage: block the server. The command still exits on its own checks."""
    def boom(url, act=""):
        raise error
    monkeypatch.setattr("gban.machine.authenticated", boom)
    code = _doctor(monkeypatch, [doctor._line("ledger", "PASS", "server")], 0)
    out = capsys.readouterr().out
    assert code == 0
    assert "UNKNOWN" in out and "could not report to http://gb.invalid" in out
    code = _doctor(monkeypatch, [doctor._line("ledger", "FAIL", "session")], 1)
    assert code == 1  # the report's own failure never turns a FAIL into success


def test_undelivered_line_is_unknown_never_pass(home):
    line = machine._unreported("http://s", "why")
    assert line["status"] == "UNKNOWN" and line["detail"] == "could not report to http://s: why"
