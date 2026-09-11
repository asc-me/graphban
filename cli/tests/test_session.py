"""PRD-40 PR 1 — the package, the session, and the config precedence (criteria 1-5, 10, 11)."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from gban import config
from gban.cli import main
from gban.client import (EXIT_NO_SESSION, EXIT_REFUSED, EXIT_UNREACHABLE, NoSession,
                       Refused, Unreachable)


@pytest.fixture()
def tty(monkeypatch):
    """A stdin that claims to be a terminal. `gban login` refuses without one, so a test about
    anything else has to say which side of that it is on."""
    monkeypatch.setattr("sys.stdin", _Tty(""))


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(config.HOME_ENV, str(tmp_path))
    for var in (config.URL_ENV, config.PROJECT_ENV, config.API_KEY_ENV):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


# ---- 2, 3: the session file ------------------------------------------------------------------

def test_login_stores_only_the_refresh_token_and_stores_it_privately(home, tty, monkeypatch, capsys):
    """2. Sabotage: store the access token too and this fails — a file that outlives its own
    expiry is a credential nobody remembers leaving there."""
    monkeypatch.setattr("gban.cli.login", lambda url, email, password: {
        "access_token": "ACCESS-DO-NOT-STORE", "refresh_token": "REFRESH-1"})
    monkeypatch.setattr("gban.cli.getpass.getpass", lambda *_: "hunter2")

    assert main(["--server", "http://gb.invalid", "login", "--email", "a@b.c"]) == 0

    path = home / config.SESSION_FILE
    stored = json.loads(path.read_text())
    assert stored["refresh_token"] == "REFRESH-1"
    assert "ACCESS-DO-NOT-STORE" not in path.read_text()
    assert config.is_private(path)
    # And the token never reaches the terminal either.
    assert "REFRESH-1" not in capsys.readouterr().out


def test_the_settings_file_is_written_and_the_second_run_needs_no_flags(home, tty, monkeypatch):
    """2. A person types `--server` once."""
    monkeypatch.setattr("gban.cli.login", lambda url, email, password: {"refresh_token": "R"})
    monkeypatch.setattr("gban.cli.getpass.getpass", lambda *_: "pw")
    main(["--server", "http://gb.invalid", "--project", "core", "login", "--email", "a@b.c"])

    assert json.loads((home / config.SETTINGS_FILE).read_text()) == {
        "project": "core", "url": "http://gb.invalid"}
    assert config.resolve(None, config.URL_ENV, "url") == "http://gb.invalid"


def test_gb_never_opens_graphbans_config_file(home, monkeypatch):
    """10 / D10. The grill's amendment, asserted by FILENAME rather than promised about which
    keys get parsed. Sabotage: read `config.json` for a fallback url and this fails."""
    (home / config.NOT_OURS).write_text(json.dumps(
        {"url": "http://wrong.invalid", "db_url": "postgresql://user:secret@db/graphban"}))

    opened: list[str] = []
    real = Path.read_text

    def watch(self, *a, **kw):
        opened.append(self.name)
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", watch)
    assert config.resolve(None, config.URL_ENV, "url") == ""
    assert config.NOT_OURS not in opened, f"gban read {config.NOT_OURS}"


def test_logout_tells_the_server_and_removes_the_file(home, monkeypatch, capsys):
    """3."""
    config.save_session("R")
    config.save_settings(url="http://gb.invalid")
    called: list[tuple] = []

    class Fake:
        def call(self, method, path, body=None):
            called.append((method, path))
            return {}

    monkeypatch.setattr("gban.cli.authenticated", lambda url: Fake())
    assert main(["logout"]) == 0
    assert called == [("POST", "/api/auth/logout")]
    assert not (home / config.SESSION_FILE).exists()


def test_logout_with_no_network_still_removes_the_file_and_says_what_it_did_not_do(
        home, monkeypatch, capsys):
    """3 / D3. A person who typed logout, saw an error and left a live token on disk is the
    outcome worth avoiding. Sabotage: keep the file when the server cannot be told."""
    config.save_session("R")
    config.save_settings(url="http://gb.invalid")

    def boom(url):
        raise Unreachable("could not reach http://gb.invalid: no route to host")

    monkeypatch.setattr("gban.cli.authenticated", boom)
    assert main(["logout"]) == 0
    assert not (home / config.SESSION_FILE).exists(), "a live token was left on disk"
    err = capsys.readouterr().out
    assert "the server was not told" in err
    assert "stays valid until it expires" in err


def test_logging_out_of_an_already_dead_session_is_not_an_error(home, monkeypatch):
    """3. The person asked to be logged out and they are."""
    config.save_session("R")
    config.save_settings(url="http://gb.invalid")

    def dead(url):
        raise NoSession("refresh token revoked")

    monkeypatch.setattr("gban.cli.authenticated", dead)
    assert main(["logout"]) == 0
    assert not (home / config.SESSION_FILE).exists()


# ---- 4: the three failures are told apart by structure, not prose ----------------------------

def test_an_expired_session_says_what_to_do_and_exits_three(home, monkeypatch, capsys):
    """4. Sabotage: let the 401 through and a person gets a traceback instead of one
    instruction."""
    config.save_settings(url="http://gb.invalid")

    def dead(url):
        raise NoSession("refresh token revoked")

    monkeypatch.setattr("gban.cli.authenticated", dead)
    assert main(["whoami"]) == EXIT_NO_SESSION
    assert "session expired, run `gban login`" in capsys.readouterr().err


def test_an_unreachable_server_is_a_different_exit_code_from_an_expired_session(
        home, monkeypatch, capsys):
    """4. The two send a reader to different places, so they are different codes."""
    config.save_settings(url="http://gb.invalid")

    def gone(url):
        raise Unreachable("could not reach http://gb.invalid: no route to host")

    monkeypatch.setattr("gban.cli.authenticated", gone)
    assert main(["whoami"]) == EXIT_UNREACHABLE
    assert "could not reach" in capsys.readouterr().err


def test_a_refusal_is_printed_in_the_servers_own_words_with_its_hint(home, monkeypatch, capsys):
    """4 / D8. Sabotage: re-word the refusal and this fails — the client would then hold a
    second copy of a rule the server already enforces."""
    config.save_settings(url="http://gb.invalid")

    def refused(url):
        raise Refused(409, "GB-A1's credential permits only 'worker'",
                      "mint a role-narrowed credential in the Fleet view")

    monkeypatch.setattr("gban.cli.authenticated", refused)
    assert main(["whoami"]) == 1
    err = capsys.readouterr().err
    assert "credential permits only 'worker'" in err
    assert "mint a role-narrowed credential" in err


# ---- 10: --json is the machine-readable path -------------------------------------------------

def test_json_output_is_parsed_and_carries_no_human_rendering(home, tty, monkeypatch, capsys):
    """10. No automated assertion reads the human format anywhere, which is what keeps it free
    to improve."""
    monkeypatch.setattr("gban.cli.login", lambda url, email, password: {"refresh_token": "R"})
    monkeypatch.setattr("gban.cli.getpass.getpass", lambda *_: "pw")
    main(["--server", "http://gb.invalid", "--json", "login", "--email", "a@b.c"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["server"] == "http://gb.invalid" and payload["user"] == "a@b.c"
    assert "signed in to" not in json.dumps(payload), "the human rendering leaked into --json"


# ---- 11: the credential appears in no output -------------------------------------------------

def test_no_command_prints_a_token(home, monkeypatch, capsys):
    """11. Sabotage: echo the session on login and this fails."""
    monkeypatch.setattr("gban.cli.login", lambda url, email, password: {
        "access_token": "AAA-SECRET", "refresh_token": "RRR-SECRET"})
    monkeypatch.setattr("gban.cli.getpass.getpass", lambda *_: "pw")
    main(["--server", "http://gb.invalid", "login", "--email", "a@b.c"])
    main(["--server", "http://gb.invalid", "--json", "login", "--email", "a@b.c"])
    seen = capsys.readouterr()
    for secret in ("AAA-SECRET", "RRR-SECRET", "pw"):
        assert secret not in seen.out and secret not in seen.err


# ---- config precedence (D10) -----------------------------------------------------------------

def test_precedence_is_flag_then_environment_then_file(home, monkeypatch):
    config.save_settings(url="http://from-file.invalid")
    assert config.resolve(None, config.URL_ENV, "url") == "http://from-file.invalid"
    monkeypatch.setenv(config.URL_ENV, "http://from-env.invalid")
    assert config.resolve(None, config.URL_ENV, "url") == "http://from-env.invalid"
    assert config.resolve("http://from-flag.invalid", config.URL_ENV, "url") \
        == "http://from-flag.invalid"


# ---- D3: the session file is written by `login` and by nothing else --------------------------

def test_authenticating_does_not_rewrite_the_session_file(home, monkeypatch):
    """D3. `/api/auth/refresh` does NOT consume the token presented — validity keys on
    `token_version` — so writing the new one back buys nothing and manufactures a race between
    concurrent invocations that does not otherwise exist.

    Sabotage: save the returned refresh token in `authenticated()` and this fails.
    """
    from gban import client as client_mod

    config.save_settings(url="http://gb.invalid")
    config.save_session("REFRESH-ORIGINAL")
    before = (home / config.SESSION_FILE).read_text()

    monkeypatch.setattr(client_mod.Client, "call",
                        lambda self, method, path, body=None: {
                            "access_token": "ACCESS-NEW", "refresh_token": "REFRESH-ROTATED"})
    session = client_mod.authenticated("http://gb.invalid")

    assert session.token == "ACCESS-NEW"
    after = (home / config.SESSION_FILE).read_text()
    assert after == before, "authenticating rewrote the session file"
    assert "REFRESH-ROTATED" not in after


def test_two_invocations_can_authenticate_from_the_same_stored_token(home, monkeypatch):
    """The property that makes the above safe: the presented token stays valid, so a second
    process is not locked out by the first."""
    from gban import client as client_mod

    config.save_session("REFRESH-ORIGINAL")
    seen: list[dict] = []

    def call(self, method, path, body=None):
        seen.append(body or {})
        return {"access_token": f"ACCESS-{len(seen)}"}

    monkeypatch.setattr(client_mod.Client, "call", call)
    first = client_mod.authenticated("http://gb.invalid")
    second = client_mod.authenticated("http://gb.invalid")

    assert first.token != second.token
    assert [b.get("refresh_token") for b in seen] == ["REFRESH-ORIGINAL", "REFRESH-ORIGINAL"]


class _Tty(io.StringIO):
    """Stdin that claims to be a terminal, so the refusal above is not what is under test."""

    def isatty(self) -> bool:
        return True


# ---- what the deployed walk found (criterion 13) ----------------------------------------------

def test_login_refuses_outright_when_there_is_no_terminal(home, monkeypatch, capsys):
    """`getpass` falls back to a plain ECHOING read when it cannot turn echo off, and warns
    about it — a warning that arrives after the person has decided to type. The walk hit this
    through a command runner with no tty and got `Warning: Password input may be echoed`
    followed by a traceback. Refusing is the only safe branch.

    Sabotage: let it prompt anyway and this fails."""
    monkeypatch.setattr("sys.stdin", io.StringIO("secret\n"))
    asked = []
    monkeypatch.setattr("gban.cli.getpass.getpass", lambda *a: asked.append(a) or "secret")
    monkeypatch.setattr("gban.cli.login", lambda *a: asked.append("posted") or {})

    assert main(["--server", "http://gb.invalid", "login",
                 "--email", "alex@example.com"]) == EXIT_REFUSED
    assert asked == [], "it must not reach the prompt, let alone the server"
    err = capsys.readouterr().err
    assert "needs a terminal" in err and "echo" in err


@pytest.mark.parametrize("ending", [EOFError, KeyboardInterrupt])
def test_a_cancelled_login_is_one_line_and_not_a_traceback(home, monkeypatch, capsys, ending):
    """Criterion 4's rule, applied to the other end of the session. Input running out is what
    the walk actually hit — under a tty that lies, or a terminal closing mid-prompt — and
    ctrl-C is what people do on purpose. A stack trace answers neither."""
    monkeypatch.setattr("sys.stdin", _Tty("secret\n"))

    def interrupted(*_args):
        raise ending

    monkeypatch.setattr("gban.cli.getpass.getpass", interrupted)
    assert main(["--server", "http://gb.invalid", "login",
                 "--email", "alex@example.com"]) == EXIT_REFUSED
    assert "cancelled" in capsys.readouterr().err
