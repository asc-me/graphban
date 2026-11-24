"""The local `--on-notify` hook (GRPH-1915)."""
from __future__ import annotations

import json
import sys
import time

import pytest

from gbfleet import service as service_mod
from gbfleet import until as until_mod
from gbfleet.client import ObservationRefused
from gbfleet.until import Wave, notify_outcome

NOTICE = {"prd": "GRPH-P1", "outcome": "drained", "open": [], "sent_at": "2026-11-24T00:00:00Z"}


def _py(tmp_path, body: str, name: str = "hook.py") -> str:
    """A hook command line that runs `body` under this interpreter, on POSIX and Windows.

    No `sh`, `cat` or `sleep`: the Windows runner has none of them on a bare CreateProcess.
    """
    script = tmp_path / name
    script.write_text(body)
    return f'"{sys.executable}" "{script}"'


def test_hook_gets_notice_json_on_stdin(tmp_path):
    out = tmp_path / "got.json"
    service_mod.run_hook(_py(tmp_path, f"import sys\nopen({str(out)!r}, 'w').write(sys.stdin.read())\n"), NOTICE)
    assert json.loads(out.read_text()) == NOTICE


def test_nonzero_exit_raises(tmp_path):
    with pytest.raises(service_mod.HookFailed, match="exited 3"):
        service_mod.run_hook(_py(tmp_path, "import sys\nsys.exit(3)\n"), NOTICE)


def test_slow_hook_is_killed(tmp_path):
    t = time.monotonic()
    with pytest.raises(service_mod.HookFailed, match="killed"):
        service_mod.run_hook(_py(tmp_path, "import time\ntime.sleep(30)\n"), NOTICE, timeout=0.5)
    assert time.monotonic() - t < 10


def test_command_runs_without_a_shell(tmp_path):
    # `;` is an argument to the hook, not a command separator: no shell sees the string.
    marker = tmp_path / "pwned"
    service_mod.run_hook(f'{_py(tmp_path, "pass")} ; touch "{marker}"', NOTICE)
    assert not marker.exists()


def test_missing_executable_raises():
    with pytest.raises(service_mod.HookFailed, match="could not run"):
        service_mod.run_hook("/nonexistent/gbfleet-hook", NOTICE)


def test_no_command_no_hook():
    assert service_mod.hook_for(None) is None
    assert service_mod.hook_for("") is None


class _Server:
    def call(self, tool, /, **kw):
        return {"results": []}

    def post_drain_notice(self, **p):
        return {"changed": True}


def test_failing_hook_is_logged_and_drain_continues(tmp_path):
    hook = service_mod.hook_for(_py(tmp_path, "import sys\nsys.exit(1)\n"))
    got = notify_outcome(_Server(), Wave(), "idle", "GRPH-P1", on_notice=hook)
    assert got is not None and got["outcome"] == "drained"


def test_hook_runs_once_with_notice(tmp_path):
    out = tmp_path / "n"
    hook = service_mod.hook_for(_py(
        tmp_path, f"import sys\nopen({str(out)!r}, 'a').write(sys.stdin.read() + '\\n')\n"))
    notify_outcome(_Server(), Wave(), "idle", "GRPH-P1", on_notice=hook)
    lines = out.read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["prd"] == "GRPH-P1"


def test_without_hook_notice_still_posts():
    got = notify_outcome(_Server(), Wave(), "idle", "GRPH-P1")
    assert got is not None


def test_make_plan_carries_on_notify(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    plan = service_mod.make_plan(
        ["--repo", str(tmp_path), "--server", "http://x", "--project", "p", "--prd", "P"],
        name="t", kind="systemd", binary="gbfleet", path_env="/bin", on_notify="echo hi there")
    assert plan.until_args[-2:] == ["--on-notify", "echo hi there"]
    assert "'echo hi there'" in service_mod.render(plan).decode()
