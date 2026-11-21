"""A supervisor that cannot see the server must say so, and must not wait forever (GRPH-1179).

`until` used to catch `ServerUnreachable`, sleep, and `continue` without writing a line. Seen
three times: a stalled wave looked exactly like an idle one from outside — absence reading as
clean, in the supervisor itself. This module is the things that close that:

* `retry` — every swallowed server failure is a log event naming the tool, the error and the
  retry count. No silent `continue`.
* `Watchdog` — progress is a SUCCESSFUL round trip or a real action, never a retry line. When
  there has been none for `stall_after` seconds and no child is live, it says `stall` and
  raises, so the process exits non-zero and the service manager (KeepAlive) restarts it.
* `SilenceWatch` — the loop itself writes nothing for longer than `SILENT_POLLS` poll
  intervals, including while a child is live and including while the loop is blocked inside
  a call. It logs `loop_silent` and does not exit: the child may still be pushing (GRPH-1786).
* `install_stack_dump` — `kill -USR1 <pid>` writes every thread's Python stack to the log.
"""

from __future__ import annotations

import faulthandler
import logging
import signal
import sys
import threading
import time
from typing import Callable

from . import observe

#: Ten minutes: longer than any healthy poll, shorter than the 10–30 minute silences seen.
STALL_AFTER_S = 600.0

#: How many of `until`'s own poll intervals of log silence is a stalled loop (GRPH-1786).
#: `until` polls at 1s and has no `--poll` flag, so this is five minutes. Longer than two
#: back-to-back 120s git calls, far shorter than the 50 minute silence that looked healthy
#: to launchd. `service status` reads the same event, not a second number.
SILENT_POLLS = 300

_dump_file = None


def install_stack_dump(stream=None) -> bool:
    """Register SIGUSR1 to dump all threads' stacks. False where the platform has no SIGUSR1."""
    global _dump_file
    sig = getattr(signal, "SIGUSR1", None)
    if sig is None or not hasattr(faulthandler, "register"):  # Windows has neither
        return False
    target = stream
    if target is None:
        # The service log is stdout/stderr redirected by the unit; faulthandler needs a real fd.
        target = sys.stderr
        try:
            target.fileno()
        except (AttributeError, OSError, ValueError):
            target = sys.__stderr__
    _dump_file = target
    faulthandler.register(sig, file=target, all_threads=True)
    observe.emit("stack_dump_registered", signal="SIGUSR1")
    return True


class Stalled(RuntimeError):
    """No progress for `stall_after` seconds with no live child."""


class Watchdog:
    def __init__(self, stall_after: float = STALL_AFTER_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.stall_after = stall_after
        self.clock = clock
        self.last_progress = clock()
        self.last_action = "start"
        self.retries = 0

    def progress(self, action: str) -> None:
        self.last_progress = self.clock()
        self.last_action = action
        self.retries = 0

    def retry(self, where: str, exc: BaseException, *, tool: str = "") -> None:
        self.retries += 1
        observe.emit("retry", where=where, tool=tool or getattr(exc, "tool", "") or where,
                     error=str(exc), retry=self.retries,
                     silent_for=round(self.clock() - self.last_progress, 1))

    def check(self, live: bool) -> None:
        """Raise `Stalled` (after logging `stall`) when nothing has progressed and nothing runs."""
        if self.stall_after <= 0 or live:
            return
        quiet = self.clock() - self.last_progress
        if quiet >= self.stall_after:
            observe.emit("stall", last_action=self.last_action, silent_for=round(quiet, 1),
                         stall_after=self.stall_after, retries=self.retries)
            raise Stalled(f"no progress for {quiet:.0f}s; last action: {self.last_action}")


class SilenceWatch:
    """Emit `loop_silent` when the until loop has not written for `polls` intervals.

    Runs on its own thread, because the defect is a loop that cannot reach its own
    `check`. A live child does not suppress it: the 2026-11-20 drain still had one
    child pushing and wrote nothing for 50 minutes. The thread sleeps with
    `time.sleep`. `Event.wait` is the call that did not return.
    """

    def __init__(self, polls: int = SILENT_POLLS, poll: float = 1.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.polls = polls
        self.poll = poll
        self.after = polls * poll
        self.clock = clock
        self._stop = False
        self._fired = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.after <= 0 or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="gbfleet-silence", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop = True

    def _run(self) -> None:
        step = min(0.25, self.after) if self.after > 0 else 0.25
        while not self._stop:
            time.sleep(step)
            if self._stop:
                return
            try:
                self.check()
            except Exception:  # noqa: BLE001 - a watchdog that dies is the silence again
                logging.getLogger("gbfleet").debug("silence watch failed", exc_info=True)

    def check(self) -> bool:
        """Log `loop_silent` once per quiet stretch. True when the loop is silent."""
        if self.after <= 0:
            return False
        last = observe.loop_last_write()
        if last <= 0:
            return False
        quiet = self.clock() - last
        if quiet < self.after:
            self._fired = False
            return False
        if not self._fired:
            observe.emit(observe.LOOP_SILENT, silent_for=round(quiet, 1),
                         poll=self.poll, polls=self.polls)
            self._fired = True
        return True


#: The running `until`'s watchdog, so module-level helpers (`_any_holdings`) can log through it.
current: Watchdog = Watchdog(0)
