#!/usr/bin/env python3
"""Generate, install and remove the Linux `systemd` unit (GRPH-583, PRD-27 S4).

The Linux half of what `graphban_service.py` does for macOS. The PRD's risk note is the reason
both exist as their own slice: *"launchd and systemd have different restart semantics; 'it
restarts on failure' must be asserted on both rather than assumed from one."*

**The two defects S3 found by installing for real are carried across, not rediscovered:**

  - `PORT` and `HOST` must be REAL environment variables. `app.serve` reads them from
    `os.environ` itself, while `.env` is read by pydantic-settings into `settings` and never
    reaches the environment. On macOS that produced a daemon listening on 8000 while its
    `.env` said 8234 — launchd happy, nothing answering. `Environment=` here, same as the
    plist.
  - **Accepted is not running.** `systemctl start` succeeding means systemd took the job. The
    install asks `is-active` afterwards rather than trusting the exit code.

**No secret in the unit, even though systemd has `EnvironmentFile=`.** Unit files in
`/etc/systemd/system` are world-readable exactly as plists are, and the app already reads its
own `.env`. One mechanism on both platforms beats a per-platform special case.

**`enable` is not optional.** A unit that is started but not enabled runs now and is gone after
a reboot — the same "looks installed" failure in a different costume.

    python3 scripts/graphban_systemd.py unit --root /opt/graphban
    sudo python3 scripts/graphban_systemd.py install --root /opt/graphban
"""
from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys
import time

UNIT = "graphban.service"

#: Never in a unit file: it is world-readable. Shared intent with the launchd generator.
SECRET_MARKERS = ("secret", "password", "token", "api_key", "apikey", "private")


def unit_text(*, root: pathlib.Path, python: pathlib.Path, user: str,
              port: int = 8000, host: str = "127.0.0.1",
              restart_sec: int = 5, user_scope: bool = False) -> str:
    """The unit file.

    `After=network-online.target` and deliberately NO `Requires=` on Postgres: the database may
    be on another host entirely, and a hard dependency on a unit that does not exist locally
    makes the service unstartable for a correct deployment.

    **`User=` is omitted in user scope, and that cost a real install to learn.** A `--user`
    unit already runs as that user; naming one makes systemd try to set supplementary groups
    it may not, and every start dies with `216/GROUP` — `Failed at step GROUP spawning
    /usr/bin/python3: Operation not permitted`. The system unit needs `User=` and must keep it.
    """
    account = "" if user_scope else f"User={user}\n"
    return f"""[Unit]
Description=Graphban API
Documentation=https://github.com/asc-me/graphban
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
{account}# How `.env` is found at all — pydantic-settings reads it relative to the cwd.
WorkingDirectory={root / "current" / "backend"}
# `app.serve` reads these from the environment itself; `.env` cannot supply them.
Environment=PORT={port}
Environment=HOST={host}
# The venv's interpreter, not whatever `python3` resolves to today.
ExecStart={python} -m app.serve
# The KeepAlive equivalent. RestartSec matters: without a delay a service that fails at
# startup spins as fast as systemd can fork it.
Restart=always
RestartSec={restart_sec}
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""


def secrets_in(text: str) -> list[str]:
    """Assignments in a unit file that look like a credential.

    Reads the RENDERED text rather than a dict, because that is what lands on disk — a check
    over an intermediate structure can pass while the file itself carries the key.
    """
    found = []
    for line in text.splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        key = line.split("=", 1)[0].strip().lower()
        value = line.split("=", 1)[1]
        target = key if key not in ("environment", "environmentfile") else value.lower()
        if any(m in target.lower() for m in SECRET_MARKERS):
            found.append(line.strip())
    return found


def unit_path(name: str = UNIT, *, user_scope: bool = False) -> pathlib.Path:
    if user_scope:
        return pathlib.Path.home() / ".config" / "systemd" / "user" / name
    return pathlib.Path("/etc/systemd/system") / name


def _ctl(user_scope: bool) -> list[str]:
    return ["systemctl", "--user"] if user_scope else ["systemctl"]


def _run(cmd: list[str]) -> tuple[int, str]:
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def is_active(name: str = UNIT, *, user_scope: bool = False) -> bool:
    rc, out = _run(_ctl(user_scope) + ["is-active", name])
    return rc == 0 and out.strip() == "active"


def install(path: pathlib.Path, text: str, *, user_scope: bool, name: str = UNIT) -> int:
    """Write, reload, enable, start — then check it is actually running."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o644)

    ctl = _ctl(user_scope)
    _run(ctl + ["daemon-reload"])
    # ENABLE, not just start. A started-but-not-enabled unit runs now and is gone after a
    # reboot, which is indistinguishable from a working install until the machine restarts.
    rc, out = _run(ctl + ["enable", name])
    if rc != 0:
        print(f"systemd refused to enable the unit: {out}", file=sys.stderr)
        return 1
    rc, out = _run(ctl + ["restart", name])
    if rc != 0:
        print(f"systemd refused to start the unit: {out}", file=sys.stderr)
        return 1

    # ACCEPTED IS NOT RUNNING — the lesson S3 learned on macOS, and a CRASH LOOP IS NOT
    # RUNNING EITHER, which is the lesson this slice learned on a real Linux box.
    #
    # The first version of this polled `is-active` and returned success on the first `active`.
    # It reported "installed and running" for a unit that was dying every five seconds:
    # `Restart=always` means systemd keeps starting it, and `is-active` says `active` for the
    # instant between fork and exit. So the guard written to prevent "installs, appears to
    # start, serves nothing" reported exactly that.
    #
    # A service that is up is still up a moment later, and `NRestarts` is the direct question.
    for _ in range(30):
        if is_active(name, user_scope=user_scope):
            break
        time.sleep(0.2)

    time.sleep(1.5)
    _, restarts = _run(ctl + ["show", "-p", "NRestarts", "--value", name])
    if is_active(name, user_scope=user_scope) and (restarts.strip() or "0") == "0":
        print(f"installed and running: {name}")
        return 0

    _, status = _run(ctl + ["status", name, "--no-pager", "-n", "10"])
    detail = (f"it has restarted {restarts.strip()} times already — it is crash looping"
              if (restarts.strip() or "0") != "0" else "it is not active")
    print(f"systemd accepted {name} but {detail}:\n{status}", file=sys.stderr)
    return 1


