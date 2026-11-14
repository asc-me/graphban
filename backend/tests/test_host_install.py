"""The host CLI composes S2–S5 into a first install (GRPH-601, PRD-27 S7).

The four platform scripts were each correct on their own. An operator who ran them in
sequence still did not have `graphban install`: nothing placed `root/current`, nothing
created the venv, the unit did not carry `GIT_SHA`, and upgrade replaced the operator's
`.env`. This file is the guard that those now happen, and that the CALL does them — a
helper nobody invokes is the same defect the S6 walk recorded.
"""
from __future__ import annotations

import pathlib
import plistlib
import sys
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))
import graphban_host as gh  # noqa: E402
import graphban_upgrade as up  # noqa: E402


@pytest.fixture()
def src(tmp_path: pathlib.Path) -> pathlib.Path:
    s = tmp_path / "src"
    (s / "backend").mkdir(parents=True)
    (s / "backend" / "pyproject.toml").write_text("[project]\nname='graphban-api'\n",
                                                   encoding="utf-8")
    (s / "backend" / ".env").write_text(
        "DATABASE_URL=postgresql://graphban@localhost/graphban\n"
        "JWT_SECRET=keep-this-secret-32-bytes-min\n",
        encoding="utf-8",
    )
    return s


def _ok_preflight(argv):
    return 0


def _ok_venv(root, backend, **kw):
    python = root / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("#!/bin/true\n", encoding="utf-8")
    return 0, python


def _ok_service(kind, path, payload, *, user_scope):
    """Accept the unit without writing it to a real supervisor directory.

    `write_service` returns `/Library/LaunchDaemons` or `/etc/systemd/system` by
    default. A test that mkdir's those is no longer a unit test.
    """
    return 0


# ---- first install --------------------------------------------------------------------------

def test_a_root_env_is_copied_into_the_working_directory(tmp_path):
    """THE COPY THIS FUNCTION EXISTS FOR. compose-style checkouts keep `.env` at
    the repo root, and the service reads `current/backend/.env`. copytree alone
    would leave it at `current/.env`, which pydantic-settings never opens."""
    src = tmp_path / "src"
    (src / "backend").mkdir(parents=True)
    (src / ".env").write_text(
        "DATABASE_URL=postgresql://graphban@localhost/graphban\n"
        "JWT_SECRET=keep-this-secret-32-bytes-min\n",
        encoding="utf-8",
    )
    root = tmp_path / "root"
    rc = gh.install(src, root, "abc1234", port=8000, host="127.0.0.1", user="alex",
                    user_scope=True, platform="darwin", preflight=_ok_preflight,
                    venv=_ok_venv, service=_ok_service)
    assert rc == 0
    env = (root / "current" / "backend" / ".env").read_text(encoding="utf-8")
    assert "keep-this-secret-32-bytes-min" in env


def test_install_places_the_release_and_the_env(src, tmp_path):
    root = tmp_path / "root"
    rc = gh.install(src, root, "abc1234", port=8234, host="127.0.0.1", user="alex",
                    user_scope=True, platform="darwin", preflight=_ok_preflight,
                    venv=_ok_venv, service=_ok_service)
    assert rc == 0
    assert (root / "current" / "backend" / "pyproject.toml").exists()
    env = (root / "current" / "backend" / ".env").read_text(encoding="utf-8")
    assert "keep-this-secret-32-bytes-min" in env
    assert (root / "current" / "GIT_SHA").read_text(encoding="utf-8").strip() == "abc1234"


def test_install_refuses_when_the_root_is_already_occupied(src, tmp_path, capsys):
    root = tmp_path / "root"
    (root / "current").mkdir(parents=True)
    rc = gh.install(src, root, "abc", port=8000, host="127.0.0.1", user="alex",
                    user_scope=True, platform="darwin", preflight=_ok_preflight,
                    venv=_ok_venv, service=_ok_service)
    assert rc == 1
    assert "already exists" in capsys.readouterr().err


def test_install_refuses_without_an_env_and_does_not_place_the_tree(tmp_path, capsys):
    src = tmp_path / "src"
    (src / "backend").mkdir(parents=True)
    root = tmp_path / "root"
    called = []

    rc = gh.install(src, root, "abc", port=8000, host="127.0.0.1", user="alex",
                    user_scope=True, platform="darwin",
                    preflight=lambda a: called.append("pf") or 0,
                    venv=lambda *a, **k: called.append("venv") or (0, pathlib.Path("x")),
                    service=lambda *a, **k: called.append("svc") or 0)
    assert rc == 2
    assert called == []
    assert not (root / "current").exists()
    assert ".env.example" in capsys.readouterr().err, (
        "missing-.env refusal must name .env.example — the file the operator copies"
    )


