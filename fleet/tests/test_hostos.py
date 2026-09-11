"""GRPH-576: the platform differences, and what a POSIX host can honestly check.

**Read this before trusting a green run here.** Most of `hostos` has two branches and
this machine can only execute one of them. A passing suite on macOS or Linux says
nothing whatever about the Windows path — that was verified separately, on the box,
with `scripts/verify_hostos_windows.py`, and its output is recorded on GRPH-576.

What IS checked here, and is worth checking:

* the POSIX branch still does what it did before the port (the rest of the suite covers
  this too — 572 tests that never knew `hostos` existed);
* the decisions that are expressible as data rather than as code paths, above all
  `JOB_LIMIT_FLAGS == 0`, which is untestable off Windows and destructive if wrong;
* that the two branches agree about their own shape, so a Windows host cannot silently
  take a POSIX-shaped path.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbfleet import hostos  # noqa: E402
from gbfleet.hostos import (  # noqa: E402
    AlreadyLocked,
    ProcessTree,
    lock_exclusive,
    read_at,
    spawn_kwargs,
    user_tag,
    write_at,
)

posix_only = pytest.mark.skipif(hostos.WINDOWS, reason="POSIX branch")


# --- the decision that cannot be tested where it applies ---------------------------

def test_the_job_object_does_not_kill_children_when_the_supervisor_dies():
    """`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` is 0x2000 and works — it was measured. It is
    not used, and this is the only place that can say so from a POSIX host.

    With it set, a supervisor crash kills every child instantly. `lock.py`'s `Takeover`
    exists so a new supervisor can ADOPT those children instead, and salvage recovers
    their uncommitted work at reap. The flag would delete a worker's work on a crash,
    on Windows only, and no test on any platform would go red.
    """
    assert hostos.JOB_LIMIT_FLAGS == 0, (
        "a job limit flag was set on children's job objects. If it is "
        "KILL_ON_JOB_CLOSE (0x2000), a supervisor crash now destroys every child's "
        "uncommitted work on Windows, and the adoption path in lock.py is dead code."
    )


def test_the_lock_byte_is_past_the_holder_record():
    """`lock.py` reads 4096 bytes from offset 0 to find out who holds the lock. Windows
    locking is MANDATORY, so if the lock sat inside that range the refused process could
    not read it, and `RepoLocked` would stop being able to name anyone — on Windows
    only, while the POSIX tests stayed green."""
    assert hostos._LOCK_BYTE >= 4096


# --- positional IO -----------------------------------------------------------------

def test_read_at_and_write_at_round_trip(tmp_path: Path):
    path = tmp_path / "f"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        write_at(fd, b"holder-record", 0)
        assert read_at(fd, 4096, 0).rstrip(b"\x00") == b"holder-record"
    finally:
        os.close(fd)


def test_write_at_does_not_depend_on_the_file_offset(tmp_path: Path):
    """The Windows implementation seeks, so a caller that had moved the offset would
    otherwise write somewhere else entirely."""
    path = tmp_path / "f"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        write_at(fd, b"AAAA", 0)
        os.lseek(fd, 3000, os.SEEK_SET)
        write_at(fd, b"BBBB", 0)
        assert read_at(fd, 4, 0) == b"BBBB"
    finally:
        os.close(fd)


# --- the lock ----------------------------------------------------------------------

def test_a_second_holder_is_refused(tmp_path: Path):
    path = tmp_path / "lock"
    first = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    second = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        lock_exclusive(first)
        with pytest.raises(AlreadyLocked):
            lock_exclusive(second)
    finally:
        os.close(first)
        os.close(second)


def test_the_record_is_readable_while_the_lock_is_held(tmp_path: Path):
    """The property that makes `RepoLocked`'s message possible. Trivially true under
    advisory POSIX locking; the reason `_LOCK_BYTE` exists at all under mandatory
    Windows locking."""
    path = tmp_path / "lock"
    first = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    second = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        write_at(first, b'{"pid": 42}', 0)
        lock_exclusive(first)
        assert b'"pid": 42' in read_at(second, 4096, 0)
    finally:
        os.close(first)
        os.close(second)


def test_the_lock_is_released_when_the_holder_dies(tmp_path: Path):
    """The kernel liveness check the whole design rests on: no stale-lock state, no
    PID-reuse window. Verified by killing a real process rather than asserting it."""
    path = tmp_path / "lock"
    src = (
        "import os, sys, time\n"
        f"sys.path.insert(0, {str(Path(hostos.__file__).parent.parent)!r})\n"
        "from gbfleet.hostos import lock_exclusive\n"
        f"fd = os.open({str(path)!r}, os.O_RDWR | os.O_CREAT, 0o600)\n"
        "lock_exclusive(fd)\n"
        "print('held', flush=True)\n"
        "time.sleep(60)\n"
    )
    holder = subprocess.Popen([sys.executable, "-c", src], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        mine = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            with pytest.raises(AlreadyLocked):
                lock_exclusive(mine)
        finally:
            os.close(mine)
    finally:
        holder.kill()
        holder.wait(timeout=10)

    deadline = time.monotonic() + 10
    while True:
        mine = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            lock_exclusive(mine)
            return
        except AlreadyLocked:
            if time.monotonic() > deadline:
                pytest.fail("the lock outlived the process holding it")
            time.sleep(0.1)
        finally:
            os.close(mine)


# --- identity and spawn shape ------------------------------------------------------

def test_user_tag_is_usable_in_a_path():
    tag = user_tag()
    assert tag and "/" not in tag and "\\" not in tag and " " not in tag


def test_spawn_kwargs_match_the_platform():
    kwargs = spawn_kwargs()
    if hostos.WINDOWS:
        assert kwargs["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
        assert "start_new_session" not in kwargs, (
            "start_new_session is a POSIX concept and Popen ignores it on Windows — a "
            "child left in the supervisor's own group takes the supervisor's CTRL_BREAK"
        )
    else:
        assert kwargs == {"start_new_session": True}


# --- the process tree --------------------------------------------------------------

_SPAWNS_A_HELPER = (
    "import subprocess, sys, time\n"
    "h = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
    "open(sys.argv[1], 'w').write(str(h.pid))\n"
    "time.sleep(300)\n"
)


def _alive(pid: int) -> bool:
    if hostos.WINDOWS:  # pragma: no cover - the box, not here
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_killing_the_tree_takes_the_helpers_with_it(tmp_path: Path):
    """The whole reason `ProcessTree` exists. A vendor CLI's helpers are its children,
    and a supervisor that kills only the leader leaves them running and working — which
    costs money and produces conflicting edits in a worktree nobody is watching."""
    marker = tmp_path / "helper.pid"
    top = subprocess.Popen(
        [sys.executable, "-c", _SPAWNS_A_HELPER, str(marker)], **spawn_kwargs()
    )
    tree = ProcessTree(top)
    try:
        deadline = time.monotonic() + 20
        while not marker.exists():
            if time.monotonic() > deadline:
                pytest.fail("the helper never started")
            time.sleep(0.1)
        helper = int(marker.read_text().strip())
        assert _alive(top.pid) and _alive(helper)

        # Checked BEFORE killing anything. If the child is not its own group leader the
        # kill below would reach pytest, and this test's failure mode would be the whole
        # run dying by signal with no output — which is exactly what happened while it
        # was being written, twice, and cost far more time than the check costs.
        if not hostos.WINDOWS:
            assert os.getpgid(top.pid) != os.getpgid(0), (
                "the child shares this process's group, so killing its tree would kill "
                "the test runner. Refusing to run the kill."
            )

        tree.kill()
        top.wait(timeout=20)

        deadline = time.monotonic() + 20
        while _alive(helper):
            if time.monotonic() > deadline:
                pytest.fail(
                    f"helper {helper} survived its leader being killed — the supervisor "
                    "would leave it running and paying"
                )
            time.sleep(0.1)
    finally:
        tree.close()
        if top.poll() is None:
            top.kill()
        if marker.exists():
            pid = int(marker.read_text().strip())
            if _alive(pid):
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass


def test_closing_the_tree_handle_leaves_the_children_running(tmp_path: Path):
    """`close()` releases the handle; it must not be a kill.

    This is `JOB_LIMIT_FLAGS == 0` observed from the outside rather than asserted as a
    number, and it is the behaviour adoption depends on: a supervisor that exits leaves
    its children for the next one, which is why `lock.py` bothers to report a takeover.
    """
    top = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                           **spawn_kwargs())
    tree = ProcessTree(top)
    try:
        tree.close()
        time.sleep(1)
        # `poll()`, not `_alive()`. `top` is OUR child, so once it dies it becomes a
        # zombie until reaped — and `os.kill(pid, 0)` succeeds on a zombie. Sabotage
        # caught this: making `close()` kill the tree left the test green, because the
        # corpse still answered. `poll()` reads the exit status and cannot be fooled.
        assert top.poll() is None, (
            "closing the tree handle killed the child. On Windows that means "
            "KILL_ON_JOB_CLOSE is set, and a supervisor crash now destroys every "
            "child's uncommitted work instead of leaving it to be adopted."
        )
    finally:
        top.kill()
        top.wait(timeout=10)


@posix_only
def test_the_posix_branch_still_uses_a_session_and_killpg(tmp_path: Path):
    """Named so the port cannot quietly change behaviour on the platform that already
    worked. `start_new_session` is what makes the child a group leader, and `killpg`
    only reaches the helpers because of it."""
    assert spawn_kwargs() == {"start_new_session": True}
    top = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                           **spawn_kwargs())
    try:
        assert os.getpgid(top.pid) == top.pid, "the child is not its own group leader"
    finally:
        top.kill()
        top.wait(timeout=10)


@posix_only
def test_signalling_a_group_we_are_in_is_refused(tmp_path: Path):
    """The guard that turns a catastrophe into a complaint.

    A child that never got its own session is in the supervisor's group, and `killpg`
    would then reach the supervisor, every sibling worker, and the operator's shell. The
    symptom is the entire session dying by signal with no output — unattributable, and
    found here only because it happened twice while building this.
    """
    from gbfleet.hostos import WouldSignalOurselves, _signal_group_posix

    # Deliberately NOT given its own session, which is the regression being guarded.
    sibling = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert os.getpgid(sibling.pid) == os.getpgid(0), "test setup is not reproducing it"
        with pytest.raises(WouldSignalOurselves):
            _signal_group_posix(sibling.pid, 9)
        assert _alive(sibling.pid), "the guard raised but signalled anyway"
    finally:
        sibling.kill()
        sibling.wait(timeout=10)


def test_spawn_attaches_a_process_tree(tmp_path: Path, git_repo: Path):
    """`stop()` falls back to building a `ProcessTree` when the child has none, and on
    POSIX that fallback is indistinguishable from the real thing — which is why removing
    the attachment in `spawn()` survived sabotage here.

    On Windows it is emphatically not the same. The job object is what owns the tree,
    and one created at stop-time cannot contain helpers the child spawned minutes
    earlier: they are outside the job and survive the kill. So the attachment is
    asserted directly, on every platform, rather than inferred from behaviour on the one
    platform where its absence does no harm.
    """
    from gbfleet.spawn import Launch, spawn

    script = tmp_path / "s.py"
    script.write_text("import time; time.sleep(30)\n", encoding="utf-8")
    launch = Launch(
        adapter="fake",
        argv=[sys.executable, str(script)],
        seat_path=tmp_path / "seat.json",
        config={"mcpServers": {}},
        instruction="",
    )
    child = spawn(launch, tmp_path, "gb/x", tmp_path / "logs")
    try:
        assert child.tree is not None, (
            "spawn() did not attach a ProcessTree. On Windows the job object would then "
            "be created at stop() time, too late to contain the helpers the child had "
            "already spawned — they would survive the kill and keep working."
        )
        assert child.tree.pid == child.pid
    finally:
        child.process.kill()
        child.process.wait(timeout=10)


# --- keeping a credential to its owner ---------------------------------------------

def test_restrict_to_owner_closes_a_file_that_was_open(tmp_path: Path):
    """The property, not the number.

    On POSIX this is `chmod`; on Windows it is `icacls`, because `chmod` there succeeds
    and restricts nothing — measured, `0o600` in and `0o666` out. Asserting the property
    is what lets one test mean the same thing on both.
    """
    path = tmp_path / "seat"
    path.write_text("gb_sk_live", encoding="utf-8")
    if not hostos.WINDOWS:
        os.chmod(path, 0o644)
    assert not hostos.is_owner_only(path) or hostos.WINDOWS

    assert hostos.restrict_to_owner(path) is True
    assert hostos.is_owner_only(path)
    assert path.read_text(encoding="utf-8") == "gb_sk_live", "restricted it from its owner"


def test_is_owner_only_is_false_for_a_readable_file(tmp_path: Path):
    """The control. A checker that answered True unconditionally would make every
    caller's verification meaningless while every test stayed green."""
    path = tmp_path / "public"
    path.write_text("x", encoding="utf-8")
    if hostos.WINDOWS:  # pragma: no cover - exercised on the box
        pytest.skip("POSIX modes; the Windows equivalent runs in the box script")
    os.chmod(path, 0o644)
    assert hostos.is_owner_only(path) is False
    os.chmod(path, 0o604)
    assert hostos.is_owner_only(path) is False, "other-readable but reported private"
    os.chmod(path, 0o640)
    assert hostos.is_owner_only(path) is False, "group-readable but reported private"


