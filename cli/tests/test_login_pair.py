"""`gban login --pair` signs in without a password prompt or a tty (GRPH-1366)."""
from __future__ import annotations

import pytest

from gban import cli as cli_mod, config
from gban.cli import main
from gban.client import Refused


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(config.HOME_ENV, str(tmp_path))
    for var in (config.URL_ENV, config.PROJECT_ENV, config.API_KEY_ENV):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli_mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(cli_mod.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(cli_mod, "authenticated", lambda url, act="": type(
        "C", (), {"call": lambda self, *a, **k: []})())
    return tmp_path


START = {"device_code": "dev", "user_code": "ABCD-EFGH", "expires_in": 600, "interval": 0}


def test_pair_prints_code_then_stores_the_approvers_session(home, monkeypatch, capsys):
    answers = iter([{"status": "pending"},
                    {"status": "approved", "refresh_token": "r", "access_token": "a",
                     "email": "approver@example.com"}])
    monkeypatch.setattr(cli_mod, "pair_start", lambda url: START)
    monkeypatch.setattr(cli_mod, "pair_poll", lambda url, dev: next(answers))
    assert main(["--server", "http://gb.invalid", "login", "--pair"]) == 0
    err = capsys.readouterr().err
    assert "ABCD-EFGH" in err
    assert config.session()["refresh_token"] == "r"
    assert config.session()["user"] == "approver@example.com"


def test_pair_refused_code_stores_nothing(home, monkeypatch):
    monkeypatch.setattr(cli_mod, "pair_start", lambda url: START)

    def boom(url, dev):
        raise Refused(400, "expired")
    monkeypatch.setattr(cli_mod, "pair_poll", boom)
    assert main(["--server", "http://gb.invalid", "login", "--pair"]) == 1
    assert not config.session().get("refresh_token")