def test_install_refuses_an_env_without_a_database_url(tmp_path, capsys):
    src = tmp_path / "src"
    (src / "backend").mkdir(parents=True)
    (src / "backend" / ".env").write_text("JWT_SECRET=only-this\n", encoding="utf-8")
    rc = gh.install(src, tmp_path / "root", "abc", port=8000, host="127.0.0.1",
                    user="alex", user_scope=True, platform="darwin",
                    preflight=_ok_preflight, venv=_ok_venv, service=_ok_service)
    assert rc == 2
    assert "DATABASE_URL" in capsys.readouterr().err


def test_a_failed_venv_does_not_leave_an_occupied_root(src, tmp_path):
    """A half-install that occupies `current` makes the next retry refuse with
    'already exists' — the same 'looks installed' trap, for a directory."""
    root = tmp_path / "root"
    rc = gh.install(src, root, "abc", port=8000, host="127.0.0.1", user="alex",
                    user_scope=True, platform="darwin", preflight=_ok_preflight,
                    venv=lambda *a, **k: (1, None), service=_ok_service)
    assert rc == 1
    assert not (root / "current").exists()


def test_a_failed_preflight_does_not_place_the_tree(src, tmp_path):
    root = tmp_path / "root"
    rc = gh.install(src, root, "abc", port=8000, host="127.0.0.1", user="alex",
                    user_scope=True, platform="darwin",
                    preflight=lambda a: 3, venv=_ok_venv, service=_ok_service)
    assert rc == 3
    assert not (root / "current").exists()


def test_install_runs_preflight_against_the_env_dsn(src, tmp_path):
    seen = []
    gh.install(src, tmp_path / "root", "abc", port=8234, host="10.0.0.1", user="alex",
               user_scope=True, platform="darwin",
               preflight=lambda a: seen.extend(a) or 0,
               venv=_ok_venv, service=_ok_service)
    assert "--database-url" in seen
    assert "postgresql://graphban@localhost/graphban" in seen
    assert "8234" in seen


def test_install_defaults_to_preflight_main():
    """The helper default. Without this, gh.main can omit the kwarg and still
    bind a stub if someone changes the default."""
    import inspect
    default = inspect.signature(gh.install).parameters["preflight"].default
    assert default is gh.pf.main


def test_the_install_cli_binds_preflight_main(src, tmp_path, monkeypatch):
    """THE CALL (GRPH-578 bounce). Tests drive gh.install(..., preflight=stub).
    The operator types `python3 scripts/graphban_host.py install`, which is
    gh.main → install(...) with no override. Passing preflight=lambda a: 0
    THERE left test_preflight + test_host_install + test_install_layout green.
    """
    seen: dict = {}

    def capture(*_a, **k):
        seen["kwargs"] = k
        return 0

    monkeypatch.setattr(gh, "install", capture)
    rc = gh.main([
        "install",
        "--root", str(tmp_path / "root"),
        "--from", str(src),
        "--sha", "abc1234",
        "--user-domain",
    ])
    assert rc == 0
    bound = seen["kwargs"].get("preflight", gh.pf.main)
    assert bound is gh.pf.main, (
        "gh.main overrode preflight — the operator CLI is no longer bound to pf.main"
    )


def test_install_creates_the_venv_from_the_placed_backend(src, tmp_path):
    seen = []

    def venv(root, backend, **kw):
        seen.append(backend)
        return _ok_venv(root, backend)

    gh.install(src, tmp_path / "root", "abc", port=8000, host="127.0.0.1", user="alex",
               user_scope=True, platform="darwin", preflight=_ok_preflight,
               venv=venv, service=_ok_service)
    assert seen == [tmp_path / "root" / "current" / "backend"]


# ---- the unit the install writes ------------------------------------------------------------

