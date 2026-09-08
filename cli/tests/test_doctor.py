"""PRD-40 PR 2 — `gban doctor` and the `gban fleet` pass-through (criteria 6, 7)."""
from __future__ import annotations

import json

import pytest

from gban import config, doctor
from gban.cli import main
from gban.client import EXIT_NO_SUPERVISOR, NoSession, Refused, Unreachable


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(config.HOME_ENV, str(tmp_path))
    for var in (config.URL_ENV, config.PROJECT_ENV, config.API_KEY_ENV):
        monkeypatch.delenv(var, raising=False)
    config.save_settings(url="http://gb.invalid", project="core")
    return tmp_path


class _Server:
    """A ledger that answers, or refuses, whatever the test needs."""

    def __init__(self, fleet=None, me=None, error=None):
        self.fleet = fleet or {"agents": []}
        self.me = me or {"email": "alex@example.com"}
        self.error = error

    def call(self, method, path, body=None):
        if self.error is not None:
            raise self.error
        return self.me if path.startswith("/api/auth/me") else self.fleet


def _lines(monkeypatch, server, *, gbfleet=None):
    monkeypatch.setattr("gban.doctor.authenticated", lambda url: server)
    monkeypatch.setattr("gban.doctor.shutil.which", lambda name: gbfleet)
    if gbfleet:
        class Done:
            returncode, stdout, stderr = 0, "PASS  repo  clean", ""
        monkeypatch.setattr("gban.doctor.subprocess.run", lambda *a, **kw: Done())
    return doctor.run("http://gb.invalid", "core", "")


# ---- 6: both halves, and neither silences the other ------------------------------------------

def test_every_line_says_which_side_it_came_from(home, monkeypatch):
    """6. A line that does not say where it came from sends a reader to the wrong machine."""
    lines, code = _lines(monkeypatch, _Server(), gbfleet="/usr/bin/gbfleet")
    assert {l["side"] for l in lines} == {"ledger", "local"}
    assert code == 0


def test_the_ledger_half_is_reported_even_with_no_gbfleet_installed(home, monkeypatch):
    """6. Sabotage: return early when `gbfleet` is missing and the half that needs no
    supervisor disappears with it."""
    lines, code = _lines(monkeypatch, _Server(), gbfleet=None)
    ledger = [l for l in lines if l["side"] == "ledger"]
    assert any(l["status"] == "PASS" for l in ledger)
    local = [l for l in lines if l["side"] == "local"]
    assert local and local[0]["status"] == "UNKNOWN"
    assert "not installed here" in local[0]["detail"]
    # UNKNOWN is not a failure: a laptop without the supervisor is not a broken setup.
    assert code == 0


def test_an_unreachable_server_is_unknown_not_a_pass_and_not_a_fail(home, monkeypatch):
    """6. 'We could not tell' must never render as 'nothing is wrong'."""
    server = _Server(error=Unreachable("could not reach http://gb.invalid: no route to host"))
    monkeypatch.setattr("gban.doctor.authenticated",
                        lambda url: (_ for _ in ()).throw(
                            Unreachable("could not reach http://gb.invalid")))
    monkeypatch.setattr("gban.doctor.shutil.which", lambda name: None)
    lines, code = doctor.run("http://gb.invalid", "core", "")
    assert [l["status"] for l in lines if l["side"] == "ledger"] == ["UNKNOWN"]
    assert code == 0


def test_the_three_ledger_failures_are_three_different_lines(home, monkeypatch):
    """6 / D7. They send a reader to three different places."""
    monkeypatch.setattr("gban.doctor.shutil.which", lambda name: None)

    monkeypatch.setattr("gban.doctor.authenticated",
                        lambda url: (_ for _ in ()).throw(NoSession("no stored session")))
    expired, _ = doctor.run("http://gb.invalid", "core", "")
    assert expired[0]["name"] == "session" and "gban login" in expired[0]["detail"]

    monkeypatch.setattr("gban.doctor.authenticated",
                        lambda url: _Server(error=Refused(404, "project not found")))
    refused, code = doctor.run("http://gb.invalid", "core", "")
    assert code == 1
    assert any(l["name"] == "session" and l["status"] == "FAIL" for l in refused)


