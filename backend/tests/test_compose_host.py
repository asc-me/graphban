"""Compose host helper starts deploy.sh --local and does not take a Docker socket (P32)."""
from __future__ import annotations

import json
import os
import pathlib
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