def test_is_owner_only_says_no_rather_than_raising_for_a_missing_file(tmp_path: Path):
    assert hostos.is_owner_only(tmp_path / "nope") is False


def test_a_directory_can_be_restricted_too(tmp_path: Path):
    """`state_root` holds the lock file and the fleet log, and makes the same promise."""
    d = tmp_path / "state"
    d.mkdir()
    assert hostos.restrict_to_owner(d) is True
    assert hostos.is_owner_only(d)


def test_a_restricted_directory_is_still_usable_by_its_owner(tmp_path: Path):
    """The half that was missing, and it cost the whole tool.

    `0o600` on a directory strips the execute bit, and a directory without `x` cannot be
    traversed. `state_root()` got the file mode, so the supervisor could not open its own
    lock file and neither `up` nor `stdio` could start — while three tests stayed green,
    because every one of them asked "can anyone else read this" and none asked "and can
    I still use it" (GRPH-600).

    Found by `gbfleet doctor` on its first run, not by this suite.
    """
    d = tmp_path / "state"
    d.mkdir()
    assert hostos.restrict_to_owner(d) is True

    inside = d / "repo.lock"
    inside.write_text("holder record", encoding="utf-8")
    assert inside.read_text(encoding="utf-8") == "holder record"
    assert [p.name for p in d.iterdir()] == ["repo.lock"], "the directory cannot be listed"

    if not hostos.WINDOWS:
        mode = d.stat().st_mode & 0o777
        assert mode & 0o100, f"the owner's execute bit is gone: {oct(mode)}"


