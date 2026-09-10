"""GRPH-844: a drain that outlives the terminal that started it.

Most of this file is about what the unit MUST NOT contain and what `status` must not say.
The install itself is one `launchctl` or `systemctl` call and is walked on a real box; what
tests can hold onto is the set of facts that were each paid for once by a service that
installed cleanly and did nothing — a unit carrying a key, a `User=` that kills every start,
an empty PATH that resolves no vendor, and a boolean that reports "not running" for a host
nobody could ask.

Both renderings are exercised on every platform by passing `kind=` explicitly. Without that
the macOS run would check the plist and CI would check the unit, and neither would ever check
the other — the same hole `test_hostos` names.
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


def _args(repo: Path) -> list[str]:
    return ["--repo", str(repo), "--server", "http://gb.invalid", "--project", "p",
            "--adapter", "claude"]


# --- what must never reach a unit file ---------------------------------------------


@pytest.mark.parametrize("kind", ["systemd", "launchd"])
def test_no_rendering_carries_the_api_key(git_repo: Path, kind, monkeypatch):
    monkeypatch.setenv(service.API_KEY_ENV, KEY)
    plan = service.make_plan(_args(git_repo), kind=kind)
    rendered = service.render(plan)
    assert KEY.encode() not in rendered
    assert service.secrets_in(rendered) == []


@pytest.mark.parametrize("kind", ["systemd", "launchd"])
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


def test_install_refuses_a_unit_that_would_leak(git_repo: Path, monkeypatch):
    """Sabotage the CALL: a rendering that leaks must be refused, not merely detected."""
    plan = service.make_plan(_args(git_repo), kind="systemd")
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
    plan = service.make_plan(_args(git_repo), kind="systemd")
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
    text = service.render(service.make_plan(_args(git_repo), kind="systemd")).decode()
    assert "\nUser=" not in text


def test_the_launchd_job_names_no_username(git_repo: Path):
    """`UserName` is a LaunchDaemon key; on an agent it asks for a privilege launchd refuses."""
    job = plistlib.loads(service.render(service.make_plan(_args(git_repo), kind="launchd")))
    assert "UserName" not in job
    # Both, or the service looks installed and is not running.
    assert job["RunAtLoad"] is True and job["KeepAlive"] is True


# --- the PATH, which is what a fleet service is actually for ------------------------


@pytest.mark.parametrize("kind", ["systemd", "launchd"])
def test_the_installing_shell_path_is_carried_into_the_unit(git_repo: Path, kind):
    """No vendor CLI is on a supervisor's default PATH, and the vendors are the point."""
    plan = service.make_plan(_args(git_repo), kind=kind, path_env="/opt/vendors:/usr/bin")
    rendered = service.render(plan).decode("utf-8", "replace")
    assert "/opt/vendors" in rendered


@pytest.mark.parametrize("kind", ["systemd", "launchd"])
def test_the_path_is_read_back_from_what_was_written(tmp_path: Path, git_repo: Path, kind,
                                                     monkeypatch):
    """`status` prints the unit's PATH, so it has to parse the file rather than guess."""
    monkeypatch.setattr(service, "unit_path_for",
                        lambda name, k: tmp_path / f"{name}.{k}")
    plan = service.make_plan(_args(git_repo), kind=kind, path_env="/opt/vendors:/usr/bin")
    plan.unit_path.write_bytes(service.render(plan))
    assert service._path_in(plan.unit_path, kind) == "/opt/vendors:/usr/bin"


# --- refusals, each one a service that would have run and done nothing --------------


def test_an_empty_install_is_refused(git_repo: Path):
    with pytest.raises(Refused, match="nothing to run"):
        service.make_plan([], kind="systemd")


def test_a_missing_repo_flag_is_refused(git_repo: Path):
    with pytest.raises(Refused, match="--repo"):
        service.make_plan(["--server", "http://gb.invalid"], kind="systemd")


def test_a_relative_repo_is_refused(git_repo: Path):
    """A unit has no working directory worth inheriting; the service would drain elsewhere."""
    with pytest.raises(Refused, match="absolute"):
        service.make_plan(["--repo", "."], kind="systemd")


def test_a_repo_that_is_not_a_git_repository_is_refused(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(Refused, match="git repository"):
        service.make_plan(["--repo", str(plain)], kind="systemd")


def test_a_repo_that_does_not_exist_is_refused(tmp_path: Path):
    with pytest.raises(Refused, match="does not exist"):
        service.make_plan(["--repo", str(tmp_path / "nope")], kind="systemd")


def test_a_zero_interval_is_refused(git_repo: Path):
    with pytest.raises(Refused, match="at least 1 second"):
        service.make_plan(_args(git_repo), kind="systemd", every=0)


def test_install_without_a_key_is_refused_before_anything_is_written(git_repo: Path):
    plan = service.make_plan(_args(git_repo), kind="systemd")
    with pytest.raises(Refused, match=service.API_KEY_ENV):
        service.install(plan, "")
    assert not plan.unit_path.exists()


# --- the repo path the unit ends up carrying ---------------------------------------


def test_the_repo_is_resolved_into_the_unit(git_repo: Path):
    """`/srv/repo/fleet/..` is absolute, passes every check, and reads as another directory."""
    plan = service.make_plan(["--repo", f"{git_repo}/./"], kind="systemd")
    assert plan.repo == git_repo.resolve()
    assert str(plan.repo) in plan.until_args, "the resolved path must reach `until` too"


def test_the_plan_names_the_lock_the_service_will_hold(git_repo: Path):
    """Per git COMMON DIR: a running drain refuses interactive `up` on the whole clone."""
    plan = service.make_plan(_args(git_repo), kind="systemd")
    assert plan.common_dir == git_repo.resolve()


# --- the environment file ------------------------------------------------------------


def test_the_key_file_holds_the_key_and_only_this_user_may_read_it(git_repo: Path, tmp_path,
                                                                   monkeypatch):
    monkeypatch.setattr(service, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(service, "env_path_for", lambda name: tmp_path / "cfg" / f"{name}.env")
    plan = service.make_plan(_args(git_repo), kind="systemd")
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


@pytest.mark.parametrize("kind", ["systemd", "launchd"])
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
    plan = service.make_plan(_args(git_repo), kind="systemd")
    # No exception: the guard does not fire, and the install proceeds to write.
    service.install(plan, "x")
    assert plan.unit_path.exists()
    assert plan.env_path.read_text().strip().endswith("=x")


def test_a_real_length_key_still_trips_it(git_repo: Path, monkeypatch):
    plan = service.make_plan(_args(git_repo), kind="systemd")
    monkeypatch.setattr(service, "render", lambda _p: f"ExecStart=/x --auth={KEY}\n".encode())
    with pytest.raises(Refused, match="contains the API key"):
        service.install(plan, KEY)
