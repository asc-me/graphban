from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


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

    # Distinguishes a polite SIGTERM from a SIGKILL, which a plain sleeper cannot: it
    # dies to both, so a test using one cannot tell whether the grace period happened.
    write(
        "notes_sigterm",
        "import signal, sys, time\n"
        "from pathlib import Path\n"
        "def bye(*a):\n"
        "    Path(sys.argv[1]).write_text('sigterm', encoding='utf-8')\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, bye)\n"
        "print('ready', flush=True)\n"
        "while True: time.sleep(0.05)\n",
    )

    write(
        "ignores_sigterm",
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, lambda *a: None)\n"
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
