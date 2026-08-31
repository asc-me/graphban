"""The launchd unit is one launchd would accept, and carries nothing it must not (GRPH-582).

PRD-27 S3. The risk the PRD names for this slice is *"an installer that half-works is worse
than none — a service that installs, appears to start, and serves nothing."* This repository
has shipped that shape twice recently: a `Heartbeat` constructed and never started (GRPH-496),
and a trace helper nobody called (GRPH-506). So the assertions below are about the unit being
correct, and the real acceptance is an actual `launchctl` load recorded on the item.

The secret test is the load-bearing one. A `LaunchDaemon` plist is world-readable — every one
in `/Library/LaunchDaemons` on a stock machine is `-rw-r--r-- root wheel` — so a credential in
`EnvironmentVariables` is readable by every user on the box. The PRD's own grill answer got
this wrong, and this file is what stops it being reintroduced.
"""
from __future__ import annotations

import pathlib
import plistlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))
import graphban_service as gs  # noqa: E402

ROOT = pathlib.Path("/opt/graphban")


@pytest.fixture()
def plist() -> dict:
    return gs.plist_dict(root=ROOT, python=ROOT / "backend" / ".venv" / "bin" / "python",
                         user="graphban")


# ---- what launchd needs to be true --------------------------------------------------------

def test_it_is_a_plist_launchd_can_parse(plist):
    """Rendered and read back, rather than asserted on the dict we just built — a dict that
    cannot serialise is a job launchd never sees."""
    assert plistlib.loads(gs.render(plist)) == plist


def test_it_starts_at_boot_and_restarts_after_a_crash(plist):
    """BOTH, and this is the assertion that matters most.

    `RunAtLoad` alone gives a service that is gone after its first crash. `KeepAlive` alone
    gives one that does not come back after a reboot. Either failure looks identical to a
    working install right up until the machine is unattended, which is the only condition this
    service exists for.
    """
    assert plist["RunAtLoad"] is True, "it would not start at boot"
    assert plist["KeepAlive"] is True, "it would not come back after a crash"


def test_it_runs_the_same_entrypoint_as_the_container(plist):
    """`app.serve`, not the uvicorn CLI. One way the application starts, so a second cannot
    drift from it — the container's own Dockerfile argues this at length."""
    assert plist["ProgramArguments"][1:] == ["-m", "app.serve"]


def test_it_uses_the_venvs_interpreter_not_whatever_is_on_path(plist):
    """A service that resolves `python3` from PATH picks up whatever the last installer put
    there, which is a different program on a different day."""
    assert plist["ProgramArguments"][0].endswith("/.venv/bin/python")


def test_the_working_directory_is_where_dotenv_lives(plist):
    """pydantic-settings reads `.env` relative to the cwd. Without this the service starts
    with defaults — including a placeholder JWT secret — and looks perfectly healthy."""
    assert plist["WorkingDirectory"] == str(ROOT / "current" / "backend"), (
        "it must read the directory S5 swaps — see test_install_layout.py"
    )


def test_the_port_is_a_real_environment_variable_not_left_to_dotenv():
    """FOUND BY A REAL INSTALL, and no unit test would have caught it.

    `app.serve` reads `$PORT`/`$HOST` from `os.environ` itself, while `.env` is read by
    pydantic-settings into `settings` and never reaches the environment. Docker supplies them
    via compose; natively nothing does.

    The first daemon this generated loaded cleanly, logged "Started server process", and
    listened on **8000** while its `.env` said 8234 — launchd happy, `launchctl list` showing
    it up, and nothing answering on the port anyone would curl. That is precisely the
    "installs, appears to start, serves nothing" failure this slice exists to prevent.
    """
    p = gs.plist_dict(root=ROOT, python=ROOT / "p", user="graphban", port=8234,
                      host="127.0.0.1")
    env = p["EnvironmentVariables"]

    assert env.get("PORT") == "8234", ".env cannot supply PORT; the plist must"
    assert env.get("HOST") == "127.0.0.1"
    assert "GIT_SHA" in env, (
        "without GIT_SHA in the environment /health reports unknown and an upgrade cannot verify"
    )


