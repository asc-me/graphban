from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest



#: PRD-38 D3: the supervisor posts telemetry on a REST path beside `/api/mcp`, over the same
#: transport, so a fake server that understands only JSON-RPC now sees requests it cannot
#: parse. Handlers call this FIRST and return its answer when it is not None. It is here
#: rather than repeated in each test because the alternative — every handler growing its own
#: guard — is how one of them ends up silently answering an MCP call with 200 and nothing.
def telemetry_ack(request):
    """A 200 for anything that is not the MCP endpoint, else None (the handler's own job)."""
    import httpx

    if request.url.path == "/api/mcp":
        return None
    return httpx.Response(200, json={"ok": True, "path": request.url.path})

def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()


def _init(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("x\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-qm", "first")
    return root


def make_stub_binary(path: Path, prints: str = "", exit_code: int = 0) -> Path:
    """A stand-in for a vendor BINARY — a file the code under test will EXECUTE by path.

    `#!/bin/sh` is not a program on Windows. CreateProcess reports "%1 is not a valid
    Win32 application" (WinError 193), which accounted for 32 of this suite's 50 failures
    the first time it ran there (GRPH-588). A `.cmd` is the portable equivalent.

    **Returns the path to invoke, which on Windows is not the path passed in** — the
    extension is what makes the file executable, so callers must use the return value.

    For stand-ins the code runs as `[interpreter, script]` rather than by path, write a
    plain `.py` instead: there is no exec bit to set and no shebang to honour.
    """
    lines = [line for line in prints.splitlines() if line != ""] if prints else []
    if os.name == "nt":
        target = path.with_suffix(".cmd")
        body = "@echo off\n" + "".join(f"@echo {line}\n" for line in lines)
        body += f"@exit /b {exit_code}\n"
        target.write_text(body, encoding="utf-8")
        return target

    body = "#!/bin/sh\n" + "".join(f"echo '{line}'\n" for line in lines)
    body += f"exit {exit_code}\n"
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def console_script(name: str) -> Path:
    """Where pip put a console script.

    `.exe` on Windows — pip writes a real launcher there, not an extensionless shim, so
    asking for the bare name finds nothing and the test fails for a reason that has
    nothing to do with what it was checking (GRPH-588).
    """
    return Path(sys.executable).parent / (f"{name}.exe" if os.name == "nt" else name)


def make_stub_script(
    path: Path,
    *,
    prints: tuple[str, ...] = (),
    exit_code: int = 0,
    sleep: float = 0.0,
    numbered_lines: int = 0,
    touch: tuple[str, ...] = (),
) -> Path:
    """A stand-in the code under test runs as ARGV — `[interpreter, script]`.

    The sibling of `make_stub_binary`, for the other half of the problem. Where that one
    is handed to something that execs a path and therefore has to be a program, this one
    is named in an argv the test controls, so it can be a plain `.py`: no shebang to
    honour, no exec bit to set, and sleeping, looping and choosing an exit status are all
    just Python (GRPH-589).

    The shell scripts these replace expressed the same four things — print, exit, sleep,
    loop — in a language Windows has no interpreter for.
    """
    body = ["import sys, time"]
    if sleep:
        body.append(f"time.sleep({sleep})")
    for name in touch:
        body.append(f"open({name!r}, 'w').close()")
    for line in prints:
        body.append(f"print({line!r}, flush=True)")
    if numbered_lines:
        body.append(f"for i in range(1, {numbered_lines} + 1): print(f'line {{i}}', flush=True)")
    body.append(f"sys.exit({exit_code})")
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return path


def stub_argv(path: Path) -> list[str]:
    """How to run a `make_stub_script` stub: through this interpreter, not by path."""
    return [sys.executable, str(path)]


def stub_command(path: Path, interpreter: Path | None = None) -> str:
    """The same, as a config string a test embeds in TOML.

    Quoted and forward-slashed: `shlex.split` eats backslashes as escapes, so a Windows
    interpreter path written raw arrives as `C:UsersAlex...`.
    """
    exe = Path(interpreter or sys.executable).as_posix()
    return f'"{exe}" "{Path(path).as_posix()}"'


def pid_alive(pid: int) -> bool:
    """Whether a process exists, on either platform.

    `os.kill(pid, 0)` is the POSIX idiom and raises `OSError: [WinError 87] The parameter
    is incorrect` on Windows — signal 0 is not a thing there. Several tests used it as a
    helper, so they failed for a reason unconnected to what they were testing
    (GRPH-588).
    """
    if os.name == "nt":
        listed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True
        ).stdout
        return str(pid) in listed
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