def test_the_darwin_unit_carries_the_revision(src, tmp_path):
    """THE DEFECT THIS FILE EXISTS FOR alongside .env preserve. Without GIT_SHA in the
    unit, `/health` reports `unknown` and S5's identity check can never succeed."""
    root = tmp_path / "root"
    written = {}

    def service(kind, path, payload, *, user_scope):
        written["kind"] = kind
        written["payload"] = payload
        return _ok_service(kind, path, payload, user_scope=user_scope)

    gh.install(src, root, "abc1234", port=8234, host="127.0.0.1", user="alex",
               user_scope=True, platform="darwin", preflight=_ok_preflight,
               venv=_ok_venv, service=service)
    plist = plistlib.loads(written["payload"])
    assert plist["EnvironmentVariables"]["GIT_SHA"] == "abc1234"
    assert plist["EnvironmentVariables"]["PORT"] == "8234"
    assert "UserName" not in plist, "a LaunchAgent must not name a user"
    assert plist["WorkingDirectory"].endswith("/current/backend")


def test_the_linux_unit_carries_the_revision(src, tmp_path):
    written = {}

    def service(kind, path, payload, *, user_scope):
        written.update(kind=kind, payload=payload, user_scope=user_scope)
        return _ok_service(kind, path, payload, user_scope=user_scope)

    gh.install(src, tmp_path / "root", "abc1234", port=8000, host="127.0.0.1",
               user="graphban", user_scope=False, platform="linux",
               preflight=_ok_preflight, venv=_ok_venv, service=service)
    assert written["kind"] == "unit"
    assert "Environment=GIT_SHA=abc1234" in written["payload"]
    assert "User=graphban" in written["payload"]
    assert not written["user_scope"]


def test_the_linux_user_scope_unit_omits_user(src, tmp_path):
    """write_service is the host dispatcher. Linux tests only used user_scope=False,
    so dropping user_scope= from the unit_text CALL left User=graphban on a --user
    unit — the same 216/GROUP crash loop (GRPH-583 bounce).
    """
    written = {}

    def service(kind, path, payload, *, user_scope):
        written.update(kind=kind, payload=payload, user_scope=user_scope)
        return _ok_service(kind, path, payload, user_scope=user_scope)

    gh.install(src, tmp_path / "root", "abc1234", port=8000, host="127.0.0.1",
               user="alex", user_scope=True, platform="linux",
               preflight=_ok_preflight, venv=_ok_venv, service=service)
    assert written["kind"] == "unit"
    assert written["user_scope"] is True
    assert "User=" not in written["payload"], (
        "the host dispatcher put User= on a --user-scope Linux unit"
    )


BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "scripts"))
import lock_constraints as lc  # noqa: E402


def _release_backend(tmp_path: pathlib.Path) -> pathlib.Path:
    """A release's backend/ as the native host sees it: the real lock and the real tool."""
    backend = tmp_path / "release" / "backend"
    (backend / "scripts").mkdir(parents=True)
    (backend / "uv.lock").write_bytes((BACKEND / "uv.lock").read_bytes())
    (backend / "scripts" / "lock_constraints.py").write_bytes(
        (BACKEND / "scripts" / "lock_constraints.py").read_bytes())
    return backend


class _Venv:
    """A venv that behaves the way pip does about what is already there.

    `pip install -e backend` leaves a dependency alone when the installed version satisfies
    the project's range — that is how a box upgraded from 2026.11.20 kept dnspython 2.9.0.
    Under `-c constraints`, only a version equal to the pin satisfies, so pip moves it.
    The `write` and `check` steps run the real tool against the real lock.
    """

    def __init__(self, root: pathlib.Path, have: dict[str, str]):
        self.root, self.have, self.seen = root, dict(have), []

    def __call__(self, cmd, **kw):
        self.seen.append(cmd)
        ok = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[1:3] == ["-m", "venv"]:
            python = self.root / "venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("x", encoding="utf-8")
        elif cmd[1:4] == ["-m", "pip", "install"]:
            if "-c" in cmd:
                text = pathlib.Path(cmd[cmd.index("-c") + 1]).read_text(encoding="utf-8")
                pins = dict(line.split("==") for line in text.split())
                for name in self.have:
                    if name in pins:
                        self.have[name] = pins[name]
        elif cmd[1].endswith("lock_constraints.py") and cmd[2] == "write":
            lc.write(pathlib.Path(cmd[3]), pathlib.Path(cmd[4]))
        elif cmd[1].endswith("lock_constraints.py") and cmd[2] == "check":
            bad = lc.drift(pathlib.Path(cmd[3]), self.have)
            return types.SimpleNamespace(returncode=1 if bad else 0, stdout="",
                                         stderr="\n".join(bad))
        return ok