def test_it_does_not_run_as_root(plist):
    assert plist["UserName"] == "graphban"


def test_the_revision_is_a_real_environment_variable():
    p = gs.plist_dict(root=ROOT, python=ROOT / "p", user="graphban", git_sha="abc1234")
    assert p["EnvironmentVariables"]["GIT_SHA"] == "abc1234"


def test_a_user_domain_plist_does_not_name_a_user():
    """UserName is a LaunchDaemon key. Naming one on an agent is a privilege launchd
    will not grant, and the job dies looking installed."""
    p = gs.plist_dict(root=ROOT, python=ROOT / "p", user="graphban", user_domain=True)
    assert "UserName" not in p


def test_logs_go_somewhere_an_operator_already_looks(plist):
    assert plist["StandardOutPath"].endswith(".log")
    assert plist["StandardErrorPath"].endswith(".err.log")


# ---- the one that is a security property ---------------------------------------------------

def test_the_plist_carries_no_secret(plist):
    """THE CORRECTION TO THE PRD'S OWN ANSWER.

    The grill said the secret would ride in `EnvironmentVariables` "so it is not in `ps`
    output" — true, and wrong about disk: launchd has no `EnvironmentFile`, and this file is
    world-readable. Not in `ps` but readable by every user is a worse trade than the one it
    was avoiding.
    """
    assert gs.secrets_in(plist) == []


def test_a_secret_added_to_the_environment_is_detected():
    """The guard must catch the obvious mistake, which is putting it in the obvious place."""
    bad = gs.plist_dict(root=ROOT, python=ROOT / "p", user="graphban")
    bad["EnvironmentVariables"]["JWT_SECRET"] = "hunter2"

    assert "EnvironmentVariables.JWT_SECRET" in gs.secrets_in(bad)


@pytest.mark.parametrize("key", ["JWT_SECRET", "SECRET_ENCRYPTION_KEY", "SMTP_PASSWORD",
                                 "GITHUB_WEBHOOK_SECRET", "UPSTREAM_FEEDBACK_TOKEN"])
def test_every_credential_this_product_actually_has_is_detected(key):
    """Named from `docs/configuration.md` rather than invented, so the guard covers the
    secrets that exist rather than the ones I happened to think of."""
    bad = gs.plist_dict(root=ROOT, python=ROOT / "p", user="graphban")
    bad["EnvironmentVariables"][key] = "x"

    assert gs.secrets_in(bad), f"{key} would have been written to a world-readable plist"


def test_the_cli_refuses_rather_than_warns(monkeypatch, capsys):
    """A world-readable plist with a credential in it is not a thing to warn about and
    continue past."""
    monkeypatch.setattr(gs, "plist_dict",
                        lambda **kw: {"Label": "x", "EnvironmentVariables": {"JWT_SECRET": "s"}})
    assert gs.main(["plist"]) == 2
    assert "refusing" in capsys.readouterr().err


# ---- where it goes ------------------------------------------------------------------------

def test_a_daemon_goes_in_the_system_directory():
    assert gs.plist_path().parent == pathlib.Path("/Library/LaunchDaemons")


# ---- accepted is not running ---------------------------------------------------------------

def test_install_reports_failure_when_launchd_took_the_job_but_it_is_not_running(monkeypatch,
                                                                                 tmp_path,
                                                                                 capsys):
    """`bootstrap` returning 0 means launchd ACCEPTED the job, not that it stayed up.

    A plist whose interpreter does not exist bootstraps cleanly and dies immediately — which
    is the shape this repository has shipped twice (a Heartbeat never started, a trace helper
    never called). So the install re-reads `launchctl list` rather than trusting the exit code.
    """
    monkeypatch.setattr(gs, "_run", lambda cmd: (0, ""))
    monkeypatch.setattr(gs, "_loaded", lambda label: False)

    rc = gs.install(tmp_path / "x.plist", b"<plist/>", user_domain=True, label="dev.test")

    assert rc == 1
    assert "not running" in capsys.readouterr().err


