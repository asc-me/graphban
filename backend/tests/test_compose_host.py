"""Compose host helper starts deploy.sh --local and does not take a Docker socket (P32)."""
from __future__ import annotations

import json
import os
import pathlib
import re
import socket
import sys
import threading
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))
import graphban_compose_host as ch  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]


def test_invalid_tag_is_refused():
    repo = REPO
    dest = pathlib.Path("/tmp/x")
    assert ch.start_apply(repo, dest, "../etc/passwd")["ok"] is False
    assert ch.start_apply(repo, dest, "")["ok"] is False
    assert ch.start_apply(repo, dest, "2026.09.2;rm -rf /")["ok"] is False


def test_apply_invokes_deploy_sh_local_dir_tag(tmp_path):
    """THE CALL. A helper that only unit-tests valid_tag while listen() never
    starts deploy.sh is the S6 defect: the button would 202 and nothing would rebuild.
    """
    repo = tmp_path / "src"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "deploy.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    dest = tmp_path / "box"
    seen: list[list[str]] = []

    def popen(cmd, **kw):
        seen.append(list(cmd))
        return None

    got = ch.start_apply(repo, dest, "2026.09.2", popen=popen)
    assert got == {"ok": True, "started": True, "tag": "2026.09.2"}
    assert seen, "deploy.sh was not started"
    cmd = seen[0]
    assert cmd[0].endswith("deploy.sh")
    assert "--local" in cmd
    assert "--dir" in cmd
    assert str(dest) in cmd
    assert cmd[-1] == "2026.09.2"
    assert "docker.sock" not in " ".join(cmd)


def test_handle_status_reports_the_clone_not_the_rsync_target(tmp_path):
    repo = tmp_path / "src"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "deploy.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    dest = tmp_path / "box"
    got = ch.handle({"op": "status"}, repo=repo, dest=dest)
    assert got["ok"] is True
    assert got["repo"] == str(repo)
    assert got["dir"] == str(dest)
    assert got["deploy"] is True


def _talk(sock: pathlib.Path, msg: dict) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(2)
        s.connect(str(sock))
        s.sendall((json.dumps(msg) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            buf += s.recv(1024)
    return json.loads(buf.decode())


def test_listen_round_trip(tmp_path, monkeypatch):
    """GRPH-892 CALL. bind() creates the path two lines before listen() accepts.
    Polling Path.exists() then connecting once races under load (CI -n auto).
    Inject that gap so exists()-then-one-connect fails here, and retrying the
    connect until it is accepted is the only green path. Do not sleep after exists().
    """
    repo = tmp_path / "src"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "deploy.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    dest = tmp_path / "box"
    # AF_UNIX paths are short; pytest's tmp_path is often too long on macOS.
    sock = pathlib.Path(f"/tmp/gb-apply-{os.getpid()}.sock")

    orig_listen = socket.socket.listen

    def listen_after_gap(self, *a, **k):
        time.sleep(0.25)
        return orig_listen(self, *a, **k)

    monkeypatch.setattr(socket.socket, "listen", listen_after_gap)

    t = threading.Thread(
        target=ch.listen, args=(sock, repo, dest), daemon=True)
    t.start()
    deadline = time.time() + 2.0
    st = None
    last_err: BaseException | None = None
    while time.time() < deadline:
        try:
            st = _talk(sock, {"op": "status"})
            break
        except (ConnectionRefusedError, FileNotFoundError, TimeoutError, OSError) as e:
            last_err = e
            time.sleep(0.02)
    try:
        assert st is not None, f"compose-host never accepted: {last_err!r}"
        assert st["ok"] is True
        assert st["deploy"] is True
    finally:
        sock.unlink(missing_ok=True)


def test_compose_mounts_the_helper_directory_not_docker_sock():
    """Sabotage the CALL: dropping the volume leaves apply=false forever on compose."""
    text = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    assert "GRAPHBAN_APPLY_DIR" in text
    assert "/run/graphban-apply" in text
    assert "docker.sock" not in text


def test_every_compose_service_restarts_after_a_host_reboot():
    """2026-11-09: ubuntu-srv rebooted and the stack stayed down (restart=no) while the
    box's other containers came back; every drain then exited on ConnectError. Sabotage:
    drop the restart line from any one service and this names it.
    """
    text = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    services = text.split("\nservices:\n", 1)[1].split("\nvolumes:\n", 1)[0]
    blocks = re.split(r"\n(?=  [a-z][a-z0-9_-]*:\n)", "\n" + services)
    names = {}
    for block in blocks:
        m = re.match(r"\s*([a-z][a-z0-9_-]*):\n", block)
        if m:
            names[m.group(1)] = re.search(r"^    restart: unless-stopped$", block, re.M) is not None
    assert set(names) >= {"db", "api", "web"}, names
    assert all(names.values()), {k: v for k, v in names.items() if not v}


def test_script_is_executable():
    script = REPO / "scripts" / "graphban_compose_host.py"
    assert script.is_file()
    assert script.stat().st_mode & 0o111


def test_apply_logs_deploy_output_so_a_failed_install_is_not_silent(tmp_path):
    """2026.09.5/.6: Install acked 202, deploy.sh died inside the docker build,
    and its output went to /dev/null — the box stayed on the old cut with nothing
    on it that said why. The log beside the socket is the trail.
    """
    repo = tmp_path / "src"
    (repo / "scripts").mkdir(parents=True)
    script = repo / "scripts" / "deploy.sh"
    script.write_text("#!/bin/sh\necho deploy-was-here\n", encoding="utf-8")
    script.chmod(0o755)
    dest = tmp_path / "box"
    log = tmp_path / "apply" / "deploy.log"

    got = ch.start_apply(repo, dest, "2026.09.2", log_path=log)
    assert got["ok"] is True

    # The child is detached by design; wait for it to land in the log.
    text = ""
    deadline = time.time() + 5
    while time.time() < deadline:
        if log.exists():
            text = log.read_text(encoding="utf-8")
            if "deploy-was-here" in text:
                break
        time.sleep(0.05)
    assert "apply 2026.09.2" in text, text
    assert "deploy-was-here" in text, text


def test_start_marker_written_before_script(tmp_path):
    repo = tmp_path / "src"
    (repo / "scripts").mkdir(parents=True)
    script = repo / "scripts" / "deploy.sh"
    script.write_text("#!/bin/sh\necho after\n")
    script.chmod(0o755)
    log = tmp_path / "apply" / "deploy.log"
    seen = {}

    def popen(cmd, **kw):
        seen["log"] = log.read_text()  # the marker is already there at spawn time
    ch.start_apply(repo, tmp_path / "box", "2026.11.1", log_path=log, popen=popen)
    import re
    assert re.search(r"^=== apply 2026\.11\.1 \S+ \d+$", seen["log"], re.M)


# ---- GRPH-1347: run the installed cut's deploy.sh; restart when the helper moved ----
import subprocess  # noqa: E402


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


@pytest.fixture
def tagged_repo(tmp_path):
    origin = tmp_path / "origin"
    (origin / "scripts").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(origin)], check=True)
    for tag in ("2026.11.1", "2026.11.2"):
        (origin / "scripts" / "deploy.sh").write_text(f"#!/bin/sh\necho {tag}\n")
        (origin / "scripts" / "deploy.sh").chmod(0o755)
        _git(origin, "add", "-A")
        _git(origin, "commit", "-qm", tag)
        _git(origin, "tag", tag)
    repo = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
    _git(repo, "checkout", "-q", "--detach", "2026.11.1")
    return repo


