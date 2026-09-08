"""PRD-40 PR 3 — the acts: `gban seats`, `gban agents`, `gban keys` (criteria 5, 8, 9, 12)."""
from __future__ import annotations

import json
import pathlib
import re

import pytest

from gban import cli as cli_mod, config
from gban.cli import COMMANDS, main
from gban.client import EXIT_NO_SESSION, EXIT_REFUSED, Refused

ROOT = pathlib.Path(__file__).resolve().parents[2]
REFERENCE = ROOT / "docs" / "api-reference.md"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(config.HOME_ENV, str(tmp_path))
    for var in (config.URL_ENV, config.PROJECT_ENV, config.API_KEY_ENV):
        monkeypatch.delenv(var, raising=False)
    config.save_settings(url="http://gb.invalid", project="core")
    config.save_session("refresh-token", user="alex@example.com")
    return tmp_path


ROSTER = {
    "agents": [
        {"id": "a1", "key": "SA-A4", "active_role": "worker", "state": "idle",
         "credential": "gb_sk_ab12", "credential_posture": None,
         "last_refusal": {"tool": "mint_enrolment", "count": 3,
                          "reason": "mint_enrolment requires role 'planner'; SA-A4 is "
                                    "registered as 'worker'"}},
        {"id": "a2", "key": "SA-A9", "active_role": "planner", "state": "working",
         "credential": "gb_sk_cd34", "last_refusal": None},
    ],
    "seats": [{"id": "s1", "role": "worker", "wave": "wave-3", "state": "unused",
               "consumed_by": None}],
    "credentials": [{"id": "k1", "name": "wave-3 worker", "prefix": "gb_sk_ab12",
                     "wave": "wave-3", "revoked": False, "posture": None}],
}


class Recorder:
    """Every call, and what the server said back."""

    def __init__(self, replies=None, error=None):
        self.calls, self.replies, self.error = [], replies or {}, error

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if self.error is not None:
            raise self.error
        for prefix, reply in self.replies.items():
            if path.startswith(prefix):
                return reply
        return ROSTER if path.startswith("/api/fleet") else {}


def _run(monkeypatch, argv, *, replies=None, error=None):
    rec = Recorder(replies, error)
    monkeypatch.setattr(cli_mod, "authenticated", lambda url, act="": rec)
    return main(argv), rec


# ---- 8: the stuck-worker case, answerable at a terminal ---------------------------------------

def test_agents_shows_what_an_agent_was_last_refused_and_why(home, monkeypatch, capsys):
    """8. The whole reason this verb exists. A roster that says "idle worker" for an agent
    being refused every call it makes is what cost an afternoon and a database query on
    Super-Arc. Sabotage: drop the refusal line and this fails."""
    code, _ = _run(monkeypatch, ["agents"])
    out = capsys.readouterr().out
    assert code == 0
    assert "SA-A4" in out
    assert "last refused mint_enrolment x3" in out
    assert "registered as 'worker'" in out, "the reason, not just the tool"


def test_an_agent_with_nothing_to_report_gets_no_refusal_line(home, monkeypatch, capsys):
    """8. Otherwise every row carries a line that means nothing and the one that matters
    stops standing out."""
    _run(monkeypatch, ["agents"])
    out = capsys.readouterr().out
    assert out.count("last refused") == 1


def test_the_roster_is_one_read_and_not_three(home, monkeypatch):
    """D11. `GET /api/fleet` already returns roster, seats and credentials together."""
    for verb in ("agents", "seats", "keys"):
        _, rec = _run(monkeypatch, [verb])
        assert len(rec.calls) == 1, f"{verb} made {len(rec.calls)} calls"
        assert rec.calls[0][0] == "GET"


# ---- 9: the server's refusal, unedited --------------------------------------------------------

def test_a_ceiling_refusal_is_printed_in_the_servers_own_words(home, monkeypatch, capsys):
    """9 / D8. The credential ceiling is the server's rule; re-wording it here would put a
    second copy of it in the client. Sabotage: paraphrase the detail and this fails."""
    detail = ("key gb_sk_ab12 permits roles ['worker']; 'planner' is not among them. Mint a "
              "different credential to widen the ceiling.")
    code, _ = _run(monkeypatch, ["agents", "role", "a1", "planner"],
                   error=Refused(409, detail, hint="POST /api/fleet/keys"))
    assert code == EXIT_REFUSED
    err = capsys.readouterr().err
    assert detail in err
    assert "POST /api/fleet/keys" in err


def test_re_tasking_says_when_it_takes_effect(home, monkeypatch, capsys):
    """9. `role_assigned_at > role_acked_at` is the mechanism; a person who reads "is now
    planner" and sees a worker one second later has been told a half-truth."""
    reply = {"agent_id": "a1", "active_role": "planner",
             "takes_effect": "on the agent's next poll"}
    code, rec = _run(monkeypatch, ["agents", "role", "a1", "planner", "--reason", "only agent"],
                     replies={"/api/fleet/agents": reply})
    assert code == 0
    assert rec.calls == [("PUT", "/api/fleet/agents/a1/role",
                          {"role": "planner", "reason": "only agent"})]
    assert "next poll" in capsys.readouterr().out


# ---- 5: a key is not a session ----------------------------------------------------------------

def test_with_only_a_key_an_act_says_which_act_needs_a_session(home, monkeypatch, capsys):
    """5. Somebody who exported GRAPHBAN_API_KEY and watched `gban fleet` work has every reason
    to read "session expired" as a bug in the tool. Sabotage: fall back to the generic
    message and this fails."""
    config.clear_session()
    monkeypatch.setenv(config.API_KEY_ENV, "gb_sk_ab12")
    assert main(["agents", "role", "a1", "planner"]) == EXIT_NO_SESSION
    err = capsys.readouterr().err
    assert "gban agents role" in err, "it must name the act, not describe a category"
    assert "not an API key" in err
    assert "gb_sk_ab12" not in err, "criterion 11: never the credential itself"


