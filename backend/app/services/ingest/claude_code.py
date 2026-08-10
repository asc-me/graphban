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
        result = row.get("toolUseResult")
        # A tool RESULT is its own record, separate from the call that produced it — 910
        # results against 18,173 calls in the real corpus. Requiring text or a tool name
        # dropped every one of them, and with them the entire failure signal (GRPH-342).
        if isinstance(result, dict) and not text:
            text = _result_text(result)
        if not text and not tool and not isinstance(result, dict):
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
            metadata={"source": source, "outcome": _outcome_of(row)},
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


def _result_text(result: dict) -> str:
    """What a tool result contributes to a lesson.

    `stderr` first and deliberately: a command that FAILED is the higher-value signal, and
    burying it under kilobytes of successful stdout is how it gets lost. Both are capped —
    a full build log is not a lesson, and the promotion ladder scores text it can compare.
    """
    err = str(result.get("stderr") or "").strip()
    if err:
        return f"stderr: {err[:600]}"
    out = str(result.get("stdout") or "").strip()
    return f"stdout: {out[:600]}" if out else ""


def _tool_of(row: dict) -> str:
    msg = row.get("message")
    blocks = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(blocks, list):
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name"):
                return str(b["name"])
    return ""


def _exit_of(row: dict) -> int | None:
    """A numeric exit code, when the harness records one.

    Claude Code does NOT — verified against 74 real transcripts: 18,167 tool calls and
    zero numeric codes. The field stays because the `Event` shape is shared and other
    harnesses do emit one; read `metadata["outcome"]` for the signal that is actually
    available here.
    """
    result = row.get("toolUseResult")
    if isinstance(result, dict):
        for key in ("exitCode", "exit_code", "returncode"):
            if isinstance(result.get(key), int):
                return result[key]
    return None


def _outcome_of(row: dict) -> str:
    """`ok` | `failed` | `unknown` — did this tool call work (GRPH-342).

    PRD-16 makes the failure signal first-class because "a lesson about a command that
    FAILED is worth more than one about a command that ran". `exit_code` cannot carry it
    for this harness, and leaving it at None meant every one of 18,167 real tool calls read
    as untroubled — `None` conflating "succeeded" with "not recorded".

    So the outcome is derived from what Claude Code actually writes: `stderr`, `interrupted`
    and `returnCodeInterpretation` (a human-readable gloss on a non-zero code). `unknown`
    when the record carries no result at all, which is most non-tool events and is not a
    claim that anything went well.
    """
    result = row.get("toolUseResult")
    if not isinstance(result, dict):
        return "unknown"
    if result.get("interrupted") is True:
        return "failed"
    if str(result.get("stderr") or "").strip():
        return "failed"
    # Present only when the command returned non-zero — the gloss IS the signal.
    if str(result.get("returnCodeInterpretation") or "").strip():
        return "failed"
    if "stdout" in result or "filePath" in result or "type" in result:
        return "ok"
    return "unknown"
