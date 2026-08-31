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


def test_install_refuses_without_an_env_and_does_not_place_the_tree(tmp_path):
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


def test_ensure_venv_creates_then_installs_the_backend(tmp_path):
    """Skip the pip and the venv is a directory nobody can import `app` from."""
    seen = []

    def runner(cmd, **kw):
        seen.append(cmd)
        if "venv" in cmd:
            python = tmp_path / "venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("x", encoding="utf-8")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    rc, python = gh.ensure_venv(tmp_path, tmp_path / "backend", runner=runner)
    assert rc == 0 and python is not None
    assert any("-m" in c and "venv" in c for c in seen)
    pip = next(c for c in seen if "pip" in c)
    assert "-e" in pip and str(tmp_path / "backend") in pip


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
