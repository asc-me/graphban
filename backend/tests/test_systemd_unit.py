"""The systemd unit is one systemd accepts, and the install cannot lie about it (GRPH-583).

PRD-27 S4, the Linux half of S3. The PRD's risk note is why they are separate slices:
*"launchd and systemd have different restart semantics; 'it restarts on failure' must be
asserted on both rather than assumed from one."* That turned out to be exactly right — the
same install logic that worked on macOS was wrong here in two ways, both found by running it
on a real box and neither reachable from a unit test.

What these assert is the unit's content and the install's HONESTY. That systemd accepts the
file, starts it, restarts it and reports a crash loop is recorded on the item, measured
against systemd 257.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))
import graphban_systemd as gsd  # noqa: E402

ROOT = pathlib.Path("/opt/graphban")


@pytest.fixture()
def unit() -> str:
    return gsd.unit_text(root=ROOT, python=ROOT / "backend" / ".venv" / "bin" / "python",
                         user="graphban", port=8000)


# ---- what the unit must say ----------------------------------------------------------------

def test_it_restarts_and_waits_before_doing_so(unit):
    """`Restart=always` is the KeepAlive equivalent; `RestartSec` is what stops a unit that
    fails at startup spinning as fast as systemd can fork it.

    The DELAY is asserted as a number, not as a key being present. `RestartSec=` alone stayed
    true under a sabotage that set it to `0` — which is the defect itself, and the assertion
    read as though it covered it.
    """
    assert "Restart=always" in unit

    delay = next((ln.split("=", 1)[1] for ln in unit.splitlines()
                  if ln.startswith("RestartSec=")), None)
    assert delay is not None, "no RestartSec at all"
    assert int(delay) > 0, f"RestartSec={delay} lets a failing unit spin as fast as it forks"


def test_it_is_wanted_by_multi_user_so_it_starts_at_boot(unit):
    """A unit that is started but never enabled runs now and is gone after a reboot — the same
    "looks installed" failure in a different costume."""
    assert "WantedBy=multi-user.target" in unit


def test_it_does_not_hard_require_postgres(unit):
    """The database may be on another host entirely. `Requires=` on a unit that does not exist
    locally makes the service unstartable for a perfectly correct deployment."""
    assert "Requires=postgres" not in unit
    assert "After=network-online.target" in unit


def test_the_port_is_an_environment_assignment_not_left_to_dotenv(unit):
    """CARRIED FROM S3, where a real install found it. `app.serve` reads `$PORT`/`$HOST` from
    `os.environ`; `.env` is read by pydantic-settings into `settings` and never reaches the
    environment. On macOS that produced a daemon listening on 8000 while its `.env` said 8234.
    """
    assert "Environment=PORT=8000" in unit
    assert "Environment=HOST=" in unit


def test_the_working_directory_is_where_dotenv_lives(unit):
    assert f"WorkingDirectory={ROOT / 'current' / 'backend'}" in unit, (
        "it must read the directory S5 swaps — see test_install_layout.py"
    )


def test_it_runs_the_same_entrypoint_as_every_other_path(unit):
    assert "-m app.serve" in unit


def test_the_unit_carries_no_secret(unit):
    """`/etc/systemd/system` is world-readable exactly as a plist is. systemd HAS
    `EnvironmentFile=`, and it is still not used for the secret: the app reads its own `.env`,
    which is one mechanism on both platforms rather than a per-platform special case."""
    assert gsd.secrets_in(unit) == []


@pytest.mark.parametrize("assignment", [
    "Environment=JWT_SECRET=hunter2",
    "Environment=SMTP_PASSWORD=x",
    "EnvironmentFile=/etc/graphban/secret_key",
])
def test_a_credential_in_the_unit_is_detected(unit, assignment):
    assert gsd.secrets_in(unit + "\n" + assignment), f"{assignment} would have been written"


def test_the_cli_refuses_rather_than_warns(monkeypatch, capsys):
    monkeypatch.setattr(gsd, "unit_text", lambda **kw: "Environment=JWT_SECRET=s\n")
    assert gsd.main(["unit"]) == 2
    assert "refusing" in capsys.readouterr().err


# ---- the two the real install found ---------------------------------------------------------

def test_a_user_scope_unit_omits_the_user_directive():
    """FOUND ON A REAL BOX. A `--user` unit already runs as that user; naming `User=` makes
    systemd try to set supplementary groups it may not, and every start dies with
    `216/GROUP` — `Failed at step GROUP spawning /usr/bin/python3: Operation not permitted`.

    The failure is invisible from the unit file: it parses, `systemd-analyze verify` accepts
    it, `systemctl start` returns 0, and the service crash loops forever.
    """
    scoped = gsd.unit_text(root=ROOT, python=ROOT / "p", user="alex", user_scope=True)
    system = gsd.unit_text(root=ROOT, python=ROOT / "p", user="graphban")

    assert "User=" not in scoped, "a --user unit naming User= dies at step GROUP"
    assert "User=graphban" in system, "the system unit still needs it"


def test_install_refuses_a_crash_looping_unit(monkeypatch, tmp_path, capsys):
    """THE GUARD THAT WAS ITSELF FOOLED, and the reason this test exists.

    The first version polled `is-active` and returned success on the first `active`. With
    `Restart=always`, systemd keeps restarting a dying unit and `is-active` reads `active` for
    the instant between fork and exit — so the check written to prevent "installs, appears to
    start, serves nothing" reported precisely that, for a unit failing every five seconds.

    A service that is up is still up a moment later, and `NRestarts` answers it directly.
    """
    monkeypatch.setattr(gsd.time, "sleep", lambda s: None)
    monkeypatch.setattr(gsd, "is_active", lambda *a, **k: True)
    monkeypatch.setattr(gsd, "_run", lambda cmd: (0, "3" if "show" in cmd else ""))

    rc = gsd.install(tmp_path / "u.service", "[Unit]\n", user_scope=True, name="x.service")

    assert rc == 1
    assert "crash looping" in capsys.readouterr().err


def test_install_accepts_a_unit_that_stays_up(monkeypatch, tmp_path, capsys):
    """The control. Without it, refusing EVERYTHING would satisfy the test above."""
    monkeypatch.setattr(gsd.time, "sleep", lambda s: None)
    monkeypatch.setattr(gsd, "is_active", lambda *a, **k: True)
    monkeypatch.setattr(gsd, "_run", lambda cmd: (0, "0" if "show" in cmd else ""))

    rc = gsd.install(tmp_path / "u.service", "[Unit]\n", user_scope=True, name="x.service")

    assert rc == 0
    assert "installed and running" in capsys.readouterr().out


def test_install_enables_before_starting(monkeypatch, tmp_path):
    """`enable` is what survives a reboot, and it must happen — a start-only install is the
    "looks installed until the machine restarts" failure."""
    seen: list[str] = []
    monkeypatch.setattr(gsd.time, "sleep", lambda s: None)
    monkeypatch.setattr(gsd, "is_active", lambda *a, **k: True)

    def rec(cmd):
        seen.append(next((c for c in cmd if c in
                          ("daemon-reload", "enable", "restart", "show", "status")), ""))
        return (0, "0")
    monkeypatch.setattr(gsd, "_run", rec)

    gsd.install(tmp_path / "u.service", "[Unit]\n", user_scope=True, name="x.service")

    assert "enable" in seen and "restart" in seen
    assert seen.index("enable") < seen.index("restart")


# ---- where it goes --------------------------------------------------------------------------

def test_a_system_unit_goes_in_etc_systemd_system():
    assert gsd.unit_path().parent == pathlib.Path("/etc/systemd/system")


def test_a_user_unit_goes_under_config():
    assert "systemd/user" in str(gsd.unit_path(user_scope=True))
