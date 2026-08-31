#!/usr/bin/env python3
"""Generate, install and remove the macOS `launchd` service (GRPH-582, PRD-27 S3).

A **LaunchDaemon**, not a LaunchAgent: an agent does not run until that user logs in, which is
wrong for a box that must come back from a power cut on its own.

**THE PLIST CARRIES NO SECRETS, and that corrects the PRD's own grill answer.** It said the
`JWT_SECRET` would reach the process through `EnvironmentVariables` "so it is not in `ps`
output". True about `ps`, wrong about disk: launchd has no `EnvironmentFile` equivalent and
plists in `/Library/LaunchDaemons` are world-readable — every one on a stock machine is
`-rw-r--r-- root wheel`. That is a worse leak than the one it avoided.

Instead the plist sets `WorkingDirectory`, and the app reads its own `.env` through
pydantic-settings, which `SettingsConfigDict(env_file=".env")` already does. The existing
mechanism, used correctly, rather than a new one.

**`RunAtLoad` and `KeepAlive` are both required.** Start at boot, and come back after a crash.
Either alone gives a service that looks installed and is gone after its first failure — which
is the "installs, appears to start, serves nothing" shape this PRD names as its main risk.

    python3 scripts/graphban_service.py plist --root /opt/graphban
    sudo python3 scripts/graphban_service.py install --root /opt/graphban
    sudo python3 scripts/graphban_service.py uninstall
"""
from __future__ import annotations

import argparse
import os
import pathlib
import plistlib
import subprocess
import sys
import time

LABEL = "dev.graphban.api"

#: Anything matching these must never appear in a plist key or value. The plist is
#: world-readable, so this is the difference between "not in `ps`" and "not on disk".
SECRET_MARKERS = ("secret", "password", "token", "api_key", "apikey", "private")


def plist_dict(*, root: pathlib.Path, python: pathlib.Path, user: str,
               label: str = LABEL, logs: pathlib.Path | None = None,
               port: int = 8000, host: str = "127.0.0.1",
               git_sha: str = "unknown", user_domain: bool = False) -> dict:
    """The launchd job description.

    `ProgramArguments` runs `app.serve` — the same entrypoint the container uses — so there is
    one way the application is started rather than a second that can drift from it.

    **`PORT` and `HOST` must be real environment variables, and finding that out cost a real
    install.** `app.serve` reads them from `os.environ` itself (the Dockerfile says so), while
    `.env` is read by pydantic-settings into `settings` and never reaches the environment. In
    Docker, compose supplies them. Natively nothing does — so the first daemon this generated
    loaded cleanly, reported "Started server process", and listened on **8000** while `.env`
    said 8234. launchd was happy, `launchctl list` showed it running, and nothing answered on
    the port anyone would have curled. That is the exact "installs, appears to start, serves
    nothing" failure this slice was written against, and no unit test would have caught it.

    Neither is a secret, so both belong here rather than in the `.env` the plist must not read.
    """
    logs = logs or pathlib.Path("/usr/local/var/log")
    # `GIT_SHA` is identity, not a secret: `/health` reports it, and S5's upgrade check
    # compares it to `--sha`. Leaving it out makes every native upgrade look like a
    # healthy box serving `unknown`.
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin",
        "PORT": str(port),
        "HOST": host,
        "GIT_SHA": git_sha,
    }
    job = {
        "Label": label,
        # The venv's python, not the system one: a service that resolves `python3` from PATH
        # picks up whatever the last installer put there.
        "ProgramArguments": [str(python), "-m", "app.serve"],
        # How `.env` is found at all — pydantic-settings reads it relative to the cwd.
        # `current` is the live release; S5 swaps it and puts the old one back. The
        # service must read THAT, not a fixed `backend/` beside it — pointing them at
        # different directories makes an upgrade replace code nobody serves.
        "WorkingDirectory": str(root / "current" / "backend"),
        # Start at boot AND come back after a crash. See the module docstring: either alone
        # produces a service that looks installed and is not running.
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(logs / "graphban.log"),
        "StandardErrorPath": str(logs / "graphban.err.log"),
        # PATH, PORT/HOST, and the revision. Nothing here is a secret, and nothing here
        # may become one — the file is world-readable.
        "EnvironmentVariables": env,
    }
    # UserName is a LaunchDaemon key. A LaunchAgent already runs as the logged-in user;
    # naming one is how you ask launchd for a privilege it will not give.
    if not user_domain:
        job["UserName"] = user
    return job


