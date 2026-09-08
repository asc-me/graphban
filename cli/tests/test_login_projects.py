"""`gban login` learns which projects exist, so `gban setup` has something to enable (GRPH-791).

Login stored whatever project the person typed, or nothing — and `setup` needs a project id
before it can mint a project-scoped key. Asking the deployment is the only way to know, and it
is one call the person has already earned by authenticating.

The interesting case is the LAST one here. Discovery is a convenience bolted onto an act that
already succeeded, so a projects call that fails must not fail the login: sending somebody back
to re-type a password the server already accepted is a worse outcome than an unset default.
"""
from __future__ import annotations

import json

import pytest

from gban import cli as cli_mod, config
from gban.cli import main
from gban.client import Refused, Unreachable


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(config.HOME_ENV, str(tmp_path))
    for var in (config.URL_ENV, config.PROJECT_ENV, config.API_KEY_ENV):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


class Projects:
    def __init__(self, rows=None, error=None):
        self.rows, self.error, self.calls = rows or [], error, []

    def call(self, method, path, body=None):
        self.calls.append((method, path))
        if self.error is not None:
            raise self.error
        return self.rows


def _login(monkeypatch, rows=None, error=None, argv=None):
    server = Projects(rows, error)
    monkeypatch.setattr(cli_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_: "alex@example.com")
    monkeypatch.setattr(cli_mod.getpass, "getpass", lambda *_: "secret")
    monkeypatch.setattr(cli_mod, "login",
                        lambda *a, **k: {"refresh_token": "r", "access_token": "a"})
    monkeypatch.setattr(cli_mod, "authenticated", lambda url, act="": server)
    code = main(argv or ["--server", "http://gb.invalid", "login"])
    return code, server


def test_one_project_becomes_the_default_without_being_asked(home, monkeypatch, capsys):
    code, _ = _login(monkeypatch, rows=[{"id": "core", "name": "Core"}])

    assert code == 0
    assert config.settings()["project"] == "core"
    assert "core" in capsys.readouterr().out


def test_several_projects_are_listed_and_none_is_chosen(home, monkeypatch, capsys):
    """Picking the first of several is a coin toss nobody sees the result of until a key
    lands in the wrong project."""
    code, _ = _login(monkeypatch, rows=[{"id": "core", "name": "Core"},
                                        {"id": "super-arc", "name": "Super Arc"}])

    assert code == 0
    assert not config.settings().get("project")
    out = capsys.readouterr().out
    assert "core" in out and "super-arc" in out
    assert "--project" in out


def test_an_explicit_project_is_never_overridden(home, monkeypatch):
    """Naming one says the thing discovery exists to guess, so discovery does not run."""
    code, server = _login(monkeypatch, rows=[{"id": "core", "name": "Core"}],
                          argv=["--server", "http://gb.invalid", "--project", "mine", "login"])

    assert code == 0
    assert config.settings()["project"] == "mine"
    assert server.calls == [], "asked the server about projects after being told one"


def test_no_projects_says_so_rather_than_leaving_a_blank(home, monkeypatch, capsys):
    code, _ = _login(monkeypatch, rows=[])

    assert code == 0
    assert "no projects" in capsys.readouterr().out.lower()


@pytest.mark.parametrize("error", [Unreachable("server down"),
                                   Refused(500, "internal", "")])
def test_a_failed_project_list_does_not_fail_the_login(home, monkeypatch, capsys, error):
    """THE ONE THAT MATTERS. The password was accepted; the session is stored. Reporting a
    non-zero exit here sends somebody to re-enter a credential that already worked."""
    code, _ = _login(monkeypatch, error=error)

    assert code == 0
    assert config.session()["refresh_token"] == "r", "the session was not stored"
    out = capsys.readouterr().out
    assert "warning" in out.lower()
    assert "--project" in out


def test_json_carries_the_projects_it_saw(home, monkeypatch, capsys):
    code, _ = _login(monkeypatch, rows=[{"id": "a", "name": "A"}, {"id": "b", "name": "B"}],
                     argv=["--server", "http://gb.invalid", "--json", "login"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["projects"] == ["a", "b"]
    assert payload["project"] == ""