def test_ensure_venv_creates_then_installs_the_backend(tmp_path):
    """Skip the pip and the venv is a directory nobody can import `app` from."""
    backend = _release_backend(tmp_path)
    venv = _Venv(tmp_path, {})
    rc, python = gh.ensure_venv(tmp_path, backend, runner=venv)
    assert rc == 0 and python is not None
    assert any("-m" in c and "venv" in c for c in venv.seen)
    pip = next(c for c in venv.seen if "pip" in c)
    assert "-e" in pip and str(backend) in pip


def test_an_upgraded_venv_is_moved_to_the_lock(tmp_path):
    """GRPH-1615. A box upgraded from 2026.11.20 holds dnspython 2.9.0; the lock pins 2.9.2
    for CVE-2026-128089. After the venv refresh every locked package is the locked version.

    Sabotage: drop `-c` from the pip call and the venv keeps 2.9.0 — `check` refuses and
    this fails on rc. Drop the `check` call too and the version assertion fails."""
    backend = _release_backend(tmp_path)
    pins = lc.locked(backend / "uv.lock")
    assert pins["dnspython"] == "2.9.2"
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    (tmp_path / "venv" / "bin" / "python").write_text("x", encoding="utf-8")
    stale = {name: "0.0.1" for name in pins} | {"dnspython": "2.9.0"}
    venv = _Venv(tmp_path, stale)

    rc, python = gh.ensure_venv(tmp_path, backend, runner=venv)

    assert rc == 0, "the venv refresh refused"
    assert venv.have["dnspython"] == "2.9.2"
    assert venv.have == {name: pins[name] for name in stale}


def test_a_venv_that_still_differs_from_the_lock_fails_the_install(tmp_path):
    """pip exiting 0 is not the proof — the check after it is. Here pip ignores the
    constraints; the install must refuse, which is what rolls an upgrade back."""
    backend = _release_backend(tmp_path)
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    (tmp_path / "venv" / "bin" / "python").write_text("x", encoding="utf-8")
    venv = _Venv(tmp_path, {"dnspython": "2.9.0"})
    real = venv.__call__

    def pip_ignores_constraints(cmd, **kw):
        if cmd[1:4] == ["-m", "pip", "install"]:
            venv.seen.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return real(cmd, **kw)

    rc, python = gh.ensure_venv(tmp_path, backend, runner=pip_ignores_constraints)
    assert (rc, python) == (1, None)


def test_a_release_without_a_lock_is_refused(tmp_path, capsys):
    """No lock is not 'nothing to pin' — it is an install the scan never saw."""
    backend = _release_backend(tmp_path)
    (backend / "uv.lock").unlink()
    venv = _Venv(tmp_path, {})
    rc, python = gh.ensure_venv(tmp_path, backend, runner=venv)
    assert (rc, python) == (1, None)
    assert not any("pip" in c for c in venv.seen)
    assert "uv.lock" in capsys.readouterr().err


def test_lock_constraints_pins_every_registry_package_and_skips_the_project():
    pins = lc.locked(BACKEND / "uv.lock")
    text = lc.constraints(BACKEND / "uv.lock")
    assert "graphban-api" not in pins
    assert text.splitlines() == [f"{n}=={pins[n]}" for n in sorted(pins)]


def test_lock_drift_names_a_mismatch_and_strict_names_an_unlocked_package():
    lock = BACKEND / "uv.lock"
    pins = lc.locked(lock)
    assert lc.drift(lock, {"dnspython": pins["dnspython"], "pip": "25.0"}, strict=True) == []
    assert lc.drift(lock, {"dnspython": "2.9.0"}) == [
        f"dnspython 2.9.0 installed, uv.lock pins {pins['dnspython']}"]
    assert lc.drift(lock, {"left-pad": "1.0"}) == []
    assert lc.drift(lock, {"left-pad": "1.0"}, strict=True) == [
        "left-pad 1.0 installed, not in uv.lock (the scan never saw it)"]
    assert lc.drift(lock, {"graphban-api": "2026.11.23"}, strict=True) == []