@pytest.fixture
def stub_binary():
    """`make_stub_binary`, for tests that would rather take a fixture than an import."""
    return make_stub_binary


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A real repository with one commit, so `git worktree add` works."""
    return _init(tmp_path / "repo")


@pytest.fixture
def other_repo(tmp_path: Path) -> Path:
    return _init(tmp_path / "other")


@pytest.fixture
def state(tmp_path: Path) -> Path:
    """A private state directory, so tests never touch the real one."""
    d = tmp_path / "state"
    d.mkdir()
    return d


@pytest.fixture
def linked_worktree(git_repo: Path, tmp_path: Path) -> Path:
    """A linked worktree of `git_repo` — what the supervisor itself creates."""
    wt = tmp_path / "wt-a"
    _git(git_repo, "worktree", "add", "-q", "-b", "gb/wave-a", str(wt))
    return wt


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    d = tmp_path / "logs"
    d.mkdir()
    return d


@pytest.fixture
def scripts(tmp_path: Path) -> dict[str, Path]:
    """Stand-in vendor binaries. Real processes, real signals, real files.

    A mock cannot tell you that a full pipe wedges a child or that killing the leader
    leaves its helpers running, and both are the failure modes that matter here.
    """
    import sys

    made: dict[str, Path] = {}

    def write(name: str, body: str) -> Path:
        path = tmp_path / f"{name}.py"
        path.write_text(body, encoding="utf-8")
        made[name] = path
        return path

    write("sleeper", "import time\ntime.sleep(300)\n")

    # A worker that behaves: does a bit of work in its worktree, then exits. Exiting on
    # empty is the normal end of a run (D-c), not a failure.
    write(
        "works_then_exits",
        "import pathlib\n"
        "pathlib.Path('feature.py').write_text('print(1)\\n', encoding='utf-8')\n",
    )

    write("exits_immediately", "pass\n")

    # P30 D6: gbagent.loop EXIT_HANDOFF_FAILED / EXIT_STUCK. Distinct from a supervisor
    # crash — 70 is a failed run (item still claimed); 75 is a completed give-up.
    write(
        "exits_handoff_failed",
        "import pathlib, sys\n"
        "pathlib.Path('feature.py').write_text('x\\n')\n"
        "sys.exit(70)\n",
    )
    write(
        "exits_stuck",
        "import pathlib, sys\n"
        "pathlib.Path('feature.py').write_text('x\\n')\n"
        "sys.exit(75)\n",
    )

    # A child that is ALIVE and says nothing, which is the case the roster cannot
    # distinguish from a busy one and `progress` exists to surface (GRPH-579).
    write("silent_then_exits", "import time\ntime.sleep(1.5)\n")

    # The realistic stuck child: it worked, and then it stopped. Distinct from
    # `silent_then_exits`, which never wrote at all — that one exercises NEVER_WROTE and
    # leaves the numeric silence path untested, which sabotage caught (GRPH-579).
    write(
        "talks_then_stalls",
        "import sys, time\n"
        "sys.stdout.write('starting work\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(1.5)\n",
    )

    # ...and the one that comes back. A child quiet for a while and then productive
    # again must stop being reported as quiet, or the summary accuses a working child.
    # It must still be TALKING when it exits, not merely have talked once. A child that
    # speaks and then falls silent again for its last half-second is quiet at the end,
    # and reporting it is correct — the first version of this stand-in tested the
    # opposite of what it claimed.
    write(
        "stalls_then_talks",
        "import sys, time\n"
        "time.sleep(1.0)\n"
        "end = time.time() + 0.6\n"
        "while time.time() < end:\n"
        "    sys.stdout.write('back at work\\n')\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(0.05)\n",
    )

    # ...and its control. Same lifetime, same exit, the only difference is that this one
    # produces output — so a test asserting the quiet one is reported has something to
    # compare against, rather than asserting a property nothing could fail.
    write(
        "chatty_then_exits",
        "import sys, time\n"
        "end = time.time() + 1.5\n"
        "while time.time() < end:\n"
        "    sys.stdout.write('working\\n')\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(0.05)\n",
    )

    # Long enough for the supervisor's wait loop to poll several times, short enough
    # that the wave ends on its own. `works_then_exits` is gone before the loop runs at
    # all, which makes it useless for anything about what happens WHILE a child works.
    write(
        "works_then_waits",
        "import pathlib, time\n"
        "pathlib.Path('feature.py').write_text('print(1)\\n', encoding='utf-8')\n"
        "time.sleep(1.5)\n",
    )

    # Leaves an uncommitted diff and keeps running, so a partition can interrupt it with
    # work in the tree — the case where "the work survives, the claim does not" has
    # something to survive.
    write(
        "writes_then_sleeps",
        "import pathlib, time\n"
        "pathlib.Path('half-done.py').write_text('half a thought\\n', encoding='utf-8')\n"
        "time.sleep(300)\n",
    )

    # Reports where it was actually started, so the cwd assertion is not a value
    # compared against itself. /proc does not exist on macOS.
    write(
        "says_where_it_is",
        "import os, sys, time\n"
        "print(os.getcwd(), flush=True)\n"
        "print(os.environ.get('GBFLEET_PROBE', ''), flush=True)\n"
        "time.sleep(300)\n",
    )

    write("exits_badly", "import sys\nsys.stderr.write('adapter blew up\\n')\nsys.exit(3)\n")

    # Distinguishes a polite stop from a hard kill, which a plain sleeper cannot: it
    # dies to both, so a test using one cannot tell whether the grace period happened.
    #
    # SIGBREAK as well as SIGTERM. There is no SIGTERM on Windows — the graceful step is
    # CTRL_BREAK — so a stand-in that trapped only SIGTERM exited with
    # STATUS_CONTROL_C_EXIT (0xC000013A) instead of on its own terms, and the test read
    # that as "never got the chance" when it plainly had (GRPH-588).
    write(
        "notes_sigterm",
        "import signal, sys, time\n"
        "from pathlib import Path\n"
        "def bye(*a):\n"
        "    Path(sys.argv[1]).write_text('sigterm', encoding='utf-8')\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, bye)\n"
        "if hasattr(signal, 'SIGBREAK'):\n"
        "    signal.signal(signal.SIGBREAK, bye)\n"
        "print('ready', flush=True)\n"
        "while True: time.sleep(0.05)\n",
    )

    write(
        "ignores_sigterm",
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, lambda *a: None)\n"
        "if hasattr(signal, 'SIGBREAK'):\n"
        "    signal.signal(signal.SIGBREAK, lambda *a: None)\n"
        "print('ready', flush=True)\n"
        "while True: time.sleep(0.1)\n",
    )

    write(
        "spawns_a_helper",
        "import subprocess, sys, time\n"
        "kid = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
        "print(kid.pid, flush=True)\n"
        "time.sleep(300)\n",
    )

    write(
        "very_chatty",
        "import sys, time\n"
        "sys.stderr.write('x' * 1_000_000)\n"
        "sys.stderr.flush()\n"
        "print('still here', flush=True)\n"
        "time.sleep(300)\n",
    )

    return {"python": Path(sys.executable), **made}