def test_a_quarantined_or_refused_agent_is_named(home, monkeypatch):
    """6. The two states that are the whole reason a person runs this — and WHICH agent,
    because 'one agent is quarantined' and 'which one' are different amounts of help."""
    fleet = {"agents": [
        {"id": "SA-A4", "state": "idle", "last_refusal": {
            "tool": "mint_enrolment", "count": 2,
            "reason": "mint_enrolment requires role 'planner'; SA-A4 is registered as 'worker'"}},
        {"id": "SA-A9", "state": "quarantined"},
    ]}
    lines, code = _lines(monkeypatch, _Server(fleet=fleet), gbfleet=None)
    text = doctor.render(lines)
    assert "SA-A4" in text and "refused mint_enrolment x2" in text
    assert "SA-A9" in text and "quarantined" in text
    assert code == 1, "an agent being refused every call is a FAIL, not a note"


def test_the_exit_code_is_the_worst_finding_across_both_halves(home, monkeypatch):
    """6. Never whichever half ran last. Sabotage: take the local half's code and a ledger
    failure reports success."""
    fleet = {"agents": [{"id": "A1", "state": "quarantined"}]}
    lines, code = _lines(monkeypatch, _Server(fleet=fleet), gbfleet="/usr/bin/gbfleet")
    assert [l["status"] for l in lines if l["side"] == "local"] == ["PASS"]
    assert code == 1


# ---- 7: the pass-through ---------------------------------------------------------------------

def test_gb_fleet_returns_the_supervisors_exit_code_unchanged(home, monkeypatch):
    """7 / D5. 75 is stuck, 69 an unreachable endpoint, 55 a spent budget. Sabotage: map a
    non-zero onto one 'pass-through failed' code and this fails."""
    seen = {}

    class Done:
        returncode = 75

    monkeypatch.setattr("gban.cli.shutil.which", lambda name: "/usr/bin/gbfleet")
    monkeypatch.setattr("gban.cli.subprocess.run",
                        lambda argv, **kw: (seen.update(argv=argv), Done())[1])
    assert main(["fleet", "up", "--adapter", "claude"]) == 75
    assert seen["argv"][:3] == ["/usr/bin/gbfleet", "up", "--adapter"]


def test_gb_fleet_fills_in_the_server_and_project_it_already_knows(home, monkeypatch):
    seen = {}

    class Done:
        returncode = 0

    monkeypatch.setattr("gban.cli.shutil.which", lambda name: "/usr/bin/gbfleet")
    monkeypatch.setattr("gban.cli.subprocess.run",
                        lambda argv, **kw: (seen.update(argv=argv), Done())[1])
    assert main(["fleet", "ps"]) == 0
    assert "--server" in seen["argv"] and "http://gb.invalid" in seen["argv"]
    assert "--project" in seen["argv"] and "core" in seen["argv"]


def test_an_explicit_server_is_not_overridden(home, monkeypatch):
    seen = {}

    class Done:
        returncode = 0

    monkeypatch.setattr("gban.cli.shutil.which", lambda name: "/usr/bin/gbfleet")
    monkeypatch.setattr("gban.cli.subprocess.run",
                        lambda argv, **kw: (seen.update(argv=argv), Done())[1])
    main(["fleet", "ps", "--server", "http://other.invalid"])
    assert seen["argv"].count("--server") == 1
    assert "http://gb.invalid" not in seen["argv"]


def test_a_missing_supervisor_says_how_to_install_it(home, monkeypatch, capsys):
    """7. Exit 4, its own code, so it cannot be confused with something gbfleet said.

    Patches `find_supervisor` rather than `shutil.which`: the lookup now also checks beside
    this interpreter, and a test that stubbed only one half would pass while the other found
    a real gbfleet on the machine running it."""
    monkeypatch.setattr("gban.cli.doctor_mod.find_supervisor", lambda: "")
    assert main(["fleet", "ps"]) == EXIT_NO_SUPERVISOR
    assert doctor.INSTALL_SUPERVISOR in capsys.readouterr().err


