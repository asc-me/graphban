"""PRD-40 PR 2 — `gb doctor` and the `gb fleet` pass-through (criteria 6, 7)."""
from __future__ import annotations

import json

import pytest

from gb import config, doctor
from gb.cli import main
from gb.client import EXIT_NO_SUPERVISOR, NoSession, Refused, Unreachable


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
    monkeypatch.setattr("gb.doctor.authenticated", lambda url: server)
    monkeypatch.setattr("gb.doctor.shutil.which", lambda name: gbfleet)
    if gbfleet:
        class Done:
            returncode, stdout, stderr = 0, "PASS  repo  clean", ""
        monkeypatch.setattr("gb.doctor.subprocess.run", lambda *a, **kw: Done())
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
    monkeypatch.setattr("gb.doctor.authenticated",
                        lambda url: (_ for _ in ()).throw(
                            Unreachable("could not reach http://gb.invalid")))
    monkeypatch.setattr("gb.doctor.shutil.which", lambda name: None)
    lines, code = doctor.run("http://gb.invalid", "core", "")
    assert [l["status"] for l in lines if l["side"] == "ledger"] == ["UNKNOWN"]
    assert code == 0


def test_the_three_ledger_failures_are_three_different_lines(home, monkeypatch):
    """6 / D7. They send a reader to three different places."""
    monkeypatch.setattr("gb.doctor.shutil.which", lambda name: None)

    monkeypatch.setattr("gb.doctor.authenticated",
                        lambda url: (_ for _ in ()).throw(NoSession("no stored session")))
    expired, _ = doctor.run("http://gb.invalid", "core", "")
    assert expired[0]["name"] == "session" and "gb login" in expired[0]["detail"]

    monkeypatch.setattr("gb.doctor.authenticated",
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

    monkeypatch.setattr("gb.cli.shutil.which", lambda name: "/usr/bin/gbfleet")
    monkeypatch.setattr("gb.cli.subprocess.run",
                        lambda argv, **kw: (seen.update(argv=argv), Done())[1])
    assert main(["fleet", "up", "--adapter", "claude"]) == 75
    assert seen["argv"][:3] == ["/usr/bin/gbfleet", "up", "--adapter"]


def test_gb_fleet_fills_in_the_server_and_project_it_already_knows(home, monkeypatch):
    seen = {}

    class Done:
        returncode = 0

    monkeypatch.setattr("gb.cli.shutil.which", lambda name: "/usr/bin/gbfleet")
    monkeypatch.setattr("gb.cli.subprocess.run",
                        lambda argv, **kw: (seen.update(argv=argv), Done())[1])
    assert main(["fleet", "ps"]) == 0
    assert "--server" in seen["argv"] and "http://gb.invalid" in seen["argv"]
    assert "--project" in seen["argv"] and "core" in seen["argv"]


def test_an_explicit_server_is_not_overridden(home, monkeypatch):
    seen = {}

    class Done:
        returncode = 0

    monkeypatch.setattr("gb.cli.shutil.which", lambda name: "/usr/bin/gbfleet")
    monkeypatch.setattr("gb.cli.subprocess.run",
                        lambda argv, **kw: (seen.update(argv=argv), Done())[1])
    main(["fleet", "ps", "--server", "http://other.invalid"])
    assert seen["argv"].count("--server") == 1
    assert "http://gb.invalid" not in seen["argv"]


def test_a_missing_supervisor_says_how_to_install_it(home, monkeypatch, capsys):
    """7. Exit 4, its own code, so it cannot be confused with something gbfleet said."""
    monkeypatch.setattr("gb.cli.shutil.which", lambda name: None)
    assert main(["fleet", "ps"]) == EXIT_NO_SUPERVISOR
    assert "graphban-fleet" in capsys.readouterr().err


def test_the_credential_reaches_gbfleet_in_the_environment_never_argv(home, monkeypatch):
    """D5. `ps` shows argv to every process on the machine."""
    seen = {}
    monkeypatch.setenv(config.API_KEY_ENV, "gb_sk_SECRET")
    monkeypatch.setattr("gb.doctor.shutil.which", lambda name: "/usr/bin/gbfleet")

    class Done:
        returncode, stdout, stderr = 0, "", ""

    def run(argv, **kw):
        seen["argv"], seen["env"] = argv, kw.get("env") or {}
        return Done()

    monkeypatch.setattr("gb.doctor.subprocess.run", run)
    monkeypatch.setattr("gb.doctor.authenticated", lambda url: _Server())
    doctor.run("http://gb.invalid", "core", "gb_sk_SECRET")

    assert "gb_sk_SECRET" not in " ".join(seen["argv"])
    assert seen["env"].get("GBFLEET_API_KEY") == "gb_sk_SECRET"


def test_doctor_json_is_parsed_and_the_human_rendering_is_absent(home, monkeypatch, capsys):
    monkeypatch.setattr("gb.doctor.authenticated", lambda url: _Server())
    monkeypatch.setattr("gb.doctor.shutil.which", lambda name: None)
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

    monkeypatch.setattr("gb.doctor.shutil.which", lambda name: "/usr/bin/gbfleet")
    monkeypatch.setattr("gb.doctor.subprocess.run", lambda *a, **kw: Done())
    monkeypatch.setattr("gb.doctor.authenticated", lambda url: _Server())
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

    monkeypatch.setattr("gb.doctor.shutil.which", lambda name: "/usr/bin/gbfleet")
    monkeypatch.setattr("gb.doctor.subprocess.run", lambda *a, **kw: Done())
    monkeypatch.setattr("gb.doctor.authenticated", lambda url: _Server())
    lines, _ = doctor.run("http://gb.invalid", "core", "")
    rendered = doctor.render(lines).splitlines()

    summary = [i for i, r in enumerate(rendered) if r.startswith("PASS") and "local" in r][0]
    assert rendered[summary + 1].startswith("    "), "the report must not sit in column zero"
    assert not any(r.startswith("gbfleet 0.1.0") for r in rendered)
