"""Machine identity and the report `gban setup` / `gban doctor` send (GRPH-1442).

The machine id is random and lives in `machine.json` in gban's home. Wiping that directory makes
a new machine, which is the intent: the id names an installation, not a person or a label.

A report that cannot be delivered NEVER counts. It comes back as an UNKNOWN line naming the
server and the reason, and the caller still exits on its own checks.
"""
from __future__ import annotations

import platform
import shutil
import socket
import subprocess  # nosec B404 - fixed argv, no shell
import uuid
from importlib import metadata

from gban import config
from gban.client import NoSession, Refused, Unreachable, authenticated
from gban.doctor import UNKNOWN, _line

MACHINE_FILE = "machine.json"


def machine_id() -> str:
    """The stored id, minted on first use. An unwritable home still yields a (volatile) id."""
    path = config.home() / MACHINE_FILE
    existing = config._read(path).get("machine_id")
    if isinstance(existing, str) and existing:
        return existing
    fresh = "gbm_" + uuid.uuid4().hex
    try:
        config._write(path, {"machine_id": fresh})
    except OSError:
        pass
    return fresh


def _version(dist: str) -> str:
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return ""


def _gbfleet_version() -> str:
    exe = shutil.which("gbfleet")
    if not exe:
        return ""
    try:
        done = subprocess.run([exe, "--version"], capture_output=True, text=True,  # nosec B603
                              timeout=5)
        return (done.stdout or "").strip().split()[-1] if done.returncode == 0 and done.stdout.strip() else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _harnesses() -> list[str]:
    return [name for name, binary in (("claude", "claude"), ("grok", "grok"), ("codex", "codex"))
            if shutil.which(binary)]


def payload(project: str, lines: list[dict]) -> dict:
    return {
        "machine_id": machine_id(), "label": socket.gethostname().split(".")[0],
        "os": f"{platform.system()} {platform.release()}".strip(),
        "gban_version": _version("gban"), "gbfleet_version": _gbfleet_version(),
        "harnesses": _harnesses(), "project": project,
        "lines": [{"status": l["status"], "name": l["name"], "detail": l.get("detail", "")}
                  for l in lines],
    }


def send(url: str, project: str, lines: list[dict]) -> list[dict]:
    """Deliver the report. Returns the line to append when it could not be delivered, else []."""
    try:
        authenticated(url, act="report").call("POST", "/api/machines/report",
                                              payload(project, lines))
    except NoSession as exc:
        return [_unreported(url, exc.why)]
    except Refused as exc:
        return [_unreported(url, exc.detail)]
    except Unreachable as exc:
        return [_unreported(url, str(exc))]
    except Exception as exc:  # noqa: BLE001 - a report must never take the command down
        return [_unreported(url, f"{type(exc).__name__}: {exc}")]
    return []


def _unreported(url: str, reason: str) -> dict:
    return _line("report", UNKNOWN, "report", f"could not report to {url}: {reason}")