def test_with_no_credential_at_all_the_message_is_the_plain_one(home, monkeypatch, capsys):
    """5. The API-key sentence is an answer to a specific confusion. Printed to somebody who
    has no key it would be noise about a thing they never did."""
    config.clear_session()
    assert main(["agents"]) == EXIT_NO_SESSION
    assert "session expired" in capsys.readouterr().err


# ---- 12: every verb names a route that exists -------------------------------------------------

#: One invocation per verb. The assertion below is that this table covers `COMMANDS`, so a
#: new verb cannot be added without appearing here — which is what stops criterion 12 from
#: passing vacuously the moment somebody adds a route nobody drives.
INVOCATIONS = {
    "whoami": ["whoami"],
    "logout": ["logout"],
    "seats": ["seats"],
    "agents": ["agents"],
    "keys": ["keys"],
    "seats issue": ["seats", "issue", "worker"],
    "seats revoke-unused": ["seats", "revoke-unused"],
    "agents role": ["agents", "role", "a1", "planner"],
    "keys mint": ["keys", "mint", "--role", "worker"],
}
#: Driven separately: `login` sends a password (its own test), `doctor` and `fleet` are PR 2,
#: `setup` writes config files and so needs a repository and a target of its own
#: (`test_setup.py`, which makes the same route-documentation assertion this file does).
NOT_DRIVEN_HERE = {"login", "doctor", "fleet", "setup"}


def _documented() -> list[re.Pattern]:
    text = REFERENCE.read_text(encoding="utf-8")
    paths = set(re.findall(r"`(/api/[^`]*)`", text))
    return [re.compile("^" + re.sub(r"\\\{[^}]*\\\}", "[^/]+", re.escape(p)) + "$")
            for p in paths]


def test_the_invocation_table_drives_every_verb(home):
    """Without this, criterion 12 checks whatever happens to be listed."""
    covered = {argv[0] for argv in INVOCATIONS.values()} | NOT_DRIVEN_HERE
    assert covered == set(COMMANDS), f"undriven verbs: {set(COMMANDS) - covered}"


@pytest.mark.parametrize("name", sorted(INVOCATIONS))
def test_every_verbs_http_call_names_a_route_that_exists(home, monkeypatch, name, capsys):
    """12. Sabotage: point a verb at an invented path and this fails. The paths come from
    the calls the verb actually MAKES, not from a registry the verb could disagree with."""
    _, rec = _run(monkeypatch, INVOCATIONS[name],
                  replies={"/api/fleet/seats": {"wave": "wave-4", "seats": []},
                           "/api/fleet/keys": {"plaintext": "x", "role": "worker"},
                           "/api/fleet/agents": {"agent_id": "a1", "active_role": "planner"}})
    documented = _documented()
    assert rec.calls, f"{name} made no HTTP call"
    for method, path, _body in rec.calls:
        bare = path.split("?")[0]
        assert any(p.match(bare) for p in documented), (
            f"{method} {bare} is not in docs/api-reference.md")


def test_the_route_check_would_notice_an_invented_path(home):
    """The sabotage, permanently: proof that `_documented()` does not match everything."""
    documented = _documented()
    assert not any(p.match("/api/fleet/seats/invented") for p in documented)


# ---- issuing, and what a client must not do with a code ---------------------------------------

def test_issuing_seats_takes_one_role_per_agent(home, monkeypatch, capsys):
    """The server's own shape: two agents on one seat share a session and cannot review each
    other, so the list repeats rather than counting."""
    reply = {"wave": "wave-4", "seats": [{"id": "s1", "role": "worker", "code": "WORKER-7F3K"},
                                         {"id": "s2", "role": "worker", "code": "WORKER-2Q9C"}]}
    code, rec = _run(monkeypatch, ["seats", "issue", "worker", "worker"],
                     replies={"/api/fleet/seats": reply})
    assert code == 0
    assert rec.calls[0][2] == {"project_id": "core", "roles": ["worker", "worker"], "wave": ""}
    assert "WORKER-7F3K" in capsys.readouterr().out


def test_an_issued_code_is_never_written_to_disk(home, monkeypatch, capsys):
    """A CLI that helpfully saved the codes would invent a second credential at rest that no
    route and no test knows about. Sabotage: write a seats file here and this fails."""
    reply = {"wave": "wave-4", "seats": [{"id": "s1", "role": "worker", "code": "WORKER-7F3K"}]}
    _run(monkeypatch, ["seats", "issue", "worker"], replies={"/api/fleet/seats": reply})
    on_disk = "".join(p.read_text(errors="ignore") for p in home.rglob("*") if p.is_file())
    assert "WORKER-7F3K" not in on_disk


def test_a_verb_that_needs_a_project_says_so_rather_than_guessing(home, monkeypatch, capsys):
    """A credential spanning several projects resolves a call that names none to its DEFAULT
    project, which is not where the seats were minted (the GRPH-718 failure)."""
    config.save_settings(url="http://gb.invalid")
    (home / config.SETTINGS_FILE).write_text(json.dumps({"url": "http://gb.invalid"}))
    with pytest.raises(SystemExit) as exit_info:
        main(["seats", "issue", "worker"])
    assert exit_info.value.code == EXIT_REFUSED
    assert "needs a project" in capsys.readouterr().err


def test_json_output_carries_no_human_rendering(home, monkeypatch, capsys):
    """10, for the new verbs."""
    _run(monkeypatch, ["--json", "agents"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["agents"][0]["last_refusal"]["tool"] == "mint_enrolment"
    assert "last refused" not in json.dumps(payload)
