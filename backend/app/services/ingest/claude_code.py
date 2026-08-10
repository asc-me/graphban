"""Claude Code transcripts: append-only JSONL (GRPH-304 / PRD-16).

Shipped first because append-only JSONL is the simplest watermark shape there is — a line
count. Nothing rewrites earlier lines, so "resume after N" is exact rather than a guess,
and getting the incremental contract right on the easy case is what makes the hard ones
(a database cursor, a rotating log) implementable against the same interface.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from app.services.ingest import Event

logger = logging.getLogger(__name__)

# Where Claude Code keeps session transcripts. Overridable so a test never depends on the
# developer's home directory, and so an operator can point at an archive.
_ROOT_ENV = "GRAPHBAN_CLAUDE_TRANSCRIPTS"
_DEFAULT_ROOT = "~/.claude/projects"


class ClaudeCodeAdapter:
    name = "claude-code"

    def __init__(self, root: str | None = None):
        self.root = Path(os.path.expanduser(root or os.getenv(_ROOT_ENV) or _DEFAULT_ROOT))

    def discover(self) -> list[str]:
        """Every `.jsonl` under the root, sorted for a stable run order.

        No try/except here, deliberately. `rglob` already yields nothing for a root that is
        missing, unreadable, or not a directory — verified, not assumed: an `except OSError`
        sat here first and sabotage showed it could never fire, because pathlib swallows all
        three. An unreachable handler is worse than none, since it implies a guarantee that
        something else is actually providing.

        The resilience that IS load-bearing lives in `parse`, where `open()` genuinely
        raises on a locked file mid-run.
        """
        return sorted(str(p) for p in self.root.rglob("*.jsonl") if p.is_file())

    def parse(self, source: str, watermark: str | None) -> tuple[list[Event], str | None]:
        """Events after line `watermark`, and the new line count.

        The watermark is a LINE COUNT rather than a byte offset. Both work for append-only
        data, but a line count survives a file being rewritten with different line endings,
        and it is readable in the database when someone is working out why a run skipped
        something.
        """
        start = int(watermark or 0)
        events: list[Event] = []
        seen = 0
        try:
            with open(source, encoding="utf-8", errors="replace") as fh:
                for seen, line in enumerate(fh, start=1):
                    if seen <= start:
                        continue
                    ev = self._event(source, line)
                    if ev is not None:
                        events.append(ev)
        except OSError as e:
            # Locked, deleted mid-run, or permission-denied. The run continues with the
            # watermark unmoved, so nothing is lost and the next run picks it up.
            logger.warning("ingest skipped %s: %s", source, e)
            return [], watermark
        return events, str(max(seen, start))

    def _event(self, source: str, line: str) -> Event | None:
        """One JSONL line to an Event, or None if it carries nothing to learn from.

        A malformed record is a WARN and a skip, never an exception — a single truncated
        line at the end of a live transcript is the NORMAL state of a session in progress,
        not a corruption worth failing a run over.
        """
        line = line.strip()
        if not line:
            return None
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            logger.warning("ingest skipped a malformed record in %s", source)
            return None
        if not isinstance(row, dict):
            logger.warning("ingest skipped a non-object record in %s", source)
            return None

        text = _text_of(row.get("message") or row)
        tool = _tool_of(row)
        if not text and not tool:
            return None
        return Event(
            session_id=str(row.get("sessionId") or Path(source).stem),
            harness=self.name,
            project=str(row.get("cwd") or ""),
            ts=str(row.get("timestamp") or ""),
            # A plain string, so a harness with a kind we have never seen costs no change
            # here (PRD-16).
            kind=str(row.get("type") or "unknown"),
            text=text,
            tool_name=tool,
            exit_code=_exit_of(row),
            metadata={"source": source},
        )


def _text_of(message) -> str:
    """The human-readable text of a record, whatever shape it arrived in."""
    if isinstance(message, str):
        return message
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [c.get("text", "") for c in content
                 if isinstance(c, dict) and c.get("type") == "text"]
        return "\n".join(p for p in parts if p)
    return ""


def _tool_of(row: dict) -> str:
    msg = row.get("message")
    blocks = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(blocks, list):
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name"):
                return str(b["name"])
    return ""


def _exit_of(row: dict) -> int | None:
    """Exit codes are a first-class signal — a lesson about a command that FAILED is worth
    more than one about a command that ran."""
    result = row.get("toolUseResult")
    if isinstance(result, dict):
        for key in ("exitCode", "exit_code", "returncode"):
            if isinstance(result.get(key), int):
                return result[key]
    return None
