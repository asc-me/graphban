"""GRPH-844 / GRPH-852: a drain that outlives the terminal that started it.

Most of this file is about what the unit MUST NOT contain and what `status` must not say.
The install itself is one `launchctl`, `systemctl`, or `schtasks` call and is walked on a
real box; what tests can hold onto is the set of facts that were each paid for once by a
service that installed cleanly and did nothing — a unit carrying a key, a `User=` that kills
every start, an empty PATH that resolves no vendor, and a boolean that reports "not running"
for a host nobody could ask.

All three renderings are exercised on every platform by passing `kind=` explicitly. Without
that the macOS run would check the plist and CI would check the unit, and neither would ever
check Task Scheduler — the same hole `test_hostos` names. Tests never write into the real
Task Scheduler: `conftest` redirects the task dir and the runner path the way it redirects
`CONFIG_DIR`.
"""

from __future__ import annotations

import os
import plistlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbfleet import service  # noqa: E402
from gbfleet.service import Refused  # noqa: E402

KEY = "gbk_live_secret_value"


#: Named in every plan below rather than discovered. CI has no `gbfleet` on PATH and runs
#: pytest from a venv, so a suite that let `make_plan` resolve one passed on the machine it
#: was written on and refused seventeen tests on the runner. `_this_gbfleet` has tests of its
#: own; nothing else here should care what is installed.
BINARY = "/opt/fake/bin/gbfleet"


def _args(repo: Path) -> list[str]:
    return ["--repo", str(repo), "--server", "http://gb.invalid", "--project", "p",
            "--adapter", "claude"]


def _plan(repo: Path, **kw):
    kw.setdefault("binary", BINARY)
    return service.make_plan(_args(repo), **kw)


# --- what must never reach a unit file ---------------------------------------------


@pytest.mark.parametrize("kind", ["systemd", "launchd", "schtasks"])
def test_no_rendering_carries_the_api_key(git_repo: Path, kind, monkeypatch):
    monkeypatch.setenv(service.API_KEY_ENV, KEY)
    plan = _plan(git_repo, kind=kind)
    rendered = service.render(plan)
    # UTF-16 task XML would encode the key differently; decode before searching so a leak
    # in either encoding still fails the test.
    text = rendered.decode("utf-16" if rendered[:2] in (b"\xff\xfe", b"\xfe\xff")
                           else "utf-8", "replace")
    assert KEY not in text
    assert service.secrets_in(rendered) == []


@pytest.mark.parametrize("kind", ["systemd", "launchd", "schtasks"])
def test_the_guard_would_catch_a_key_that_did_reach_one(kind):
    """The guard itself, since the renderings are supposed to make it unreachable.

    A check that can only ever pass is not a check. `EnvironmentFile` is exempt because it
    names the path that keeps the key OUT, and a marker firing on it would fire on the fix.
    """
    assert service.secrets_in(b"Environment=GBFLEET_API_KEY=abc\n")
    leaky = plistlib.dumps({"EnvironmentVariables": {"ANTHROPIC_API_KEY": "sk-x"}})
    assert service.secrets_in(leaky) == ["EnvironmentVariables.ANTHROPIC_API_KEY"], (
        "the walk has to descend: EnvironmentVariables is where somebody would put one"
    )
    assert service.secrets_in(b"# a comment mentioning api_key\n") == []
    assert service.secrets_in(b"EnvironmentFile=/home/me/.config/gbfleet/gbfleet.env\n") == []
    # The regression this shape exists for: `SECRET_MARKERS` contains "private", and every
    # macOS temp path is under /private. A substring grep refused every install on this OS.
    assert service.secrets_in(b"WorkingDirectory=/private/var/folders/x/repo\n") == []
    assert service.secrets_in(
        plistlib.dumps({"WorkingDirectory": "/private/var/folders/x/repo"})) == []
    leaky_task = (
        '<?xml version="1.0"?><Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
        "<Actions><API_KEY>gbk_x</API_KEY></Actions></Task>"
    ).encode()
    assert service.secrets_in(leaky_task) == ["Task.Actions.API_KEY"], (
        f"got {service.secrets_in(leaky_task)!r}; element NAMES are where a key goes in XML"
    )


