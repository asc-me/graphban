"""Whether a child is producing anything, measured locally.

GRPH-579. Between spawn and reap the supervisor's only liveness signal was the server
roster's `offline` flag, and `Limits.disowned_after` is candid about what that is worth:
`offline` means "no heartbeat within the TTL", which a revoked child and a *busy* one
produce identically. So it is set to 1800s, and a genuinely stuck child is invisible for
half an hour and then blamed on the network.

Meanwhile the child's stdout and stderr are open in `log_dir`, growing or not growing,
and nothing looked at them. That is the signal this module reads. It is worth having for
three reasons the roster cannot match: it needs no network (D-i — the supervisor's job is
to keep working when the server is unreachable), it needs no cooperation from the vendor,
and it is the same measurement for every adapter including the ones with no debug flag
at all.

**What it does not mean.** A child that has written nothing for five minutes is not
thereby stuck. It may be inside one long tool call — `gbagent`'s `run_tests` timeout
alone is 1800s — and file writes are buffered, so output arrives in chunks with real
silence between them. Both of those make silence weak evidence, which is precisely why
nothing here kills anything. `Pulse` is reported so an operator can see a child has been
quiet for twenty minutes *while it is happening*, instead of working it out afterwards
from a log. Acting on it would repeat the mistake `fleet_idle` exists to avoid: two
things owning one transition is how they come to disagree.

**Bytes, not mtime.** A log file's mtime moves when anything touches it, including a
flush that writes nothing new; bytes only move when the child actually said something.
The two disagree exactly in the case that matters, so this counts bytes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

#: Rendered instead of a duration for a child that has produced nothing at all since it
#: started. Not zero: zero reads as "it wrote something just now", which is the opposite
#: of what a child that has never written anything is doing. The same reasoning as
#: `observe.NEVER_REGISTERED`, for the same reason — a missing or zeroed field reads as
#: fine, and this is the field whose whole job is to say something is not fine.
NEVER_WROTE = "never_wrote"


@dataclass(frozen=True)
class Pulse:
    """One reading of what a child has produced.

    `silent_for` is the field an operator actually looks at, and `NEVER_WROTE` is a
    distinct answer from a number: a child silent for 600s has been working and stopped,
    a child that never wrote is one that may never have started properly. Collapsing
    them loses the difference between "stuck" and "born broken".
    """

    at: float
    #: Total bytes across every file being watched, since the child started.
    total_bytes: int
    #: New bytes since the previous reading. Zero is meaningful: it is what silence is.
    new_bytes: int
    #: Seconds since the last reading that saw new bytes, or `NEVER_WROTE`.
    silent_for: float | str
    #: Seconds since the child started.
    age: float
    #: Which files were readable at this reading. A file that has vanished is dropped
    #: rather than counted as zero — a deleted log is not a silent child, and treating
    #: it as one would report a fault in the wrong place.
    watched: int

    @property
    def quiet(self) -> bool:
        """True when this reading saw nothing new. One reading only — an operator cares
        about a run of them, which is what `silent_for` accumulates."""
        return self.new_bytes == 0

    def as_dict(self) -> dict:
        return {
            "total_bytes": self.total_bytes,
            "new_bytes": self.new_bytes,
            "silent_for": self.silent_for,
            "age": round(self.age, 1),
            "watched": self.watched,
        }


@dataclass
class Output:
    """Watches one child's files and turns successive readings into `Pulse`es.

    Sizes are re-read every sample rather than cached, because the point is to notice a
    file that has stopped growing, and a cache would report the last thing it saw
    forever.
    """

    paths: tuple[Path, ...]
    started_at: float
    _total: int = 0
    _last_wrote_at: float | None = None
    _seen_any: bool = field(default=False, init=False)

    @classmethod
    def watching(cls, paths, started_at: float) -> "Output":
        return cls(paths=tuple(Path(p) for p in paths), started_at=started_at)

    def _size(self) -> tuple[int, int]:
        total = 0
        readable = 0
        for path in self.paths:
            try:
                total += path.stat().st_size
            except OSError:
                # Missing or unreadable. Skipped, not counted as zero: a log that is not
                # there yet is a child that has not opened it, and a log that was deleted
                # is somebody else's fault. Either way it is not evidence of silence.
                continue
            readable += 1
        return total, readable

    def sample(self, now: float) -> Pulse:
        total, readable = self._size()

        # Shrinkage means the file was truncated or replaced under us. Treated as new
        # output rather than negative output: something wrote, and reporting a negative
        # delta would make the rate meaningless for the rest of the run.
        new = total - self._total if total >= self._total else total
        self._total = total

        if new > 0:
            self._last_wrote_at = now
            self._seen_any = True

        if not self._seen_any:
            silent: float | str = NEVER_WROTE
        else:
            silent = max(0.0, now - (self._last_wrote_at or self.started_at))

        return Pulse(
            at=now,
            total_bytes=total,
            new_bytes=new,
            silent_for=silent,
            age=max(0.0, now - self.started_at),
            watched=readable,
        )