def test_install_waits_for_the_previous_job_to_go_before_bootstrapping(monkeypatch, tmp_path):
    """FOUND BY A REAL REINSTALL. `bootout` is asynchronous, and bootstrapping into the gap
    fails with `Bootstrap failed: 5: Input/output error` — a message that names nothing
    useful. An identical retry a second later succeeded.

    So the label must be gone from `launchctl list` before the new job is handed over.
    """
    seen: list[str] = []
    states = iter([True, True, False])  # loaded, loaded, then gone

    monkeypatch.setattr(gs, "_run", lambda cmd: (seen.append(cmd[1]), (0, ""))[1])
    monkeypatch.setattr(gs, "_loaded", lambda label: next(states, False))
    monkeypatch.setattr(gs.time, "sleep", lambda s: None)

    gs.install(tmp_path / "x.plist", b"<plist/>", user_domain=True, label="dev.test")

    assert seen.index("bootout") < seen.index("bootstrap"), "it bootstrapped before booting out"


def test_a_listed_crash_loop_is_not_running(monkeypatch):
    """A real pid is the whole test. `launchctl list` still prints a crash-loop as
    `-  1  label`. The first version asked only whether the label APPEARED, so it
    reported success for a service that had never stayed up. Tests that mock
    `_loaded` cannot catch `return True` whenever the label is in the listing.
    """
    def with_listing(out):
        monkeypatch.setattr(gs, "_run", lambda cmd: (0, out))

    with_listing("- \t1\tdev.graphban.api\n")
    assert gs._loaded("dev.graphban.api") is False, (
        "a crash-loop listed as `-  1  label` was treated as running"
    )
    with_listing("45316\t0\tdev.graphban.api\n")
    assert gs._loaded("dev.graphban.api") is True
    with_listing("")
    assert gs._loaded("dev.graphban.api") is False


def test_install_polls_loaded_until_the_previous_job_is_gone_before_bootstrap(
    monkeypatch, tmp_path
):
    """The wait test used to assert only that bootout is invoked before bootstrap,
    which remains true if the wait-until-gone loop is deleted. The async-bootout
    fix is the poll of `_loaded` between those two calls.
    """
    calls: list[str] = []
    states = iter([True, True, False])

    monkeypatch.setattr(gs, "_run", lambda cmd: (calls.append(cmd[1]), (0, ""))[1])
    monkeypatch.setattr(gs, "_loaded", lambda label: (calls.append("loaded"), next(states, False))[1])
    monkeypatch.setattr(gs.time, "sleep", lambda s: None)

    gs.install(tmp_path / "x.plist", b"<plist/>", user_domain=True, label="dev.test")

    bootout = calls.index("bootout")
    bootstrap = calls.index("bootstrap")
    assert "loaded" in calls[bootout:bootstrap], (
        "install bootstrapped without waiting for the previous job to leave launchctl list"
    )


def test_the_cli_install_command_calls_install(monkeypatch, tmp_path):
    """Tests drive `install()` as a function. The operator command is `main(["install"])`.
    Returning 0 without calling `install()` left every test green.
    """
    seen: list[str] = []
    monkeypatch.setattr(gs, "plist_dict", lambda **kw: {"Label": "x"})
    monkeypatch.setattr(gs, "secrets_in", lambda p: [])
    monkeypatch.setattr(gs, "plist_path", lambda *a, **k: tmp_path / "x.plist")
    monkeypatch.setattr(gs, "render", lambda d: b"<plist/>")
    monkeypatch.setattr(
        gs, "install",
        lambda *a, **k: seen.append("install") or 0,
    )
    assert gs.main(["install", "--root", str(tmp_path), "--user-domain"]) == 0
    assert seen == ["install"], "the CLI install command never handed the plist to launchd"


def test_the_user_domain_path_is_a_launch_agent():
    """Used only to verify the mechanism without a privileged install; the shipped service is
    the daemon, because an agent does not run until that user logs in."""
    assert gs.plist_path(user_domain=True).parent.name == "LaunchAgents"