def test_install_refuses_a_unit_that_would_leak(git_repo: Path, monkeypatch):
    """Sabotage the CALL: a rendering that leaks must be refused, not merely detected."""
    plan = _plan(git_repo, kind="systemd")
    monkeypatch.setattr(service, "render", lambda _p: b"Environment=GBFLEET_API_KEY=abc\n")
    with pytest.raises(Refused, match="credential"):
        service.install(plan, KEY)
    assert not plan.unit_path.exists(), "nothing may be written after a refusal"


def test_install_refuses_a_unit_carrying_the_key_under_an_innocent_name(git_repo: Path,
                                                                       monkeypatch):
    """The guard `secrets_in` cannot be: the key smuggled through an argument.

    No marker list covers `ExecStart=… --header X-API=<key>`, and the key is in the file all
    the same. So `install` also looks for the literal value it is about to write — the one
    check here that cannot be fooled by naming.
    """
    plan = _plan(git_repo, kind="systemd")
    smuggled = f"ExecStart=/bin/gbfleet until --header auth={KEY}\n".encode()
    assert service.secrets_in(smuggled) == [], (
        "if a marker catches this, the test is checking the wrong guard"
    )
    monkeypatch.setattr(service, "render", lambda _p: smuggled)
    with pytest.raises(Refused, match="contains the API key"):
        service.install(plan, KEY)
    assert not plan.unit_path.exists()


# --- the two facts that kill a service at startup ----------------------------------


def test_the_systemd_unit_names_no_user(git_repo: Path):
    """`User=` in a --user unit is `216/GROUP` on every start. Paid for on a real install."""
    text = service.render(_plan(git_repo, kind="systemd")).decode()
    assert "\nUser=" not in text


def test_the_launchd_job_names_no_username(git_repo: Path):
    """`UserName` is a LaunchDaemon key; on an agent it asks for a privilege launchd refuses."""
    job = plistlib.loads(service.render(_plan(git_repo, kind="launchd")))
    assert "UserName" not in job
    # Both, or the service looks installed and is not running.
    assert job["RunAtLoad"] is True and job["KeepAlive"] is True


# --- the PATH, which is what a fleet service is actually for ------------------------


@pytest.mark.parametrize("kind", ["systemd", "launchd", "schtasks"])
def test_the_installing_shell_path_is_carried_into_the_unit(git_repo: Path, kind):
    """No vendor CLI is on a supervisor's default PATH, and the vendors are the point."""
    plan = _plan(git_repo, kind=kind, path_env="/opt/vendors:/usr/bin")
    rendered = service.render(plan)
    text = rendered.decode("utf-16" if rendered[:2] in (b"\xff\xfe", b"\xfe\xff")
                           else "utf-8", "replace")
    assert "/opt/vendors" in text


@pytest.mark.parametrize("kind", ["systemd", "launchd", "schtasks"])
def test_the_path_is_read_back_from_what_was_written(tmp_path: Path, git_repo: Path, kind,
                                                     monkeypatch):
    """`status` prints the unit's PATH, so it has to parse the file rather than guess."""
    monkeypatch.setattr(service, "unit_path_for",
                        lambda name, k: tmp_path / f"{name}.{k}")
    plan = _plan(git_repo, kind=kind, path_env="/opt/vendors:/usr/bin")
    plan.unit_path.write_bytes(service.render(plan))
    assert service._path_in(plan.unit_path, kind) == "/opt/vendors:/usr/bin"


# --- refusals, each one a service that would have run and done nothing --------------


def test_an_empty_install_is_refused(git_repo: Path):
    with pytest.raises(Refused, match="nothing to run"):
        service.make_plan([], kind="systemd", binary=BINARY)


def test_a_missing_repo_flag_is_refused(git_repo: Path):
    with pytest.raises(Refused, match="--repo"):
        service.make_plan(["--server", "http://gb.invalid"], kind="systemd", binary=BINARY)


def test_a_relative_repo_is_refused(git_repo: Path):
    """A unit has no working directory worth inheriting; the service would drain elsewhere."""
    with pytest.raises(Refused, match="absolute"):
        service.make_plan(["--repo", "."], kind="systemd", binary=BINARY)