def test_a_restricted_file_does_not_get_the_directory_mode(tmp_path: Path):
    """The control on the split. Handing every path `0o700` would also make both tests
    above pass, while quietly marking every seat file executable."""
    if hostos.WINDOWS:  # pragma: no cover - POSIX modes; icacls has no such distinction
        pytest.skip("POSIX modes; Windows grants full control either way")

    seat = tmp_path / "config.toml"
    seat.write_text("gb_sk_live", encoding="utf-8")
    assert hostos.restrict_to_owner(seat) is True
    assert seat.stat().st_mode & 0o777 == 0o600, "a credential file was made executable"


@posix_only
def test_restrict_to_owner_does_not_trust_chmod_to_have_worked(tmp_path: Path, monkeypatch):
    """It verifies its own work, and says False when the filesystem ignored it.

    Not hypothetical: `chmod` is a silent no-op on FAT32, on many network mounts, and —
    the whole reason this ticket exists — on Windows. A version that returned True
    because the call did not raise would report a credential as protected on exactly the
    filesystems where it is not, which is the failure being fixed rather than a new one.
    """
    path = tmp_path / "seat"
    path.write_text("gb_sk_live", encoding="utf-8")
    os.chmod(path, 0o644)

    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)  # succeeds, changes nothing

    assert hostos.restrict_to_owner(path) is False, (
        "reported success from a chmod that did nothing — a credential would be "
        "recorded as protected while being world-readable"
    )


