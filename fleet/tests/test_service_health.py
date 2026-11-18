"""GRPH-1312 — a drain that exits non-zero at startup is a failed install, and says why.

A fresh clone's `.gbfleet/servers` named servers the host did not have; `until` refused at
startup and exited 2. `service install` had read the job once, two seconds in, while it was
still running, and reported success. `service status` then said "NOT running, last exit 2"
with no reason, though the err log held the refusal.

Sabotage: drop the health-window loop in `service.install` and
`test_a_drain_that_exits_2_inside_the_health_window_fails_the_install` reads "running" and
the CLI exits 0. Drop `reason=` in `service.status` and
`test_status_names_the_reason_from_the_err_log` loses the refusal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gbfleet import service
from gbfleet.cli import main

from tests.test_service import KEY, _args, _plan

REFUSAL = ("gbfleet until: browser, browser-use not in ~/.claude.json. Refusing rather than "
           "spawning without it")


def _readings(name, kind, script, *, says=""):
    """A `status` that answers each call with the next scripted (running, last_exit).

    `says` is what the drain writes to its err log as it dies. It is written at the reading
    that first finds the job dead, because that is when a real drain writes it. A log that
    already existed before `install` is the other order, and has its own test
    (`test_a_log_written_before_install_is_still_read_after_it`, GRPH-1662).
    """
    left = list(script)
    said: list[str] = []

    def fake(n, kind=""):
        running, last = left.pop(0) if len(left) > 1 else left[0]
        died = running is False and last not in (None, 0)
        if died and says and not said:
            _write_err(n, says)
            said.append(n)
        return service.Status(
            name=n, kind=kind or "systemd", installed=True, running=running,
            last_exit=last, unit_path=service.unit_path_for(n, kind or "systemd"),
            path_env="/usr/bin", reason=service.err_reason(n) if died else "")
    return fake


@pytest.fixture()
def quick(monkeypatch, tmp_path):
    # A config tree of this test's own. The suite's shared one may already hold a `logs/`
    # made before `install` ran `icacls` over its parent, and on Windows that strips the
    # only entries it had (see `_readings`).
    monkeypatch.setattr(service, "CONFIG_DIR", tmp_path / "gbfleet-config")
    monkeypatch.setattr(service, "_run", lambda cmd, timeout=20.0: (0, ""))
    monkeypatch.setattr(service, "SETTLE", 0)
    monkeypatch.setattr(service, "HEALTH_POLL", 0.001)
    monkeypatch.setattr(service, "HEALTH_WINDOW", 0.05)
    monkeypatch.setattr(service, "linger", lambda: True)


def _write_err(name: str, text: str) -> None:
    path = service.CONFIG_DIR / "logs" / f"{name}.err.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_a_drain_that_exits_2_inside_the_health_window_fails_the_install(
        git_repo: Path, monkeypatch, quick, capsys):
    plan = _plan(git_repo, kind="systemd")
    monkeypatch.setattr(service, "status",
                        _readings(plan.name, "systemd", [(True, None), (True, None),
                                                         (False, 2)],
                                  says="an older run's noise\n" + REFUSAL + "\n\n"))
    done = service.install(plan, KEY)
    assert done.running is False
    assert done.state is not None and done.state.last_exit == 2
    assert done.state.reason == REFUSAL

    monkeypatch.setenv(service.API_KEY_ENV, KEY)
    monkeypatch.setattr(service, "make_plan", lambda rest, **kw: plan)
    monkeypatch.setattr(service, "status",
                        _readings(plan.name, "systemd", [(True, None), (False, 2)],
                                  says=REFUSAL + "\n"))
    code = main(["service", "install", "--", *_args(git_repo)])
    err = capsys.readouterr().err
    assert code == 1, "a drain that died at startup was reported as installed"
    assert "NOT running, last exit 2" in err
    assert REFUSAL in err
    assert "the supervisor gave no reason" not in err


def test_a_log_written_before_install_is_still_read_after_it(
        git_repo: Path, monkeypatch, quick):
    """GRPH-1662. `install` restricts the config directory, and on Windows that ran
    `icacls /inheritance:r` with a non-inheritable grant, leaving a log that already
    existed under it with no entry its owner could read — `err_reason` came back "".
    The log is written FIRST here, which is the order a re-install meets.

    Sabotage (Windows CI): drop `(OI)(CI)` from `hostos.restrict_to_owner` and the reason
    reads "".
    """
    plan = _plan(git_repo, kind="systemd")
    _write_err(plan.name, REFUSAL + "\n")
    monkeypatch.setattr(service, "status",
                        _readings(plan.name, "systemd", [(True, None), (False, 2)]))
    done = service.install(plan, KEY)
    assert done.state is not None and done.state.last_exit == 2
    assert done.state.reason == REFUSAL
    assert service.err_reason(plan.name) == REFUSAL


def test_a_drain_still_running_at_the_end_of_the_window_is_installed(
        git_repo: Path, monkeypatch, quick):
    plan = _plan(git_repo, kind="systemd")
    monkeypatch.setattr(service, "status", _readings(plan.name, "systemd", [(True, None)]))
    done = service.install(plan, KEY)
    assert done.running is True


def test_an_idle_exit_0_inside_the_window_is_still_success(git_repo: Path, monkeypatch, quick):
    """`until` exits 0 when there is no ready work; that is not a failed install."""
    plan = _plan(git_repo, kind="systemd")
    monkeypatch.setattr(service, "status",
                        _readings(plan.name, "systemd", [(True, None), (False, 0)]))
    done = service.install(plan, KEY)
    assert done.state is not None and done.state.idle


def test_status_names_the_reason_from_the_err_log(git_repo: Path, monkeypatch):
    name = "drain-1312"
    _write_err(name, REFUSAL + "\n")
    unit = service.unit_path_for(name, "systemd")
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("[Service]\n", encoding="utf-8")
    monkeypatch.setattr(service, "_running_systemd", lambda n: (False, "inactive", 2))
    monkeypatch.setattr(service, "linger", lambda: True)
    state = service.status(name, kind="systemd")
    assert state.reason == REFUSAL
    assert state.line().endswith(f"— {REFUSAL}")


def test_a_clean_idle_status_carries_no_reason(git_repo: Path, monkeypatch):
    name = "drain-1312-idle"
    _write_err(name, REFUSAL + "\n")
    unit = service.unit_path_for(name, "systemd")
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("[Service]\n", encoding="utf-8")
    monkeypatch.setattr(service, "_running_systemd", lambda n: (False, "inactive", 0))
    monkeypatch.setattr(service, "linger", lambda: True)
    # GRPH-1698: exit 0 is idle only when the drain's log says its queue was empty.
    log = service.CONFIG_DIR / "logs" / f"{name}.log"
    log.write_text('{"ok": true, "reason": "idle"}\n', encoding="utf-8")
    state = service.status(name, kind="systemd")
    assert state.reason == "" and state.idle
    log.write_text("no run result yet\n", encoding="utf-8")
    state = service.status(name, kind="systemd")
    assert state.reason == "" and not state.idle and "reason unknown" in state.line()


def test_the_committed_servers_file_marks_host_optional_servers():
    """Clause 1, already true on main via GRPH-1397: a server not every drain host has is
    marked optional with `?`, so `until` logs and skips it instead of refusing."""
    root = Path(__file__).resolve().parents[2]
    names = [ln.strip() for ln in (root / ".gbfleet" / "servers").read_text().splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    assert "browser?" in names and "browser-use?" in names
    assert "browser" not in names and "browser-use" not in names


@pytest.mark.parametrize("reason,unready,says", [
    ("unready", 3, "unready, last exit 0, 3 line item(s)"),
    ("", 0, "reason unknown"),
])
def test_an_unsettled_exit_0_does_not_fail_the_install(
        git_repo: Path, monkeypatch, quick, capsys, reason, unready, says):
    """GRPH-1698: exit 0 with `reason: unready` (or no reason yet) is not idle — and not a
    crash. The install names it and succeeds; "NOT running ... gave no reason" was wrong.

    Sabotage: drop the `unsettled` branch in `cli` and this exits 1 with "NOT running".
    """
    plan = _plan(git_repo, kind="systemd")

    def fake(n, kind=""):
        return service.Status(
            name=n, kind="systemd", installed=True, running=False, last_exit=0,
            unit_path=service.unit_path_for(n, "systemd"), path_env="/usr/bin",
            run_reason=reason, unready=unready)

    monkeypatch.setenv(service.API_KEY_ENV, KEY)
    monkeypatch.setattr(service, "make_plan", lambda rest, **kw: plan)
    monkeypatch.setattr(service, "status", fake)
    code = main(["service", "install", "--", *_args(git_repo)])
    out = capsys.readouterr()
    assert code == 0, out.err
    assert says in out.out
    assert "NOT running" not in out.err and "NOT running" not in out.out
