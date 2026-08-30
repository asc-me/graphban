"""The install slices agree on one layout (GRPH-587, PRD-27 S6).

Found by reading S3, S4 and S5 side by side while starting the acceptance walk — before the
walk itself ran a single command:

    S3 launchd   WorkingDirectory = root/backend
    S4 systemd   WorkingDirectory = root/backend
    S5 upgrade   swaps root/current

**They did not compose.** An upgrade replaced `root/current` while the service went on reading
`root/backend`, so the new code was never served. It failed SAFE — the identity check saw the
old sha and rolled back — but it could never succeed, and every test in all three slices passed,
because each was only ever exercised on its own.

That is the shape GRPH-503 recorded: *"every walk invoked the CLI directly with `--agent-id`,
so the adapter's argv was verified and the thing the argv is FOR never was."* Five slices, all
green, all exercising the layer below the one that was broken.

This file is the guard that would have caught it: it asserts the slices agree, rather than
asserting each is internally consistent.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))
import graphban_host as gh  # noqa: E402
import graphban_service as gs  # noqa: E402
import graphban_systemd as gsd  # noqa: E402
import graphban_upgrade as up  # noqa: E402

ROOT = pathlib.Path("/opt/graphban")
PYTHON = ROOT / "venv" / "bin" / "python"


def _plist():
    return gs.plist_dict(root=ROOT, python=PYTHON, user="graphban")


def _unit():
    return gsd.unit_text(root=ROOT, python=PYTHON, user="graphban")


def test_the_service_reads_the_directory_the_upgrade_swaps():
    """THE DEFECT THIS FILE EXISTS FOR. If these disagree, an upgrade replaces code nobody
    serves — and the failure is invisible from either side."""
    swapped = ROOT / "current"

    assert _plist()["WorkingDirectory"].startswith(str(swapped)), (
        "launchd points somewhere the upgrade does not swap"
    )
    assert f"WorkingDirectory={swapped}" in _unit(), (
        "systemd points somewhere the upgrade does not swap"
    )


def test_both_platforms_use_the_same_working_directory():
    """One layout, not two. A per-platform path is a per-platform bug waiting to happen."""
    plist_wd = _plist()["WorkingDirectory"]
    unit_wd = next(ln.split("=", 1)[1] for ln in _unit().splitlines()
                   if ln.startswith("WorkingDirectory="))

    assert plist_wd == unit_wd


def test_the_interpreter_lives_outside_the_swapped_release():
    """The venv must survive an upgrade.

    Inside `current/`, a swap replaces the very interpreter the unit names — so the service
    would point at a path that ceased to exist halfway through the operation meant to keep it
    running. Outside, the path in the unit is stable and rollback needs no second venv.
    """
    swapped = str(ROOT / "current")
    exec_start = _plist()["ProgramArguments"][0]

    assert not exec_start.startswith(swapped), "the interpreter is inside the swapped release"
    assert exec_start.startswith(str(ROOT / "venv"))
    assert f"ExecStart={ROOT / 'venv'}" in _unit()


def test_the_host_cli_renders_the_same_working_directory():
    """S7 is the CALL. A helper that agrees with S3/S4 while the command that operators
    run points somewhere else is the S6 defect wearing a dispatcher."""
    _, plist, kind = gh.write_service(
        platform="darwin", root=ROOT, python=PYTHON, user="graphban",
        port=8000, host="127.0.0.1", git_sha="x", user_scope=False)
    assert kind == "plist"
    import plistlib
    job = plistlib.loads(plist)
    assert job["WorkingDirectory"] == str(ROOT / "current" / "backend")

    _, unit, kind = gh.write_service(
        platform="linux", root=ROOT, python=PYTHON, user="graphban",
        port=8000, host="127.0.0.1", git_sha="x", user_scope=False)
    assert kind == "unit"
    assert f"WorkingDirectory={ROOT / 'current' / 'backend'}" in unit


def test_uninstall_removes_the_paths_the_installers_create():
    """A remover that misses what the installer wrote leaves a machine nobody can cleanly
    reinstall on."""
    import inspect

    src = inspect.getsource(up.uninstall)
    for path in ("current", "previous", "venv"):
        assert f'"{path}"' in src, f"uninstall does not remove {path}"