def test_the_image_installs_against_the_lock_and_checks_it():
    """The Dockerfile is the other shipped install. It must build its constraints from the
    lock, install under them, and fail the build if the image still differs (strict: a
    fresh image has no excuse for a package the lock does not name)."""
    df = (BACKEND / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY uv.lock" in df and "COPY scripts/lock_constraints.py" in df
    write = df.index("lock_constraints.py write uv.lock")
    install = df.index("pip install --no-cache-dir . -c")
    check = df.index("lock_constraints.py check uv.lock --strict")
    assert write < install < check


# ---- upgrade composition --------------------------------------------------------------------

def test_upgrade_keeps_the_operators_env(tmp_path, monkeypatch):
    monkeypatch.setattr(up.time, "sleep", lambda s: None)
    root = tmp_path / "root"
    (root / "current" / "backend").mkdir(parents=True)
    (root / "current" / "backend" / ".env").write_text("JWT_SECRET=keep-me\n", encoding="utf-8")
    (root / "current" / "GIT_SHA").write_text("oldsha\n", encoding="utf-8")
    (root / "current" / "marker").write_text("old", encoding="utf-8")
    incoming = tmp_path / "incoming"
    (incoming / "backend").mkdir(parents=True)
    (incoming / "backend" / ".env").write_text("JWT_SECRET=from-the-tarball\n", encoding="utf-8")
    (incoming / "marker").write_text("new", encoding="utf-8")

    up.upgrade(root, incoming, "newsha", base="http://x", python=pathlib.Path("py"),
               restart=lambda a: None,
               probe=lambda b, **k: {"status": "ok", "git_sha": "newsha", "db": "ok"},
               web=lambda b, **k: "", head=lambda r, p: "0093")

    assert (root / "current" / "backend" / ".env").read_text(encoding="utf-8") == "JWT_SECRET=keep-me\n"
    assert (root / "current" / "GIT_SHA").read_text(encoding="utf-8").strip() == "newsha"


def test_a_failed_venv_refresh_rolls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(up.time, "sleep", lambda s: None)
    root = tmp_path / "root"
    (root / "current").mkdir(parents=True)
    (root / "current" / "marker").write_text("old", encoding="utf-8")
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    (incoming / "marker").write_text("new", encoding="utf-8")

    rc = up.upgrade(root, incoming, "newsha", base="http://x", python=pathlib.Path("py"),
                    restart=lambda a: None, sync_deps=lambda r, c: 1,
                    probe=lambda b, **k: {"status": "ok", "git_sha": "newsha"},
                    web=lambda b, **k: "", head=lambda r, p: "0093")

    assert rc == up.EXIT_ROLLED_BACK
    assert (root / "current" / "marker").read_text(encoding="utf-8") == "old"


def test_rewire_gets_the_new_sha_and_the_old_one_on_rollback(tmp_path, monkeypatch):
    monkeypatch.setattr(up.time, "sleep", lambda s: None)
    root = tmp_path / "root"
    (root / "current").mkdir(parents=True)
    (root / "current" / "GIT_SHA").write_text("oldsha\n", encoding="utf-8")
    (root / "current" / "marker").write_text("old", encoding="utf-8")
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    (incoming / "marker").write_text("new", encoding="utf-8")
    seen: list[str] = []

    up.upgrade(root, incoming, "newsha", base="http://x", python=pathlib.Path("py"),
               restart=lambda a: None, rewire=seen.append,
               probe=lambda b, **k: None, web=lambda b, **k: "", head=lambda r, p: "")

    assert seen[0] == "newsha"
    assert seen[-1] == "oldsha"


def test_platform_restart_targets_the_user_domain_on_darwin(monkeypatch):
    seen = []
    monkeypatch.setattr(up.subprocess, "run",
                        lambda cmd, **k: seen.append(cmd) or types.SimpleNamespace(
                            returncode=0, stdout="", stderr=""))
    restart = up.platform_restart(user_scope=True, platform="darwin")
    restart("stop")
    joined = " ".join(seen[0])
    assert "gui/" in joined
    assert "system/" not in joined


def test_platform_restart_uses_systemctl_user_on_linux(monkeypatch):
    seen = []
    monkeypatch.setattr(up.subprocess, "run",
                        lambda cmd, **k: seen.append(cmd) or types.SimpleNamespace(
                            returncode=0, stdout="", stderr=""))
    restart = up.platform_restart(user_scope=True, platform="linux")
    restart("stop")
    assert seen[0][:2] == ["systemctl", "--user"]


def test_platform_restart_system_domain_is_still_the_default(monkeypatch):
    seen = []
    monkeypatch.setattr(up.subprocess, "run",
                        lambda cmd, **k: seen.append(cmd) or types.SimpleNamespace(
                            returncode=0, stdout="", stderr=""))
    restart = up.platform_restart(user_scope=False, platform="linux")
    restart("start")
    assert seen[0] == ["systemctl", "start", "graphban.service"]


# ---- dispatcher CALLs (GRPH-601 bounce) -----------------------------------------------------

def test_the_install_cli_passes_the_detected_sha(src, tmp_path, monkeypatch):
    """gh.main → install(sha=…). Passing sha="" at the CALL left every helper green."""
    seen: dict = {}

    def capture(_src, _root, sha, **_k):
        seen["sha"] = sha
        return 0

    monkeypatch.setattr(gh, "install", capture)
    monkeypatch.setattr(gh, "detect_sha", lambda _src, explicit: explicit or "from-git")
    rc = gh.main([
        "install",
        "--root", str(tmp_path / "root"),
        "--from", str(src),
        "--sha", "abc1234",
        "--user-domain",
    ])
    assert rc == 0
    assert seen["sha"] == "abc1234"


def test_upgrade_defaults_to_preserve_env():
    import inspect
    default = inspect.signature(up.upgrade).parameters["preserve"].default
    assert default is up.preserve_env


def test_host_upgrade_binds_preserve_env_and_user_scope_restart(tmp_path, monkeypatch):
    """THE CALL. Skip preserve only inside up.upgrade: 1 failed. Skip it at
    host_upgrade (or force system-domain restart there): suite stayed green."""
    seen: dict = {}

    def capture_upgrade(*_a, **k):
        seen["upgrade"] = k
        return 0

    def capture_restart(**k):
        seen["restart"] = k
        return lambda _action: None

    monkeypatch.setattr(gh.up, "upgrade", capture_upgrade)
    monkeypatch.setattr(gh.up, "platform_restart", capture_restart)
    rc = gh.host_upgrade(
        tmp_path / "root", tmp_path / "release", "newsha",
        base="http://127.0.0.1:8000", port=8000, host="127.0.0.1",
        user="alex", user_scope=True, platform="darwin",
    )
    assert rc == 0
    bound = seen["upgrade"].get("preserve", up.preserve_env)
    assert bound is up.preserve_env, (
        "host_upgrade overrode preserve — operator .env is no longer kept on the CALL"
    )
    assert seen["restart"].get("user_scope") is True, (
        "host_upgrade forced the system domain on a --user-scope upgrade"
    )


def test_host_uninstall_binds_user_scope_restart(tmp_path, monkeypatch):
    seen: dict = {}

    def capture_restart(**k):
        seen["restart"] = k
        return lambda _action: None

    monkeypatch.setattr(gh.up, "platform_restart", capture_restart)
    monkeypatch.setattr(gh.gs, "uninstall", lambda *_a, **_k: None)
    monkeypatch.setattr(gh.up, "uninstall", lambda *_a, **_k: 0)
    rc = gh.host_uninstall(tmp_path / "root", user_scope=True, platform="darwin")
    assert rc == 0
    assert seen["restart"].get("user_scope") is True


def test_the_upgrade_cli_dispatches_to_host_upgrade(tmp_path, monkeypatch):
    seen: dict = {}

    def capture(*_a, **k):
        seen["kwargs"] = k
        return 0

    monkeypatch.setattr(gh, "host_upgrade", capture)
    release = tmp_path / "release"
    release.mkdir()
    rc = gh.main([
        "upgrade",
        "--root", str(tmp_path / "root"),
        "--release", str(release),
        "--sha", "abc1234",
        "--user-domain",
    ])
    assert rc == 0
    assert seen["kwargs"].get("user_scope") is True


def test_the_uninstall_cli_dispatches_to_host_uninstall(tmp_path, monkeypatch):
    seen: dict = {}

    def capture(*_a, **k):
        seen["kwargs"] = k
        return 0

    monkeypatch.setattr(gh, "host_uninstall", capture)
    rc = gh.main([
        "uninstall",
        "--root", str(tmp_path / "root"),
        "--user-domain",
    ])
    assert rc == 0
    assert seen["kwargs"].get("user_scope") is True
