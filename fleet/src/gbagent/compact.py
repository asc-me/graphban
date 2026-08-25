"""Compaction: drop tool output, never instructions (PRD-24 S4, D7, AC-9).

**The loop does this, not the model.** There is no `compact` tool, so the agent cannot forget
to call it, call it wrongly, or spend a 30-second turn deciding to. The trigger is checked after
every turn and acted on before the next one.

**Nothing is deleted — old tool RESULTS are shortened in place.** That is not a stylistic
choice. Every `tool` message answers a `tool_call` in the assistant message before it, and an
endpoint that receives a call with no answer rejects the conversation. Removing messages means
tracking that pairing and getting it right on every path; rewriting a result's `content` leaves
the structure exactly as the model built it and touches only the thing D7 names. A model that
forgets what it was asked to do while remembering a directory listing is worse than one that
stops, and the surest way not to drop an instruction is to have no code that can.

**Always kept verbatim:** the system instruction, the item and its description, the plan the
agent wrote, and the last five turns. The first three are kept BY CONSTRUCTION rather than by a
condition — they are not tool results, and nothing that is not a tool result is ever touched.
An earlier draft guarded them explicitly with `index < 2 or index == plan`, which sabotage
showed could not fire: no `tool` message is ever the system prompt or a prose turn. A guard that
cannot fail is not protection, it is a comment that looks like protection, so the comment is
what it now is. Only the last-five-turns boundary is a real condition, because a tool result
genuinely can be on either side of it.

**Short results are left alone.** Summarising a 40-character refusal into a 60-character note
about a refusal makes the context bigger and the run worse.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

#: D7. Measured in tokens rather than turns, because turns vary by three orders of magnitude:
#: a `list_dir` costs a few hundred and a `run_tests` on a failing suite costs thousands.
DEFAULT_THRESHOLD = 0.70

#: Turns kept verbatim at the end of the conversation (D7).
KEEP_LAST_TURNS = 5

#: Only results longer than this are summarised.
MIN_SUMMARISE_CHARS = 240

#: Characters per token, MEASURED rather than assumed — 3.47 to 4.03 against `qwen3-coder:30b`
#: and `gpt-oss:20b` on this repository's own Python, Markdown and tool results. 3.5 is the low
#: end of that range, chosen deliberately: this is only the fallback for an endpoint that reports
#: no usage, and an estimate that runs high compacts early while one that runs low compacts too
#: late. Only one of those two failures loses a run.
CHARS_PER_TOKEN = 3.5


@dataclass
class Compaction:
    """What compaction did, in numbers a run can be judged by."""

    messages: list[dict]
    summarised: int = 0
    chars_before: int = 0
    chars_after: int = 0
    #: Results that were over the threshold but sat inside the kept region. Reported rather than
    #: forced: they are the last five turns and the plan, which D7 says are what survive.
    protected: int = 0

    @property
    def freed_chars(self) -> int:
        return self.chars_before - self.chars_after

    def __str__(self) -> str:
        return (f"compacted {self.summarised} tool results, "
                f"{self.chars_before} -> {self.chars_after} chars")


def estimate_tokens(messages: list[dict]) -> int:
    """A fallback, and it says so. Real counts come from the endpoint's own tokenizer.

    Used only when a turn reports no usage. Reporting 0 in that case — which is what
    `usage.get("prompt_tokens", 0)` would hand back — is the same defect as a test count of
    zero on an unreadable run: it reads as "plenty of room left" and compaction never fires.
    """
    return int(sum(len(_text_of(m)) for m in messages) / CHARS_PER_TOKEN)


def _text_of(message: dict) -> str:
    """Everything in a message that costs tokens, including the tool-call arguments."""
    parts = [str(message.get("content") or ""), str(message.get("reasoning") or "")]
    for call in message.get("tool_calls") or []:  # arguments carry whole file bodies
        fn = call.get("function") or {}
        parts.append(str(fn.get("name") or ""))
        parts.append(str(fn.get("arguments") or ""))
    return "".join(parts)


def should_compact(reported: int | None, messages: list[dict], window: int,
                   threshold: float = DEFAULT_THRESHOLD) -> bool:
    """Has the conversation crossed `threshold` of the model's window?

    `reported` is the endpoint's `prompt_tokens` for the last turn — the model's own count,
    which cannot be wrong. `None` or zero means it did not say, and then the estimate is used
    rather than treating silence as room to spare.
    """
    if window < 1:
        raise ValueError(f"window must be a positive number of tokens, got {window}")
    used = reported if reported else estimate_tokens(messages)
    return used >= int(window * threshold)


def _keep_boundary(messages: list[dict], keep_last_turns: int) -> int:
    """Index of the first message that must be kept verbatim as one of the last N turns.

    A turn is an assistant message and whatever answered it, so counting assistant messages
    backwards is what "five turns" means. Fewer than N assistant messages means everything is
    recent and nothing is compactable, which is the correct answer rather than an edge case.
    """
    seen = 0
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "assistant":
            seen += 1
            if seen == keep_last_turns:
                return index
    return 0


def _call_names(messages: list[dict]) -> dict[str, str]:
    """`tool_call_id` -> a short description of what was asked for.

    A `tool` message carries only its id and its content; the name and arguments live in the
    assistant message that requested it. Without this correlation a summary can only say "some
    output was here", which tells the model nothing about whether to ask again.
    """
    names: dict[str, str] = {}
    for message in messages:
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            # Parsed, not string-matched. The arguments are JSON and a `content` holding the
            # word `"path":` would otherwise be read as the path. Malformed arguments are a
            # label problem only, so they degrade to the bare tool name rather than raising
            # inside compaction — which runs on the one path a long run depends on.
            argument = ""
            try:
                decoded = json.loads(str(fn.get("arguments") or "{}"))
            except json.JSONDecodeError:
                decoded = {}
            if isinstance(decoded, dict):
                argument = str(decoded.get("path") or decoded.get("pattern") or "")
            names[call.get("id") or ""] = f"{fn.get('name') or 'tool'} {argument}".strip()
    return names


def _summarise(label: str, content: str) -> str:
    """One line, in D7's shape: `read_file src/x.py -> 240 lines`."""
    lines = content.count("\n") + 1
    return f"[compacted] {label} -> {lines} lines, {len(content)} chars, dropped from context"


def compact(messages: list[dict], *, keep_last_turns: int = KEEP_LAST_TURNS) -> Compaction:
    """Shorten old tool results. Returns a NEW list; the input is not mutated.

    Not mutating matters more than it looks: the session hands over its own history, and a
    compaction that half-applied before hitting a bad message would leave a conversation that
    is neither the old one nor the new one.
    """
    boundary = _keep_boundary(messages, keep_last_turns)
    labels = _call_names(messages)

    out: list[dict] = []
    result = Compaction(messages=out)
    for index, message in enumerate(messages):
        result.chars_before += len(_text_of(message))
        protected = index >= boundary
        content = str(message.get("content") or "")
        if message.get("role") == "tool" and len(content) > MIN_SUMMARISE_CHARS:
            if protected:
                result.protected += 1
            else:
                label = labels.get(str(message.get("tool_call_id") or ""), "tool result")
                message = {**message, "content": _summarise(label, content)}
                result.summarised += 1
        out.append(message)
        result.chars_after += len(_text_of(message))
    return result
