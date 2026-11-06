#!/usr/bin/env python3
"""Host helper so Settings → Updates → Install can apply a compose cut (P32).

The API container does not get a Docker socket. This process runs on the host,
listens on a unix socket, and starts `scripts/deploy.sh --local --dir <compose> <tag>`
from a real git clone. The rsync target (`~/agentledger` on ubuntu-srv) has no
`.git` — deploy.sh cannot fetch from there.

    python3 scripts/graphban_compose_host.py listen \\
      --repo ~/graphban-src --dir ~/agentledger \\
      --socket ~/.graphban-apply/apply.sock

Point compose at the socket directory (`GRAPHBAN_APPLY_DIR` in the host `.env`).
The API looks for `/run/graphban-apply/apply.sock` inside the container.

JWT in the API is the operator gate. This helper trusts whoever can write to the
socket — keep it 0600 and do not put it on a TCP port.

deploy.sh runs detached and its output is appended to `deploy.log` beside the
socket. An Install that acks but never lands on the new cut is a build failure;
that log is where it says so.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import time

TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}$")
OPS = ("status", "apply")


def file_sha(path: pathlib.Path) -> str:
    """Short hash of a file; "" when unreadable. Used to notice the helper's own script moving."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except OSError:
        return ""


HELPER_PATH = pathlib.Path(__file__).resolve()
STARTUP_SHA = file_sha(HELPER_PATH)


def _git(repo: pathlib.Path, *args: str) -> tuple[int, str]:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def checkout_tag(repo: pathlib.Path, tag: str) -> tuple[bool, str, str]:
    """Move --repo to `tag` so the deploy.sh that runs is the cut's own.

    Returns (ok, error, output). Never stashes or resets: a dirty tree refuses.
    A --repo that is not a git clone is left alone (nothing to move).
    """
    if not (repo / ".git").exists():
        return True, "", ""
    code, out = _git(repo, "status", "--porcelain")
    if code != 0:
        return False, f"git status failed in --repo: {out}", out
    if out:
        return False, f"--repo has local changes: {out}", out
    code, fout = _git(repo, "fetch", "--tags", "origin")
    if code != 0:
        return False, f"git fetch failed in --repo: {fout}", fout
    code, _ = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}")
    if code != 0:
        return False, f"tag {tag} does not exist in --repo after fetch", fout
    code, cout = _git(repo, "checkout", "--detach", f"refs/tags/{tag}")
    if code != 0:
        return False, f"git checkout {tag} failed: {cout}", f"{fout}\n{cout}"
    return True, "", f"{fout}\n{cout}".strip()


def valid_tag(tag: str) -> bool:
    return bool(TAG_RE.fullmatch(tag or "")) and ".." not in tag


def start_apply(repo: pathlib.Path, dest: pathlib.Path, tag: str, *,
                popen=subprocess.Popen, log_path: pathlib.Path | None = None,
                reexec=None) -> dict:
    """Ack immediately. deploy.sh recreates the API container; waiting on it
    would drop the Install request on the floor and look like a failed click.

    deploy.sh output is APPENDED to `log_path` when given, not /dev/null: the
    container rebuild is the step that can fail, and the 2026.09.5/.6 cuts died
    there with the output discarded, so the box stayed on the old cut with
    nothing on it that said why. `None` keeps /dev/null (tests that inject a
    fake popen).
    """
    if not valid_tag(tag):
        return {"ok": False, "error": "invalid tag"}
    dest.mkdir(parents=True, exist_ok=True)
    out = subprocess.DEVNULL
    log = None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = log_path.open("ab", buffering=0)
        log.write(
            f"\n=== apply {tag} {time.strftime('%Y-%m-%dT%H:%M:%S%z')} {int(time.time())}\n"
            .encode("utf-8"))
        out = log
    try:
        ok, err, text = checkout_tag(repo, tag)
        if log is not None and text:
            log.write((text + "\n").encode("utf-8"))
        if not ok:
            if log is not None:
                log.write(f"refused: {err}\n".encode("utf-8"))
            return {"ok": False, "error": err}
        script = repo / "scripts" / "deploy.sh"
        if not script.is_file():
            return {"ok": False, "error": "deploy.sh missing in --repo"}
        popen(
            [str(script), "--local", "--dir", str(dest), tag],
            cwd=str(repo),
            start_new_session=True,
            stdout=out,
            stderr=subprocess.STDOUT,
        )
    finally:
        if log is not None:
            log.close()  # the child holds its own descriptor
    # The checkout may have replaced this very file; run the new one from now on.
    if reexec is not None and file_sha(HELPER_PATH) != STARTUP_SHA:
        reexec()
    return {"ok": True, "started": True, "tag": tag}


def handle(msg: dict, *, repo: pathlib.Path, dest: pathlib.Path,
           apply=start_apply, log_path: pathlib.Path | None = None,
           reexec=None) -> dict:
    op = msg.get("op")
    if op == "status":
        return {
            "ok": True,
            "repo": str(repo),
            "dir": str(dest),
            "deploy": (repo / "scripts" / "deploy.sh").is_file(),
            "helper_sha": STARTUP_SHA,
        }
    if op == "apply":
        kw = {"reexec": reexec} if reexec is not None else {}
        return apply(repo, dest, str(msg.get("tag") or ""), log_path=log_path, **kw)
    return {"ok": False, "error": f"unknown op {op!r}"}


def _read_msg(conn: socket.socket, *, limit: int = 8192) -> dict:
    buf = b""
    while b"\n" not in buf and len(buf) < limit:
        chunk = conn.recv(1024)
        if not chunk:
            break
        buf += chunk
    if not buf:
        return {"op": ""}
    return json.loads(buf.decode("utf-8"))


def _execv_self() -> None:
    os.execv(sys.executable, [sys.executable, *sys.argv])


def listen(sock_path: pathlib.Path, repo: pathlib.Path, dest: pathlib.Path, *,
           execv=_execv_self) -> int:
    sock_path = sock_path.expanduser()
    repo = repo.expanduser().resolve()
    dest = dest.expanduser().resolve()
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    if sock_path.exists():
        sock_path.unlink()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    os.chmod(sock_path, 0o600)
    srv.listen(8)
    # Where a failed Install explains itself. deploy.sh acks and runs detached,
    # so the apply request looks successful either way; the log is the only place
    # the box records what the rebuild actually said.
    log_path = sock_path.parent / "deploy.log"
    print(f"compose-host listening on {sock_path} repo={repo} dir={dest}", flush=True)
    while True:
        conn, _ = srv.accept()
        restart: list[bool] = []
        with conn:
            try:
                msg = _read_msg(conn)
                out = handle(msg, repo=repo, dest=dest, log_path=log_path,
                             reexec=lambda: restart.append(True))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
                out = {"ok": False, "error": type(e).__name__}
            conn.sendall((json.dumps(out) + "\n").encode("utf-8"))
        if restart:
            # Reply first, then become the checked-out helper. The deploy child is in
            # its own session and survives; the next listen() rebinds the socket.
            print("compose-host script changed; re-executing", flush=True)
            execv()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Host helper for compose apply from Settings → Updates.")
    ap.add_argument("command", choices=("listen",))
    ap.add_argument("--repo", required=True,
                    help="git clone deploy.sh can fetch from (not the rsync target)")
    ap.add_argument("--dir", required=True,
                    help="compose project directory (the rsync target)")
    ap.add_argument("--socket", default="~/.graphban-apply/apply.sock")
    args = ap.parse_args(argv)
    return listen(pathlib.Path(args.socket), pathlib.Path(args.repo),
                  pathlib.Path(args.dir))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