def uninstall(path: pathlib.Path, *, user_scope: bool, name: str = UNIT) -> int:
    ctl = _ctl(user_scope)
    _run(ctl + ["stop", name])
    _run(ctl + ["disable", name])
    if path.exists():
        path.unlink()
    _run(ctl + ["daemon-reload"])
    print(f"removed {name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate and manage the Graphban systemd unit.")
    ap.add_argument("command", choices=("unit", "install", "uninstall", "status"))
    ap.add_argument("--root", default="/opt/graphban")
    ap.add_argument("--python", default="")
    ap.add_argument("--user", default="graphban", help="the service account")
    ap.add_argument("--name", default=UNIT)
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--user-scope", action="store_true",
                    help="install as a --user unit (verification without root)")
    args = ap.parse_args(argv)

    root = pathlib.Path(args.root).resolve()
    python = pathlib.Path(args.python) if args.python else root / "venv" / "bin" / "python"
    text = unit_text(root=root, python=python, user=args.user, port=args.port,
                     host=args.bind, user_scope=args.user_scope)

    leaked = secrets_in(text)
    if leaked:
        print(f"refusing: the unit would carry {leaked} and it is world-readable",
              file=sys.stderr)
        return 2

    path = unit_path(args.name, user_scope=args.user_scope)
    if args.command == "unit":
        sys.stdout.write(text)
        return 0
    if args.command == "install":
        return install(path, text, user_scope=args.user_scope, name=args.name)
    if args.command == "uninstall":
        return uninstall(path, user_scope=args.user_scope, name=args.name)
    print("active" if is_active(args.name, user_scope=args.user_scope) else "not active")
    return 0 if is_active(args.name, user_scope=args.user_scope) else 1


if __name__ == "__main__":
    sys.exit(main())