def secrets_in(plist: dict) -> list[str]:
    """Keys or values that look like a credential, so a leak fails a test rather than a review.

    Checks VALUES too: `EnvironmentVariables` is a nested dict, and the failure being prevented
    is somebody adding `{"JWT_SECRET": "..."}` there because it is the obvious place.
    """
    found: list[str] = []

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                low = str(k).lower()
                if any(m in low for m in SECRET_MARKERS):
                    found.append(f"{path}{k}")
                walk(v, f"{path}{k}.")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}{i}.")

    walk(plist, "")
    return found


def render(plist: dict) -> bytes:
    return plistlib.dumps(plist, sort_keys=True)


def plist_path(label: str = LABEL, *, user_domain: bool = False) -> pathlib.Path:
    if user_domain:
        return pathlib.Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    return pathlib.Path("/Library/LaunchDaemons") / f"{label}.plist"


def _run(cmd: list[str]) -> tuple[int, str]:
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def install(path: pathlib.Path, data: bytes, *, user_domain: bool, label: str = LABEL) -> int:
    """Write the plist and hand it to launchd, then say what launchd thought.

    `bootstrap` rather than the deprecated `load`, and the result is REPORTED: a plist written
    to disk that launchd never accepted is the "installed but not running" state this slice
    exists to make impossible.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    # 0644 is what launchd expects for a daemon plist and is why no secret may be in it.
    path.chmod(0o644)

    domain = f"gui/{os.getuid()}" if user_domain else "system"
    _run(["launchctl", "bootout", f"{domain}/{label}"])  # idempotent: ignore "not loaded"

    # BOOTOUT IS ASYNCHRONOUS, and bootstrapping into the gap fails with a message that names
    # nothing useful: `Bootstrap failed: 5: Input/output error`. Found by reinstalling over a
    # running job on a real Mac — the first attempt failed, the job was gone from
    # `launchctl list` a second later, and an identical retry succeeded.
    #
    # So: wait for the label to actually disappear before handing launchd the new one. The
    # retry underneath covers the same race from the other side, because "wait until it is
    # gone" is a poll and a poll can be unlucky.
    #
    # `_loaded` is the wrong question here (GRPH-582 bounce). A listed-with-no-pid job
    # (`-  1  label`) is not running, but it is also not gone — bootstrapping into that
    # listing is the async-bootout race. Wait until the row is absent.
    for _ in range(50):
        if _list_row(label) is None:
            break
        time.sleep(0.1)

    rc, out = _run(["launchctl", "bootstrap", domain, str(path)])
    if rc != 0:
        time.sleep(1.0)
        rc, out = _run(["launchctl", "bootstrap", domain, str(path)])
    if rc != 0:
        print(f"launchd refused the job: {out}", file=sys.stderr)
        return 1

    # ACCEPTED IS NOT RUNNING. `bootstrap` returning 0 means launchd took the job, not that it
    # stayed up — a plist whose ProgramArguments point at a missing interpreter bootstraps
    # cleanly and dies immediately. Report what `launchctl list` says rather than what the
    # exit code implied, because "installed and not running" is the failure this slice exists
    # to make impossible.
    # AND A CRASH LOOP IS NOT RUNNING EITHER. Checking immediately is not enough: launchd has
    # just forked, so the job HAS a pid for the instant between fork and exit, and this
    # returned success for a service dying on ModuleNotFoundError every few seconds during the
    # S6 walk. The systemd side hit the identical race and grew a settle; this is that.
    time.sleep(2.0)
    if not _loaded(label):
        _, listing = _run(["launchctl", "list"])
        line = next((l for l in listing.splitlines() if label in l), "(not listed)")
        print(f"launchd accepted {label} but it is not running — check the log\n  {line}",
              file=sys.stderr)
        return 1
    print(f"installed {label} in {domain}")
    return 0


def _list_row(label: str) -> list[str] | None:
    """The `PID Status Label` row for this job, or None if launchd does not list it."""
    _, out = _run(["launchctl", "list"])
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == label:
            return parts
    return None


def _loaded(label: str) -> bool:
    """Is this job RUNNING — not merely known to launchd?

    `launchctl list` prints `PID  Status  Label`, and a crash-looping job is still listed:
    `-  1  dev.graphban.api`, with no pid and a non-zero exit status. The first version of
    this asked only whether the label APPEARED, so it answered yes for a service that had
    never once stayed up.

    Found during the S6 walk against a job failing on `ModuleNotFoundError` every few seconds:
    the install printed "installed" while `launchctl list` showed `-` and `1`. The systemd side
    had already learned this and grown an `NRestarts` check, and the lesson was never carried
    back here — which is exactly the cross-slice gap a walk exists to find.

    A real pid is the whole test: launchd gives one to a process that is up, and a `-` to one
    that is not.
    """
    parts = _list_row(label)
    return bool(parts and parts[0].isdigit())


def uninstall(path: pathlib.Path, *, user_domain: bool, label: str = LABEL) -> int:
    domain = f"gui/{os.getuid()}" if user_domain else "system"
    _run(["launchctl", "bootout", f"{domain}/{label}"])
    if path.exists():
        path.unlink()
    print(f"removed {label} from {domain}")
    return 0


def status(label: str = LABEL) -> int:
    rc, out = _run(["launchctl", "list"])
    for line in out.splitlines():
        if label in line:
            print(line)
            return 0
    print(f"{label} is not loaded")
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=("plist", "install", "uninstall", "status"))
    ap.add_argument("--root", default="/opt/graphban", help="the install root")
    ap.add_argument("--python", default="", help="defaults to <root>/venv/bin/python — OUTSIDE the swapped release")
    ap.add_argument("--user", default="graphban", help="the service account")
    ap.add_argument("--label", default=LABEL)
    ap.add_argument("--logs", default="/usr/local/var/log")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--bind", default="127.0.0.1", help="the address app.serve binds")
    # For verifying the mechanism on a workstation without a privileged install. The SHIPPED
    # service is a daemon; this exists so "launchd accepted it and it served" can be proven
    # without asking somebody for root on their laptop.
    ap.add_argument("--user-domain", action="store_true",
                    help="install as a LaunchAgent for the current user (verification only)")
    args = ap.parse_args(argv)

    root = pathlib.Path(args.root).resolve()
    python = pathlib.Path(args.python) if args.python else root / "venv" / "bin" / "python"
    data = plist_dict(root=root, python=python, user=args.user, label=args.label,
                      logs=pathlib.Path(args.logs), port=args.port, host=args.bind,
                      user_domain=args.user_domain)

    leaked = secrets_in(data)
    if leaked:
        # Refuse rather than warn. A world-readable plist with a credential in it is not a
        # thing to print a warning about and continue past.
        print(f"refusing: the plist would carry {', '.join(leaked)} and it is world-readable",
              file=sys.stderr)
        return 2

    path = plist_path(args.label, user_domain=args.user_domain)
    if args.command == "plist":
        sys.stdout.write(render(data).decode())
        return 0
    if args.command == "install":
        return install(path, render(data), user_domain=args.user_domain, label=args.label)
    if args.command == "uninstall":
        return uninstall(path, user_domain=args.user_domain, label=args.label)
    return status(args.label)


if __name__ == "__main__":
    sys.exit(main())
