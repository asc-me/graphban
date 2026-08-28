"""Run `hostos`'s Windows branch on a Windows box and say whether it holds.

GRPH-576. `fleet/tests/test_hostos.py` runs on the developer's machine and can only
execute one of the two branches; on a POSIX host it proves nothing at all about
Windows. This is the other half, and it is a script rather than a test because the
machine that must run it is not the machine that runs CI.

    py fleet\\scripts\\verify_hostos_windows.py

Loads `hostos.py` by path, so it needs neither an installed `gbfleet` nor the Python
version the package requires — the point is to exercise the operating system, and the
box may not have 3.12 yet. Exits non-zero if any check fails, and prints what it
measured either way so the output can be pasted onto the ticket as evidence.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOSTOS = Path(__file__).resolve().parents[1] / "src" / "gbfleet" / "hostos.py"

spec = importlib.util.spec_from_file_location("gbfleet_hostos", HOSTOS)
hostos = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hostos)

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    if not condition:
        FAILURES.append(name)


def alive(pid: int) -> bool:
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True
    ).stdout
    return str(pid) in out


SPAWNS_A_HELPER = (
    "import subprocess, sys, time\n"
    "h = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
    "open(sys.argv[1], 'w').write(str(h.pid))\n"
    "time.sleep(300)\n"
)


def main() -> int:
    print(f"hostos from {HOSTOS}")
    print(f"python {sys.version.split()[0]}  os.name={os.name}\n")

    print("platform selection")
    check("hostos reports Windows", hostos.WINDOWS, f"WINDOWS={hostos.WINDOWS}")
    if not hostos.WINDOWS:
        print("\nNot a Windows host. Nothing below would mean anything; stopping.")
        return 1
    check("no job limit flags are set", hostos.JOB_LIMIT_FLAGS == 0,
          f"JOB_LIMIT_FLAGS={hostos.JOB_LIMIT_FLAGS:#x}")
    check("spawn_kwargs asks for a new process group",
          bool(hostos.spawn_kwargs().get("creationflags", 0)
               & subprocess.CREATE_NEW_PROCESS_GROUP),
          str(hostos.spawn_kwargs()))
    check("user_tag is path-safe", bool(hostos.user_tag()) and
          not set(hostos.user_tag()) & set('\\/: '), repr(hostos.user_tag()))

    print("\npositional IO (os.pread/os.pwrite do not exist here)")
    check("os.pread really is absent", not hasattr(os, "pread"))
    tmp = Path(tempfile.gettempdir()) / "gbfleet-verify.bin"
    fd = os.open(tmp, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        hostos.write_at(fd, b"AAAAAAAAAAAAA", 0)
        # A stale file offset must not move either call. Both directions are checked
        # separately: sabotage on the developer's machine cannot reach this branch at
        # all, so if it is wrong here nothing anywhere else will say so.
        os.lseek(fd, 3000, os.SEEK_SET)
        hostos.write_at(fd, b"holder-record", 0)
        check("write_at ignores the current file offset",
              hostos.read_at(fd, 13, 0) == b"holder-record")
        os.lseek(fd, 3000, os.SEEK_SET)
        check("read_at ignores the current file offset",
              hostos.read_at(fd, 13, 0) == b"holder-record")
    finally:
        os.close(fd)

    print("\nthe lock")
    lock = Path(tempfile.gettempdir()) / "gbfleet-verify.lock"
    first = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    second = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        hostos.write_at(first, b'{"pid": 42, "repo": "x"}', 0)
        hostos.lock_exclusive(first)
        refused = False
        try:
            hostos.lock_exclusive(second)
        except hostos.AlreadyLocked:
            refused = True
        check("a second holder is refused", refused)
        # The whole reason the lock sits on a byte past the record.
        record = hostos.read_at(second, 4096, 0)
        check("the holder record is readable while the lock is held",
              b'"pid": 42' in record, repr(record[:40]))
        # lock.py truncates and rewrites while holding the lock; the lock byte is past
        # end-of-file after that, which must stay legal.
        rewrote = True
        try:
            os.ftruncate(first, 0)
            hostos.write_at(first, b'{"pid": 99}', 0)
            os.fsync(first)
        except OSError as exc:
            rewrote = False
            print(f"        ftruncate/write while locked failed: {exc}")
        check("the record can be truncated and rewritten while locked", rewrote)
    finally:
        os.close(first)
        os.close(second)

    print("\nthe lock dies with its holder")
    src = (
        "import os, sys, time, importlib.util\n"
        f"spec = importlib.util.spec_from_file_location('h', r'{HOSTOS}')\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        f"fd = os.open(r'{lock}', os.O_RDWR | os.O_CREAT, 0o600)\n"
        "m.lock_exclusive(fd)\n"
        "print('held', flush=True)\n"
        "time.sleep(60)\n"
    )
    holder = subprocess.Popen([sys.executable, "-c", src], stdout=subprocess.PIPE, text=True)
    try:
        check("the child process took the lock", holder.stdout.readline().strip() == "held")
    finally:
        holder.kill()
        holder.wait(timeout=10)
    released = False
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            hostos.lock_exclusive(fd)
            released = True
            break
        except hostos.AlreadyLocked:
            time.sleep(0.2)
        finally:
            os.close(fd)
    check("the lock is released when its holder is killed", released)

    print("\nthe process tree")
    marker = Path(tempfile.gettempdir()) / "gbfleet-verify.pid"
    if marker.exists():
        marker.unlink()
    top = subprocess.Popen([sys.executable, "-c", SPAWNS_A_HELPER, str(marker)],
                           **hostos.spawn_kwargs())
    tree = hostos.ProcessTree(top)
    helper = None
    try:
        deadline = time.monotonic() + 20
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        helper = int(marker.read_text().strip()) if marker.exists() else None
        check("a helper process started", helper is not None and alive(helper),
              f"top={top.pid} helper={helper}")

        # The measured Windows trap: terminating only the leader leaves the helper.
        tree.kill()
        try:
            top.wait(timeout=20)
        except subprocess.TimeoutExpired:
            pass
        deadline = time.monotonic() + 20
        while helper is not None and alive(helper) and time.monotonic() < deadline:
            time.sleep(0.2)
        check("killing the tree took the helper with it",
              helper is not None and not alive(helper),
              f"helper {helper} alive={alive(helper) if helper else '?'}")
    finally:
        tree.close()
        if top.poll() is None:
            top.kill()
        for pid in filter(None, [helper]):
            if alive(pid):
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        if marker.exists():
            marker.unlink()

    print("\nclosing the handle must NOT kill the tree (KILL_ON_JOB_CLOSE is off)")
    survivor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                                **hostos.spawn_kwargs())
    tree2 = hostos.ProcessTree(survivor)
    try:
        tree2.close()
        time.sleep(2)
        check("the child outlives the job handle being closed", alive(survivor.pid),
              "a supervisor crash must leave children to be adopted, not destroy their work")
    finally:
        survivor.kill()
        survivor.wait(timeout=10)

    for path in (tmp, lock):
        try:
            path.unlink()
        except OSError:
            pass

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + "; ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