def test_a_repo_that_is_not_a_git_repository_is_refused(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(Refused, match="git repository"):
        service.make_plan(["--repo", str(plain)], kind="systemd", binary=BINARY)


def test_a_repo_that_does_not_exist_is_refused(tmp_path: Path):
    with pytest.raises(Refused, match="does not exist"):
        service.make_plan(["--repo", str(tmp_path / "nope")], kind="systemd", binary=BINARY)


def test_a_zero_interval_is_refused(git_repo: Path):
    with pytest.raises(Refused, match="at least 1 second"):
        _plan(git_repo, kind="systemd", every=0)


def test_install_without_a_key_is_refused_before_anything_is_written(git_repo: Path):
    plan = _plan(git_repo, kind="systemd")
    with pytest.raises(Refused, match=service.API_KEY_ENV):
        service.install(plan, "")
    assert not plan.unit_path.exists()


# --- the repo path the unit ends up carrying ---------------------------------------


def test_the_repo_is_resolved_into_the_unit(git_repo: Path):
    """`/srv/repo/fleet/..` is absolute, passes every check, and reads as another directory."""
    plan = service.make_plan(["--repo", f"{git_repo}/./"], kind="systemd", binary=BINARY)
    assert plan.repo == git_repo.resolve()
    assert str(plan.repo) in plan.until_args, "the resolved path must reach `until` too"


def test_the_plan_names_the_lock_the_service_will_hold(git_repo: Path):
    """Per git COMMON DIR: a running drain refuses interactive `up` on the whole clone."""
    plan = _plan(git_repo, kind="systemd")
    assert plan.common_dir == git_repo.resolve()


# --- the environment file ------------------------------------------------------------


def test_the_key_file_holds_the_key_and_only_this_user_may_read_it(git_repo: Path, tmp_path,
                                                                   monkeypatch):
    monkeypatch.setattr(service, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(service, "env_path_for", lambda name: tmp_path / "cfg" / f"{name}.env")
    plan = _plan(git_repo, kind="systemd")
    warnings = service.write_env(plan, KEY)
    assert warnings == []
    assert plan.env_path.read_text().strip() == f"{service.API_KEY_ENV}={KEY}"
    if os.name != "nt":
        assert plan.env_path.stat().st_mode & 0o077 == 0


# --- status: three answers, never two -----------------------------------------------


def test_an_unsupervised_host_is_unknown_not_not_installed(monkeypatch):
    monkeypatch.setattr(service, "host", lambda: service.Host("", "no systemd here"))
    state = service.status("gbfleet")
    assert state.kind == ""
    assert state.installed is None, "None is 'nobody could ask'; False would claim we looked"
    assert state.running is None
    assert "no systemd" in state.detail


def test_a_missing_unit_is_not_installed_rather_than_unknown(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(service, "host", lambda: service.Host("systemd"))
    monkeypatch.setattr(service, "unit_path_for", lambda n, k: tmp_path / "absent.service")
    monkeypatch.setattr(service, "linger", lambda: None)
    state = service.status("gbfleet")
    assert state.installed is False and state.running is None


def test_a_listed_launchd_job_with_no_pid_is_not_running(monkeypatch):
    """`- 1 label` is a job launchd knows about and is not running.

    Presence in the listing is not the answer, and reading it as one is how the server's
    installer once reported "installed" over a crash loop.
    """
    monkeypatch.setattr(service, "_run",
                        lambda cmd, timeout=20.0: (0, "-\t1\tdev.graphban.fleet.gbfleet"))
    running, detail, last = service._running_launchd("dev.graphban.fleet.gbfleet")
    assert running is False and "1" in detail
    assert last == 1, "the middle column is the last exit status, and it is the whole story"


def test_a_running_launchd_job_reports_its_pid(monkeypatch):
    monkeypatch.setattr(service, "_run",
                        lambda cmd, timeout=20.0: (0, "4242\t0\tdev.graphban.fleet.gbfleet"))
    assert service._running_launchd("dev.graphban.fleet.gbfleet") == (True, "pid 4242", 0)


def test_launchctl_that_cannot_be_asked_answers_none(monkeypatch):
    monkeypatch.setattr(service, "_run", lambda cmd, timeout=20.0: (1, "boom"))
    running, _, _ = service._running_launchd("x")
    assert running is None, "a failed launchctl is not evidence that nothing is running"


@pytest.mark.parametrize("out,expected", [
    ("active", True), ("activating", True),
    ("inactive", False), ("failed", False),
    ("", None), ("something-new", None),
])
def test_systemd_activity_has_three_answers(monkeypatch, out, expected):
    monkeypatch.setattr(service, "_run", lambda cmd, timeout=20.0: (0, out))
    assert service._running_systemd("gbfleet")[0] is expected


# --- uninstall ------------------------------------------------------------------------


def test_uninstall_removes_the_key_file_too(tmp_path: Path, git_repo: Path, monkeypatch):
    """A live key left in ~/.config after an uninstall is what nobody goes looking for."""
    unit = tmp_path / "gbfleet.service"
    env = tmp_path / "gbfleet.env"
    unit.write_text("[Unit]\n")
    env.write_text("GBFLEET_API_KEY=x\n")
    monkeypatch.setattr(service, "host", lambda: service.Host("systemd"))
    monkeypatch.setattr(service, "unit_path_for", lambda n, k: unit)
    monkeypatch.setattr(service, "env_path_for", lambda n: env)
    monkeypatch.setattr(service, "_run", lambda cmd, timeout=20.0: (0, ""))
    removed = service.uninstall("gbfleet")
    assert set(removed) == {str(unit), str(env)}
    assert not unit.exists() and not env.exists()


def test_uninstall_on_an_unsupervised_host_refuses(monkeypatch):
    monkeypatch.setattr(service, "host", lambda: service.Host("", "nothing here"))
    with pytest.raises(Refused, match="nothing here"):
        service.uninstall("gbfleet")


def test_a_drain_between_cycles_is_idle_not_broken():
    """`until` exits 0 when there is no ready work. That is the normal state, not a fault.

    Getting this wrong would be worse than having no check: a `FAIL` for most of every cycle
    is a check an operator learns to ignore, and it would be loudest exactly when the fleet
    is behaving correctly.
    """
    idle = service.Status(name="d", kind="launchd", installed=True, running=False,
                          last_exit=0, unit_path=Path("/tmp/x"))
    dead = service.Status(name="d", kind="launchd", installed=True, running=False,
                          last_exit=1, unit_path=Path("/tmp/x"))
    unknown = service.Status(name="d", kind="launchd", installed=True, running=False,
                             last_exit=None, unit_path=Path("/tmp/x"))
    assert idle.idle and "idle between runs" in idle.line()
    assert not dead.idle and "last exit 1" in dead.line()
    # Not idle: nobody said it exited cleanly, and "we could not tell" must not read as fine.
    assert not unknown.idle and "last exit unknown" in unknown.line()


def test_a_running_service_is_never_idle():
    live = service.Status(name="d", kind="systemd", installed=True, running=True, last_exit=0)
    assert not live.idle


# --- finding a drain nobody remembered installing -----------------------------------


@pytest.mark.parametrize("kind", ["systemd", "launchd", "schtasks"])
def test_installed_names_finds_what_this_program_wrote(tmp_path: Path, kind, monkeypatch):
    """Every unit is prefixed so it can be found again.

    Without this `status` and `doctor` can only ask about a name they already know, and an
    operator who ran `--name nightly` and forgot has a broken service neither will mention.
    """
    monkeypatch.setattr(service, "host", lambda: service.Host(kind))
    monkeypatch.setattr(service, "unit_dir", lambda k: tmp_path)
    for name in ("drain", "nightly"):
        service.unit_path_for(name, kind).write_bytes(b"x")
    # Things this program did not write, in the same directory. A LaunchAgents folder is full
    # of them, and `~/.config/systemd/user` is the user's own.
    (tmp_path / "com.someone.else.plist").write_bytes(b"x")
    (tmp_path / "syncthing.service").write_bytes(b"x")
    (tmp_path / "SomethingElse.xml").write_bytes(b"x")
    assert service.installed_names(kind) == ["drain", "nightly"]


def test_installed_names_on_an_unsupervised_host_is_empty(monkeypatch):
    monkeypatch.setattr(service, "host", lambda: service.Host("", "nothing here"))
    assert service.installed_names() == []


def test_the_systemd_unit_name_is_prefixed():
    assert service.unit_name("nightly") == "gbfleet-nightly.service"


def test_a_nonsense_short_key_does_not_trip_the_literal_guard(git_repo: Path, monkeypatch):
    """Found on the walk: `GBFLEET_API_KEY=x` refused the install.

    A substring search over a whole unit file is evidence only for a string long enough not
    to occur by accident, and "x" is in every path in it. A key too short to be real is let
    through here and fails at the server, which is the right place for it to fail.
    """
    plan = _plan(git_repo, kind="systemd")
    # No exception: the guard does not fire, and the install proceeds to write.
    service.install(plan, "x")
    assert plan.unit_path.exists()
    assert plan.env_path.read_text().strip().endswith("=x")


def test_a_real_length_key_still_trips_it(git_repo: Path, monkeypatch):
    plan = _plan(git_repo, kind="systemd")
    monkeypatch.setattr(service, "render", lambda _p: f"ExecStart=/x --auth={KEY}\n".encode())
    with pytest.raises(Refused, match="contains the API key"):
        service.install(plan, KEY)


# --- which gbfleet a unit is told to run --------------------------------------------


def test_the_running_gbfleet_wins(tmp_path: Path, monkeypatch):
    """The operator typed a program. Installing a different one is not done quietly."""
    typed = tmp_path / "gbfleet"
    typed.write_text("#!/bin/sh\n")
    typed.chmod(0o755)
    monkeypatch.setattr(service.sys, "argv", [str(typed)])
    monkeypatch.setattr(service.shutil, "which", lambda _n: "/somewhere/else/gbfleet")
    assert service._this_gbfleet() == str(typed.resolve())


def test_the_console_script_beside_this_interpreter_comes_before_path(tmp_path: Path,
                                                                     monkeypatch):
    """`python -m gbfleet.cli`, and every venv that is not on PATH.

    Without this the checkout's own gbfleet is invisible and a `uv tool` copy elsewhere gets
    installed instead — and on CI, where nothing is on PATH at all, `make_plan` refused.
    """
    binroot = tmp_path / "venv" / "bin"
    binroot.mkdir(parents=True)
    (binroot / "gbfleet").write_text("#!/bin/sh\n")
    (binroot / "gbfleet").chmod(0o755)
    monkeypatch.setattr(service.sys, "argv", ["/usr/lib/python3/site-packages/gbfleet/cli.py"])
    monkeypatch.setattr(service.sys, "executable", str(binroot / "python"))
    monkeypatch.setattr(service.shutil, "which", lambda _n: "/somewhere/else/gbfleet")
    assert service._this_gbfleet() == str(binroot / "gbfleet")


def test_the_interpreter_path_is_not_resolved_through_the_venv_symlink(tmp_path: Path,
                                                                      monkeypatch):
    """Measured: `.resolve()` on a venv's python lands in the INTERPRETER's bin.

    A venv `bin/python` is a symlink to the interpreter it was made from, so resolving it
    walks out of the venv entirely and the console script beside it disappears.
    """
    real = tmp_path / "toolchain" / "bin"
    real.mkdir(parents=True)
    (real / "python3").write_text("#!/bin/sh\n")
    (real / "python3").chmod(0o755)
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to(real / "python3")
    (venv / "gbfleet").write_text("#!/bin/sh\n")
    (venv / "gbfleet").chmod(0o755)
    monkeypatch.setattr(service.sys, "argv", ["pytest"])
    monkeypatch.setattr(service.sys, "executable", str(venv / "python"))
    monkeypatch.setattr(service.shutil, "which", lambda _n: None)
    assert service._this_gbfleet() == str(venv / "gbfleet")


def test_path_is_the_last_resort(tmp_path: Path, monkeypatch):
    found = tmp_path / "onpath" / "gbfleet"
    found.parent.mkdir()
    found.write_text("#!/bin/sh\n")
    monkeypatch.setattr(service.sys, "argv", ["pytest"])
    monkeypatch.setattr(service.sys, "executable", str(tmp_path / "nothing" / "python"))
    monkeypatch.setattr(service.shutil, "which", lambda _n: str(found))
    assert service._this_gbfleet() == str(found.resolve())


def test_no_gbfleet_anywhere_is_refused(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(service.sys, "argv", ["pytest"])
    monkeypatch.setattr(service.sys, "executable", str(tmp_path / "nothing" / "python"))
    monkeypatch.setattr(service.shutil, "which", lambda _n: None)
    with pytest.raises(Refused, match="absolute path to gbfleet"):
        service._this_gbfleet()


# --- Windows: Task Scheduler, never Session-0 (GRPH-852) -----------------------------


def test_windows_host_is_schtasks_not_the_old_refusal(monkeypatch):
    """`host()` today must not return the Session-0 refusal string; schtasks is the kind."""
    monkeypatch.setattr(service, "WINDOWS", True)
    monkeypatch.setattr(service.shutil, "which",
                        lambda n: r"C:\Windows\System32\schtasks.exe" if n == "schtasks"
                        else None)
    found = service.host()
    assert found.kind == "schtasks" and found.supervised
    assert "Session" not in found.why
    assert "no user-domain equivalent" not in found.why


def test_windows_without_schtasks_is_unknown_not_not_installed(monkeypatch):
    monkeypatch.setattr(service, "WINDOWS", True)
    monkeypatch.setattr(service.shutil, "which", lambda _n: None)
    found = service.host()
    assert found.kind == "" and not found.supervised
    state = service.status("drain")
    assert state.installed is None and state.running is None
    assert "schtasks" in state.detail or "Task Scheduler" in state.detail


def test_schtasks_rendering_is_task_scheduler_not_sc_exe(git_repo: Path):
    """Sabotage of 'write a sc.exe service instead' is refused by asserting the shape."""
    plan = _plan(git_repo, kind="schtasks", path_env=r"C:\vendors;C:\Windows\System32")
    rendered = service.render(plan)
    assert rendered[:2] == b"\xff\xfe", "schtasks /Create /XML wants UTF-16 LE"
    text = rendered.decode("utf-16")
    assert "schemas.microsoft.com/windows/2004/02/mit/task" in text
    assert "InteractiveToken" in text
    assert "LogonTrigger" in text
    assert "Session-0" in text or "Session 0" in text or "not a Session-0" in text
    assert "sc.exe" not in text.lower()
    assert "create service" not in text.lower()
    assert KEY not in text
    assert r"C:\vendors" in text
    assert "InteractiveToken" in text  # logged-on-only; not S4U / password


def test_schtasks_runner_sources_env_and_sets_path(git_repo: Path):
    plan = _plan(git_repo, kind="schtasks", path_env=r"C:\vendors;C:\Windows")
    runner = service._render_schtasks_runner(plan)
    assert service.API_KEY_ENV not in runner or KEY not in runner
    assert str(plan.env_path) in runner
    assert r"C:\vendors" in runner
    assert "until" in runner
    assert str(plan.binary) in runner


def test_schtasks_install_writes_runner_and_never_calls_sc(git_repo: Path, monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=20.0):
        calls.append(list(cmd))
        return 0, "SUCCESS"

    monkeypatch.setattr(service, "_run", fake_run)
    monkeypatch.setattr(service, "SETTLE", 0)
    monkeypatch.setattr(service, "status",
                        lambda name, kind="": service.Status(
                            name=name, kind="schtasks", installed=True, running=False,
                            last_exit=0, path_env=r"C:\vendors",
                            logon_policy=service.LOGON_WHEN_LOGGED_ON,
                            unit_path=service.unit_path_for(name, "schtasks")))
    plan = _plan(git_repo, kind="schtasks", path_env=r"C:\vendors")
    done = service.install(plan, KEY)
    assert plan.unit_path.exists()
    assert plan.env_path.read_text().strip() == f"{service.API_KEY_ENV}={KEY}"
    runner = service.runner_path_for(plan.name)
    assert runner.exists() and KEY not in runner.read_text()
    assert any(c[:2] == ["schtasks", "/Create"] for c in calls)
    assert not any(Path(c[0]).name.lower() in {"sc", "sc.exe"} for c in calls), (
        f"Session-0 sc.exe must never be the install path; got {calls!r}"
    )
    assert done.state is not None and done.state.idle
    assert any(service.LOGON_WHEN_LOGGED_ON in w for w in done.warnings)


def test_schtasks_uninstall_removes_env_and_runner(tmp_path: Path, git_repo: Path,
                                                     monkeypatch):
    unit = tmp_path / "gbfleet-drain.xml"
    env = tmp_path / "drain.env"
    runner = tmp_path / "drain.cmd"
    unit.write_bytes(b"x")
    env.write_text("GBFLEET_API_KEY=x\n")
    runner.write_text("@echo off\n")
    monkeypatch.setattr(service, "host", lambda: service.Host("schtasks"))
    monkeypatch.setattr(service, "unit_path_for", lambda n, k: unit)
    monkeypatch.setattr(service, "env_path_for", lambda n: env)
    monkeypatch.setattr(service, "runner_path_for", lambda n: runner)
    deleted: list[list[str]] = []
    monkeypatch.setattr(service, "_run",
                        lambda cmd, timeout=20.0: (deleted.append(list(cmd)) or (0, "")))
    removed = service.uninstall("drain")
    assert set(removed) == {str(unit), str(env), str(runner)}
    assert not unit.exists() and not env.exists() and not runner.exists()
    assert any(c[:2] == ["schtasks", "/Delete"] for c in deleted)


def test_schtasks_status_names_logon_policy_and_idle_is_not_a_fault(monkeypatch):
    idle = service.Status(
        name="drain", kind="schtasks", installed=True, running=False, last_exit=0,
        unit_path=Path("C:/x.xml"), logon_policy=service.LOGON_WHEN_LOGGED_ON,
        path_env=r"C:\vendors")
    assert idle.idle
    line = idle.line()
    assert "idle between runs" in line
    assert service.LOGON_WHEN_LOGGED_ON in line


def test_schtasks_query_ready_with_exit_0_is_idle(monkeypatch):
    listing = (
        "\nFolder: \\\n"
        "HostName:                             BOX\n"
        "TaskName:                             \\gbfleet-drain\n"
        "Status:                               Ready\n"
        "Last Result:                          0\n"
    )
    monkeypatch.setattr(service, "_run", lambda cmd, timeout=20.0: (0, listing))
    running, detail, last = service._running_schtasks("drain")
    assert running is False and last == 0
    assert "0" in detail


def test_schtasks_query_running_is_alive(monkeypatch):
    listing = (
        "Status:                               Running\n"
        "Last Result:                          267009\n"
    )
    monkeypatch.setattr(service, "_run", lambda cmd, timeout=20.0: (0, listing))
    running, _, last = service._running_schtasks("drain")
    assert running is True and last is None


def test_schtasks_load_uses_schtasks_create_xml(git_repo: Path, monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(service, "_run",
                        lambda cmd, timeout=20.0: (seen.append(list(cmd)) or (0, "ok")))
    plan = _plan(git_repo, kind="schtasks")
    plan.unit_path.parent.mkdir(parents=True, exist_ok=True)
    plan.unit_path.write_bytes(b"x")
    service._load_schtasks(plan)
    create = next(c for c in seen if c[:2] == ["schtasks", "/Create"])
    assert "/XML" in create and str(plan.unit_path) in create
    assert service.task_name(plan.name) in create
    assert any(c[:2] == ["schtasks", "/Run"] for c in seen)


def test_install_refuses_a_utf16_task_that_embeds_the_key(git_repo: Path, monkeypatch):
    """UTF-8 byte search would miss a key living in a UTF-16 task XML."""
    plan = _plan(git_repo, kind="schtasks")
    smuggled = f'<?xml version="1.0"?><Task><Arguments>--auth={KEY}</Arguments></Task>'
    monkeypatch.setattr(service, "render", lambda _p: smuggled.encode("utf-16"))
    with pytest.raises(Refused, match="contains the API key"):
        service.install(plan, KEY)
    assert not plan.unit_path.exists()