def test_the_credential_reaches_gbfleet_in_the_environment_never_argv(home, monkeypatch):
    """D5. `ps` shows argv to every process on the machine."""
    seen = {}
    monkeypatch.setenv(config.API_KEY_ENV, "gb_sk_SECRET")
    monkeypatch.setattr("gban.doctor.shutil.which", lambda name: "/usr/bin/gbfleet")

    class Done:
        returncode, stdout, stderr = 0, "", ""

    def run(argv, **kw):
        seen["argv"], seen["env"] = argv, kw.get("env") or {}
        return Done()

    monkeypatch.setattr("gban.doctor.subprocess.run", run)
    monkeypatch.setattr("gban.doctor.authenticated", lambda url: _Server())
    doctor.run("http://gb.invalid", "core", "gb_sk_SECRET")

    assert "gb_sk_SECRET" not in " ".join(seen["argv"])
    assert seen["env"].get("GBFLEET_API_KEY") == "gb_sk_SECRET"


def test_doctor_json_is_parsed_and_the_human_rendering_is_absent(home, monkeypatch, capsys):
    monkeypatch.setattr("gban.doctor.authenticated", lambda url: _Server())
    monkeypatch.setattr("gban.doctor.shutil.which", lambda name: None)
    main(["--json", "doctor"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert all("side" in line for line in payload["lines"])


# ---- what the deployed walk found (criterion 13) ----------------------------------------------

def test_the_local_summary_line_says_what_happened_not_the_childs_banner(home, monkeypatch):
    """The walk read `FAIL local gbfleet  gbfleet 0.1.0 doctor` — a version number standing
    where the reason belongs. The child's report is a REPORT, in its own field; promoting
    some line of it into `detail` would be parsing the supervisor's output, which is the one
    thing D5 is careful not to do."""
    class Done:
        returncode, stdout, stderr = 1, "gbfleet 0.1.0 doctor\n\n  [FAIL] api key — unset\n", ""

    monkeypatch.setattr("gban.doctor.shutil.which", lambda name: "/usr/bin/gbfleet")
    monkeypatch.setattr("gban.doctor.subprocess.run", lambda *a, **kw: Done())
    monkeypatch.setattr("gban.doctor.authenticated", lambda url: _Server())
    lines, code = doctor.run("http://gb.invalid", "core", "")

    local_line = [l for l in lines if l["side"] == "local"][0]
    assert "0.1.0" not in local_line["detail"], "the child's banner is not this line's reason"
    assert "exited 1" in local_line["detail"]
    assert "[FAIL] api key" in local_line["report"], "the report is kept, in its own field"
    assert code == 1


def test_the_childs_report_is_indented_under_its_summary(home, monkeypatch):
    """A reader scanning the summary column must be able to skip a page of somebody else's
    report without losing the two lines they came for."""
    class Done:
        returncode, stdout, stderr = 0, "gbfleet 0.1.0 doctor\n  [PASS] repository\n", ""

    monkeypatch.setattr("gban.doctor.shutil.which", lambda name: "/usr/bin/gbfleet")
    monkeypatch.setattr("gban.doctor.subprocess.run", lambda *a, **kw: Done())
    monkeypatch.setattr("gban.doctor.authenticated", lambda url: _Server())
    lines, _ = doctor.run("http://gb.invalid", "core", "")
    rendered = doctor.render(lines).splitlines()

    summary = [i for i, r in enumerate(rendered) if r.startswith("PASS") and "local" in r][0]
    assert rendered[summary + 1].startswith("    "), "the report must not sit in column zero"
    assert not any(r.startswith("gbfleet 0.1.0") for r in rendered)


# ---- what an independent reviewer found by bouncing this -------------------------------------

def test_gban_fleet_hands_the_supervisor_the_same_credential_doctor_does(home, monkeypatch):
    """THE BOUNCE. `doctor` translated $GRAPHBAN_API_KEY into $GBFLEET_API_KEY for its child;
    `gban fleet` was a bare subprocess.run and did not. So on a machine set up the way this
    tool's own premise assumes — one key exported under gban's name — `gban doctor` reported
    the local half green using a credential it manufactured, and `gban fleet up` (which
    `gban seats issue` sends people to BY NAME) started the supervisor with nothing.

    `gbfleet` reads only $GBFLEET_API_KEY and scores its absence FAIL, so the doctor was
    certifying a local half the next command could not reproduce.

    Sabotage: drop the env from cmd_fleet and this fails."""
    seen = {}
    monkeypatch.setenv(config.API_KEY_ENV, "gb_sk_SECRET")
    monkeypatch.delenv("GBFLEET_API_KEY", raising=False)
    monkeypatch.setattr("gban.cli.shutil.which", lambda name: "/usr/bin/gbfleet")

    class Done:
        returncode = 0

    monkeypatch.setattr("gban.cli.subprocess.run",
                        lambda argv, **kw: (seen.update(argv=argv, env=kw.get("env") or {}),
                                            Done())[1])
    assert main(["fleet", "up", "--seats-file", "s.txt"]) == 0

    assert seen["env"].get("GBFLEET_API_KEY") == "gb_sk_SECRET"
    assert "gb_sk_SECRET" not in " ".join(seen["argv"]), "never argv; ps shows it to everyone"


def test_a_credential_the_caller_set_for_the_supervisor_is_not_overwritten(home, monkeypatch):
    """Somebody who exported $GBFLEET_API_KEY deliberately — to run the supervisor on a
    different credential from the one `gban` uses — means it."""
    seen = {}
    monkeypatch.setenv(config.API_KEY_ENV, "gb_sk_GBAN")
    monkeypatch.setenv("GBFLEET_API_KEY", "gb_sk_THEIRS")
    monkeypatch.setattr("gban.cli.shutil.which", lambda name: "/usr/bin/gbfleet")

    class Done:
        returncode = 0

    monkeypatch.setattr("gban.cli.subprocess.run",
                        lambda argv, **kw: (seen.update(env=kw.get("env") or {}), Done())[1])
    main(["fleet", "ps"])
    assert seen["env"]["GBFLEET_API_KEY"] == "gb_sk_THEIRS"


def test_both_launch_paths_go_through_one_function(home, monkeypatch):
    """They disagreed once, and the disagreement is what made the doctor's verdict a lie
    about what the next command would do. One function is what stops the next divergence."""
    import inspect

    from gban import cli as cli_mod

    for fn in (cli_mod.cmd_fleet, doctor.local):
        assert "child_environment" in inspect.getsource(fn), (
            f"{fn.__qualname__} builds the child's environment some other way")


# ---- finding a supervisor that PATH cannot see -----------------------------------------------

def test_a_sibling_gbfleet_is_found_when_path_cannot_see_it(home, monkeypatch, tmp_path):
    """MEASURED. `uv tool install "graphban-cli[fleet]"` puts gbfleet in the same `bin/` as
    gban and exposes only gban — uv deliberately exposes the requested package's executables
    and no others. `gban fleet` then said "gbfleet is not installed here" while gbfleet sat in
    the very environment it was running from. A plain `pip install` of both into one
    virtualenv has the same shape.

    Sabotage: go back to `shutil.which` alone and this fails."""
    binbase = tmp_path / "bin"
    binbase.mkdir()
    sibling = binbase / "gbfleet"
    sibling.write_text("#!/bin/sh\nexit 0\n")
    sibling.chmod(0o755)

    monkeypatch.setattr("gban.doctor.shutil.which", lambda name: None)
    monkeypatch.setattr("sys.executable", str(binbase / "python"))
    assert doctor.find_supervisor() == str(sibling)


def test_path_still_wins_over_a_sibling(home, monkeypatch, tmp_path):
    """A supervisor the operator put on PATH deliberately is the one they meant. The sibling
    is a fallback for what PATH cannot see, not an override of it."""
    binbase = tmp_path / "bin"
    binbase.mkdir()
    (binbase / "gbfleet").write_text("#!/bin/sh\nexit 0\n")
    (binbase / "gbfleet").chmod(0o755)

    monkeypatch.setattr("gban.doctor.shutil.which", lambda name: "/usr/local/bin/gbfleet")
    monkeypatch.setattr("sys.executable", str(binbase / "python"))
    assert doctor.find_supervisor() == "/usr/local/bin/gbfleet"


def test_a_sibling_that_is_not_executable_is_not_a_supervisor(home, monkeypatch, tmp_path):
    binbase = tmp_path / "bin"
    binbase.mkdir()
    (binbase / "gbfleet").write_text("not a program\n")
    (binbase / "gbfleet").chmod(0o644)

    monkeypatch.setattr("gban.doctor.shutil.which", lambda name: None)
    monkeypatch.setattr("sys.executable", str(binbase / "python"))
    assert doctor.find_supervisor() == ""


def test_the_install_it_names_is_one_that_exists(home, monkeypatch, capsys):
    """It said `uv pip install graphban-fleet` until somebody ran it. Neither package is on
    PyPI, so that 404s — a tool whose remedy does not work spends the reader's trust before
    it spends their time.

    Sabotage: name a PyPI install again and this fails."""
    monkeypatch.setattr("gban.cli.doctor_mod.find_supervisor", lambda: "")
    assert main(["fleet", "ps"]) == EXIT_NO_SUPERVISOR
    err = capsys.readouterr().err
    assert doctor.INSTALL_SUPERVISOR in err
    assert "uv pip install graphban-fleet" not in err, "that package name has never existed"


def test_the_doctor_names_the_same_install(home, monkeypatch):
    monkeypatch.setattr("gban.doctor.find_supervisor", lambda: "")
    monkeypatch.setattr("gban.doctor.authenticated", lambda url: _Server())
    lines, _ = doctor.run("http://gb.invalid", "core", "")
    local = [l for l in lines if l["side"] == "local"][0]
    assert doctor.INSTALL_SUPERVISOR in local["detail"]
    assert "uv pip install graphban-fleet" not in local["detail"]


def test_gban_fleet_runs_a_sibling_supervisor_too(home, monkeypatch, tmp_path):
    """Not just `doctor`. The two used to disagree about the environment they hand the child
    (GRPH-782); disagreeing about whether the child EXISTS is the same defect one step
    earlier, and `gban fleet` is the command a person actually runs.

    Sabotage: point cmd_fleet back at `shutil.which` and this fails."""
    binbase = tmp_path / "bin"
    binbase.mkdir()
    sibling = binbase / "gbfleet"
    sibling.write_text("#!/bin/sh\nexit 0\n")
    sibling.chmod(0o755)

    seen = {}
    monkeypatch.setattr("gban.doctor.shutil.which", lambda name: None)
    monkeypatch.setattr("gban.cli.shutil.which", lambda name: None)
    monkeypatch.setattr("sys.executable", str(binbase / "python"))

    class Done:
        returncode = 0

    monkeypatch.setattr("gban.cli.subprocess.run",
                        lambda argv, **kw: (seen.update(argv=argv), Done())[1])
    assert main(["fleet", "ps"]) == 0
    assert seen["argv"][0] == str(sibling), "it did not reach the supervisor beside it"


def test_both_commands_find_the_supervisor_the_same_way(home):
    """One lookup, or they drift — which is what GRPH-782 was."""
    import inspect

    from gban import cli as cli_mod

    for fn in (cli_mod.cmd_fleet, doctor.local):
        assert "find_supervisor" in inspect.getsource(fn), (
            f"{fn.__qualname__} looks for gbfleet some other way")


# ---- offering to install the supervisor, never doing it uninvited -----------------------------

class _Tty:
    """Stdin that claims to be a terminal, so the guard below is not what is under test."""

    def isatty(self) -> bool:
        return True


def test_it_never_installs_without_being_asked(home, monkeypatch, capsys):
    """THE INVARIANT. Installing software is not a side effect anybody should get from
    `gban fleet ps` — it writes outside this program and a person who typed a read-only
    command did not consent to it.

    Sabotage: install on a bare return and this fails."""
    ran = []
    monkeypatch.setattr("sys.stdin", _Tty())
    monkeypatch.setattr("gban.doctor.shutil.which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr("gban.doctor.subprocess.run", lambda *a, **kw: ran.append(a))
    monkeypatch.setattr("builtins.input", lambda *_: "")   # a bare return is not a yes

    assert doctor.offer_to_install() is False
    assert ran == [], "it installed without an explicit yes"


def test_a_yes_runs_the_install_and_reports_what_it_found(home, monkeypatch):
    ran = []

    class Done:
        returncode = 0

    monkeypatch.setattr("sys.stdin", _Tty())
    monkeypatch.setattr("gban.doctor.shutil.which",
                        lambda name: "/usr/bin/uv" if name == "uv" else None)
    monkeypatch.setattr("gban.doctor.subprocess.run",
                        lambda argv, **kw: (ran.append(argv), Done())[1])
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    monkeypatch.setattr("gban.doctor.find_supervisor", lambda: "/usr/bin/gbfleet")

    assert doctor.offer_to_install() is True
    assert ran == [["uv", "tool", "install", "graphban-fleet"]]


def test_a_failed_install_is_reported_and_not_papered_over(home, monkeypatch, capsys):
    """Returning True after a failed install would send the caller to look for a binary that
    is not there, and the second error would describe the wrong thing."""
    class Died:
        returncode = 2

    monkeypatch.setattr("sys.stdin", _Tty())
    monkeypatch.setattr("gban.doctor.shutil.which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr("gban.doctor.subprocess.run", lambda *a, **kw: Died())
    monkeypatch.setattr("builtins.input", lambda *_: "y")

    assert doctor.offer_to_install() is False
    assert "install failed" in capsys.readouterr().err


def test_it_does_not_prompt_where_nobody_can_answer(home, monkeypatch):
    """A prompt in a script is a hang, and a hang is worse than the error it replaced."""
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setattr("gban.doctor.shutil.which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr("builtins.input",
                        lambda *_: pytest.fail("it prompted with no terminal"))
    assert doctor.offer_to_install() is False


def test_no_uv_means_no_offer(home, monkeypatch):
    """The offer names one installer. Without it there is nothing to offer, and the caller's
    message is the whole answer."""
    monkeypatch.setattr("sys.stdin", _Tty())
    monkeypatch.setattr("gban.doctor.shutil.which", lambda name: None)
    monkeypatch.setattr("builtins.input",
                        lambda *_: pytest.fail("it offered an installer it does not have"))
    assert doctor.offer_to_install() is False


def test_gban_fleet_runs_the_command_after_a_successful_install(home, monkeypatch):
    """The point of asking at all: the person typed `gban fleet ps`, and after saying yes
    they should get `ps` — not a second error telling them to type it again."""
    seen = {}

    class Done:
        returncode = 0

    calls = iter(["", "/usr/bin/gbfleet"])   # missing, then present after the install
    monkeypatch.setattr("gban.cli.doctor_mod.find_supervisor", lambda: next(calls))
    monkeypatch.setattr("gban.cli.doctor_mod.offer_to_install", lambda: True)
    monkeypatch.setattr("gban.cli.subprocess.run",
                        lambda argv, **kw: (seen.update(argv=argv), Done())[1])

    assert main(["fleet", "ps"]) == 0
    assert seen["argv"][0] == "/usr/bin/gbfleet"


def test_a_declined_offer_still_prints_the_command(home, monkeypatch, capsys):
    monkeypatch.setattr("gban.cli.doctor_mod.find_supervisor", lambda: "")
    monkeypatch.setattr("gban.cli.doctor_mod.offer_to_install", lambda: False)
    assert main(["fleet", "ps"]) == EXIT_NO_SUPERVISOR
    assert doctor.INSTALL_SUPERVISOR in capsys.readouterr().err