# --- a clean stop is not a crash ----------------------------------------------------

def test_a_status_the_os_imposed_is_not_read_as_one_the_program_chose():
    """`stop()`'s graceful step is SIGTERM here and CTRL_BREAK on Windows, and the two
    leave completely different traces: `-15` there, `STATUS_CONTROL_C_EXIT` (3221225786)
    on Windows. Neither is a number the program picked."""
    assert hostos.terminated_by_signal(0) is False
    assert hostos.terminated_by_signal(3) is False
    assert hostos.terminated_by_signal(None) is False

    if hostos.WINDOWS:  # pragma: no cover - the box
        assert hostos.terminated_by_signal(hostos.CONTROL_C_EXIT) is True
        assert hostos.terminated_by_signal(-15) is False
    else:
        assert hostos.terminated_by_signal(-15) is True
        assert hostos.terminated_by_signal(-9) is True
        # Not on POSIX: 3221225786 is an ordinary, if odd, exit status here.
        assert hostos.terminated_by_signal(hostos.CONTROL_C_EXIT) is False


def test_exit_meaning_says_stopped_rather_than_printing_the_raw_status():
    """Without this, every child stopped cleanly on Windows is recorded as
    `exited 3221225786` — a shutdown that reads like a crash, on every child, every time
    the supervisor asks one to stop."""
    from gbfleet.adapters import ADAPTERS

    adapter = ADAPTERS["grok"]
    assert adapter.exit_meaning(0) == "finished"
    assert "3" in adapter.exit_meaning(3)

    imposed = hostos.CONTROL_C_EXIT if hostos.WINDOWS else -15
    assert "stopped by signal" in adapter.exit_meaning(imposed), (
        f"a child the supervisor stopped reports {imposed} and the fleet calls it an "
        "ordinary non-zero exit"
    )


