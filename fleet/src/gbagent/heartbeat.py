"""Stay visible while blocked (PRD-24, GRPH-496).

**Presence is derived from `last_seen_at`, and only `heartbeat` refreshes it.** Not
`update_item`, not `search_code` — an agent calling tools all day still ages out. So an agent
that does not heartbeat reads `offline` to the entire fleet one presence TTL after it
registered, which is 150 seconds by default, while it is working perfectly well.

**A thread, not a call in the turn loop, and that is the whole design.** The silence does not
come from the loop being slow; it comes from `run_tests`, which is a blocking `subprocess.run`
with a 1800s timeout, and this repository's own backend suite takes about nine minutes. A
heartbeat at the top of each turn cannot fire during the one thing that actually goes quiet.
Anything that beats only between turns is a fix that looks right and does nothing.

**What it costs if it is wrong in each direction.** Missing beats gets a working agent
declared dead, its item lease expired, and its work handed to somebody else while it is still
building — the supervisor's backstop (GRPH-452) exists to survive exactly that and is a net,
not a floor. Extra beats cost one small request every fifty seconds. The asymmetry is why this
errs toward beating.

**A failed beat is not one thing.** The client already draws the line this needs and says why:
a 5xx or a dropped connection is `ServerUnreachable` and "retrying is the right move"; a 4xx is
`ProtocolError` because "a bad credential does not become true by waiting". So a revoked seat
and a flaky network are distinguishable here, and collapsing them would either kill children
over a blip or keep a disowned one spending money.

This does NOT stop the child itself. It records that the claim is gone and the loop reads that
at its next turn boundary — because interrupting a blocked subprocess from a daemon thread is a
different and much larger decision, and the supervisor already stops disowned children.
"""
from __future__ import annotations

import logging
import threading

from gbfleet.client import ProtocolError, ServerUnreachable, ToolFailed

from .coord import Coordinator

logger = logging.getLogger("gbagent.heartbeat")

#: Used only when the server does not say. It answers with its own cadence on every
#: presence-only beat, so this is the value for the window before the first reply lands.
FALLBACK_INTERVAL = 30.0

#: Tool-failure codes that mean this agent's claim is gone rather than that a call was
#: malformed. `validation` is `heartbeat`'s "no registered agent" — the row was dismissed.
#: `conflict` is "not the lease holder" — somebody else holds the item now. Both mean the
#: same thing to a child: keep building and you are building for nobody.
GONE_CODES = frozenset({"validation", "conflict"})


class Heartbeat:
    """Beats on a timer in a daemon thread. Start it, work, stop it."""

    def __init__(self, coordinator: Coordinator, *, interval: float | None = None) -> None:
        self._coord = coordinator
        self._interval = interval or FALLBACK_INTERVAL
        self._explicit = interval is not None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: Set when the server says this agent's claim is gone. Read by the loop.
        self.gone: str = ""
        #: Beats that actually landed. The number a test can assert on, and the number that
        #: tells a walk whether this ever worked at all.
        self.beats = 0
        #: The last transient failure, kept for the handoff note. Transient ones are not
        #: findings on their own, but "twelve of them" is.
        self.misses = 0

    @property
    def interval(self) -> float:
        return self._interval

    def start(self) -> None:
        """Learn the cadence from the server, then beat until stopped.

        The cadence call is also the first liveness check: if the seat does not authenticate,
        this is where that is discovered, before a turn has been spent on it.
        """
        if not self._explicit:
            try:
                said = self._coord.cadence().get("heartbeat_interval_seconds")
                if isinstance(said, (int, float)) and said > 0:
                    self._interval = float(said)
            except (ServerUnreachable, ProtocolError, ToolFailed) as exc:
                # Not fatal. A cadence we could not ask for is what FALLBACK_INTERVAL is
                # for, and the beats below will surface a real problem soon enough.
                logger.info("heartbeat: could not read the cadence (%s); using %.0fs",
                            type(exc).__name__, self._interval)
        self._thread = threading.Thread(target=self._run, name="gbagent-heartbeat",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        # Waits on the Event rather than sleeping, so `stop()` returns promptly instead of
        # after a full interval. A child that has finished should not spend fifty seconds
        # being tidy.
        while not self._stop.wait(self._interval):
            self._beat_once()

    def _beat_once(self) -> None:
        try:
            self._coord.beat()
            self.beats += 1
        except ServerUnreachable as exc:
            # The network, or a 5xx. Nothing is known about the seat, so nothing is decided.
            self.misses += 1
            logger.info("heartbeat: server unreachable (%s)", exc)
        except ToolFailed as exc:
            if exc.code in GONE_CODES:
                self.gone = f"the server refused a heartbeat: {exc.code}: {exc}"
                self._stop.set()
            else:
                self.misses += 1
                logger.info("heartbeat: refused (%s)", exc)
        except ProtocolError as exc:
            # A 4xx. The credential itself is no longer accepted, and waiting cannot fix it.
            self.gone = f"the server no longer accepts this credential: {exc}"
            self._stop.set()
