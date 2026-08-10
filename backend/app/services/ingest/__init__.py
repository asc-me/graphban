"""Transcript ingest: sessions as a first-class input alongside items (GRPH-304 / PRD-16).

Graphban learns from what people write DOWN about work. The richer signal — what actually
happened in tool calls, exit codes, guard fires and corrections — was never read.

Two shapes carry the whole design:

- `Event` is what every harness normalises to. `kind` is a plain string, not an enum, so a
  new harness adds a value without touching the core — an enum would make every adapter a
  change to shared code, which is how a plugin point stops being one.
- `IngestAdapter` is `discover()` + `parse(source, watermark)`. The watermark is opaque to
  the caller and owned by the adapter, because "where did I get to" means a byte offset in
  one harness and a row id in another, and a core that knows which is a core that has to
  learn every harness.

**Resilience is a hard requirement, not a nicety.** A malformed record, a locked file, or a
path that vanished mid-run is a WARN and a skip. An ingest that dies on the first bad line
of a 40k-line transcript is an ingest nobody leaves switched on.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass
class Event:
    """One normalised thing that happened in a session.

    `text` is what gets learned from; everything else is the context that makes a lesson
    attributable. All of it passes through the redactor before any row is written.
    """

    session_id: str
    harness: str
    project: str
    ts: str
    kind: str
    text: str = ""
    tool_name: str = "" 
    exit_code: int | None = None
    metadata: dict = field(default_factory=dict)


class IngestAdapter(Protocol):
    """A source of session transcripts."""

    name: str

    def discover(self) -> list[str]:
        """Sources available now. Never raises — an unreadable root is an empty list."""
        ...

    def parse(self, source: str, watermark: str | None) -> tuple[list[Event], str | None]:
        """Events after `watermark`, and the new watermark. Opaque to the caller."""
        ...