def _head(repo):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip()


def _tag_sha(repo, tag):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", f"{tag}^{{commit}}"],
                          capture_output=True, text=True, check=True).stdout.strip()


def test_apply_runs_the_installed_tags_deploy_sh(tagged_repo, tmp_path):
    seen = []
    log = tmp_path / "a" / "deploy.log"
    got = ch.start_apply(tagged_repo, tmp_path / "box", "2026.11.2",
                         popen=lambda cmd, **kw: seen.append(
                             (cmd[0], pathlib.Path(cmd[0]).read_text())),
                         log_path=log)
    assert got["ok"] is True
    assert "2026.11.2" in seen[0][1]
    assert _head(tagged_repo) == _tag_sha(tagged_repo, "2026.11.2")


def test_apply_refuses_a_dirty_repo(tagged_repo, tmp_path):
    (tagged_repo / "scripts" / "deploy.sh").write_text("#!/bin/sh\necho mine\n")
    calls = []
    log = tmp_path / "a" / "deploy.log"
    got = ch.start_apply(tagged_repo, tmp_path / "box", "2026.11.2",
                         popen=lambda *a, **k: calls.append(1), log_path=log)
    assert got["ok"] is False and "local changes" in got["error"]
    assert not calls
    assert "echo mine" in (tagged_repo / "scripts" / "deploy.sh").read_text()
    assert "local changes" in log.read_text()


def test_apply_refuses_a_missing_tag(tagged_repo, tmp_path):
    calls = []
    got = ch.start_apply(tagged_repo, tmp_path / "box", "2099.01.1",
                         popen=lambda *a, **k: calls.append(1))
    assert got["ok"] is False and "2099.01.1" in got["error"]
    assert not calls


def test_helper_reexecs_once_when_its_file_changed(tagged_repo, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ch, "STARTUP_SHA", "stale")
    ch.start_apply(tagged_repo, tmp_path / "box", "2026.11.2",
                   popen=lambda *a, **k: None, reexec=lambda: calls.append(1))
    assert calls == [1]


def test_helper_does_not_reexec_when_unchanged(tagged_repo, tmp_path):
    calls = []
    ch.start_apply(tagged_repo, tmp_path / "box", "2026.11.2",
                   popen=lambda *a, **k: None, reexec=lambda: calls.append(1))
    assert calls == []


def test_status_carries_helper_sha(tmp_path):
    got = ch.handle({"op": "status"}, repo=tmp_path, dest=tmp_path)
    assert got["helper_sha"] == ch.STARTUP_SHA and got["helper_sha"]


def test_compose_forwards_every_vapid_setting_to_the_api():
    """Sabotage the CALL: GRPH-1579 — api.environment is an explicit list, so a Settings
    field missing from it can never be set from .env on a Compose install."""
    import yaml

    from app.config import Settings

    env = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))[
        "services"
    ]["api"]["environment"]
    fields = [f for f in Settings.model_fields if f.startswith("vapid_")]
    assert len(fields) == 3
    for f in fields:
        assert f.upper() in env, f"{f.upper()} is not forwarded in api.environment"
        assert f"${{{f.upper()}:-" in env[f.upper()]