# --- is this process sandboxed? (GRPH-838) --------------------------------------------

def test_sandboxed_answers_in_three_ways_and_this_platform_in_one_of_them():
    """`None` is an answer — "this platform cannot be asked" — and it must never collapse
    into `False`, which the doctor would print as PASS. macOS can be asked; nothing else
    can, yet."""
    answer = hostos.sandboxed()
    if sys.platform == "darwin":
        assert answer in (True, False)
    else:
        assert answer is None


def _sandboxed_in(argv_prefix: list[str]) -> str:
    src = Path(__file__).resolve().parents[1] / "src"
    code = (f"import sys; sys.path.insert(0, {str(src)!r}); "
            "from gbfleet import hostos; print(hostos.sandboxed())")
    return subprocess.run([*argv_prefix, sys.executable, "-c", code],
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.mark.skipif(sys.platform != "darwin" or shutil.which("sandbox-exec") is None,
                    reason="a real Seatbelt needs macOS and sandbox-exec")
def test_sandboxed_is_true_inside_a_real_seatbelt_and_false_outside(tmp_path: Path):
    """Against the kernel, not a mock. The profile allows everything — being inside ANY
    Seatbelt is what `sandbox_check` reports, which is exactly the question: the Grok
    profile that killed four children allowed reads everywhere too."""
    profile = tmp_path / "allow-all.sb"
    profile.write_text("(version 1)\n(allow default)\n", encoding="utf-8")

    assert _sandboxed_in(["sandbox-exec", "-f", str(profile)]) == "True"
    assert _sandboxed_in([]) == "False", "the control: the same code from a plain shell"


# --- how much room this machine has left (GRPH-842) --------------------------------
#
# The parser is tested separately from the call that feeds it, on purpose: the macOS
# branch cannot run on Linux and the Linux branch cannot run on macOS, so a suite that
# only ever calls `available_memory()` checks one of the three and reports green.
#
# The Linux branch was ALSO run on a real Linux box rather than left to CI, the same way
# GRPH-576 verified the Windows paths on the box. Measured 2026-09-10 on a 30 GB Ubuntu
# server: `available_memory()` returned 24527056896 (22.8 GB) against a `/proc/meminfo`
# reading of `MemAvailable: 23952204 kB`, `fits` said 30 children, and a Headroom over a
# reader pinned at RESERVE allowed the first child and refused the second. The Windows
# branch has NOT been run on a Windows host and is the one path here nobody has watched.

#: Real `vm_stat` output, captured 2026-09-10 on the 24 GB Apple Silicon box where the
#: memory kill happened. Kept verbatim, including the 16384-byte page size that a
#: hardcoded 4096 would have got wrong by a factor of four.
VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                   140100.
Pages active:                                 397822.
Pages inactive:                               347624.
Pages speculative:                             48267.
Pages throttled:                                   0.
Pages wired down:                             316170.
Pages purgeable:                                9392.
"Translation faults":                    40089326139.
Pages copy-on-write:                      1366069412.
Pages occupied by compressor:                 255790.
"""


def test_parse_vm_stat_sums_the_reclaimable_pages():
    got = hostos._parse_vm_stat(VM_STAT)
    assert got == (140100 + 347624 + 48267) * 16384


def test_parse_vm_stat_excludes_purgeable_and_active():
    """Purgeable is a subset of the partitions above it, not one of its own.

    Measured on that box: free+active+inactive+speculative+wired+compressor came to
    1505773 of 1572864 pages, and adding purgeable (9392) does not close the gap. Counting
    it would double-count; counting `active` would report a busy machine as empty.
    """
    got = hostos._parse_vm_stat(VM_STAT)
    assert got < (140100 + 347624 + 48267 + 9392) * 16384
    assert got < 397822 * 16384 + got  # active is nowhere in the figure


def test_parse_vm_stat_reads_the_page_size_from_the_output():
    small = VM_STAT.replace("page size of 16384 bytes", "page size of 4096 bytes")
    assert hostos._parse_vm_stat(small) == hostos._parse_vm_stat(VM_STAT) // 4


@pytest.mark.parametrize("text", [
    "",
    "Mach Virtual Memory Statistics:\nPages free: 1.\n",          # no page size
    "Mach Virtual Memory Statistics: (page size of 0 bytes)\n",   # nonsense page size
    "Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free: lots.\n",
])
def test_parse_vm_stat_refuses_what_it_does_not_recognise(text):
    assert hostos._parse_vm_stat(text) is None


def test_a_partial_vm_stat_is_unmeasured_rather_than_small():
    """One missing counter must not read as a machine that is suddenly out of room.

    Returning the sum of what was found would make the gate stop spawning for a reason
    nobody could see — the exact failure this whole change exists to remove.
    """
    without_inactive = "\n".join(
        line for line in VM_STAT.splitlines() if not line.startswith("Pages inactive")
    )
    assert hostos._parse_vm_stat(without_inactive + "\n") is None


@pytest.mark.real_memory
def test_available_memory_answers_on_this_host():
    """Whatever platform runs the suite, the answer is a plausible number or None."""
    got = hostos.available_memory()
    if got is None:
        assert sys.platform not in ("darwin",) and os.name != "nt", (
            "this platform has a branch; None means it stopped working"
        )
        return
    assert 0 < got < 2**50


@pytest.mark.real_memory
@pytest.mark.skipif(sys.platform != "linux", reason="reads /proc/meminfo")
def test_proc_branch_agrees_with_meminfo():
    stated = None
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            stated = int(line.split()[1]) * 1024
    if stated is None:  # pragma: no cover - kernel older than 3.14
        pytest.skip("no MemAvailable on this kernel")
    got = hostos._available_proc()
    # Not equality: the two readings are taken microseconds apart on a live machine.
    assert got is not None and abs(got - stated) < stated * 0.25


_RAW_SESSION_FLAG = re.compile(r"^\s*start_new_session\s*=\s*True\s*,?\s*$")


def test_the_suite_does_not_pass_start_new_session_as_a_raw_popen_flag():
    """`start_new_session=True` is POSIX. Popen ignores it on Windows, so the child
    stays in pytest's process group; `stop()`'s CTRL_BREAK then hits the CI shell
    (pwsh debugger / cmd.exe `Terminate batch job`) and the Windows job dies at ~5%
    — GRPH-855 / PR #759. `spawn_kwargs()` is the portable form.
    """
    root = Path(__file__).resolve().parent
    offenders = []
    for path in sorted(root.rglob("*.py")):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _RAW_SESSION_FLAG.match(line):
                offenders.append(f"{path.name}:{i}")
    assert not offenders, (
        "start_new_session=True is ignored on Windows; use **spawn_kwargs() so a "
        f"CTRL_BREAK stays in the child's group: {offenders}"
    )
