"""GRPH-215 phase 1: parse cursor-agent stream-json for files the run wrote.

Reads are not writes. Malformed lines are skipped. Empty is not a write.
The CALL is `record.from_cursor_stream` and the reap's `including_stream`.
"""
from __future__ import annotations

from pathlib import Path

from gbfleet.adapters.cursor import CursorAgent
from gbfleet.adapters.cursor_stream import touched
from gbfleet.record import from_cursor_stream
from gbfleet.touchpoints import including_stream
from gbfleet.worktree import create

# Documented sequence from cursor.com/docs/cli/reference/output-format (2026-09).
_DOCS = """
{"type":"system","subtype":"init","cwd":"/Users/user/project","session_id":"c6b62c6f-7ead-4fd6-9922-e952131177ff","model":"Claude 4 Sonnet","permissionMode":"default"}
{"type":"user","message":{"role":"user","content":[{"type":"text","text":"Read README.md and create a summary"}]},"session_id":"c6b62c6f-7ead-4fd6-9922-e952131177ff"}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"I'll read the README.md file"}]},"session_id":"c6b62c6f-7ead-4fd6-9922-e952131177ff"}
{"type":"tool_call","subtype":"started","call_id":"a","tool_call":{"readToolCall":{"args":{"path":"README.md"}}},"session_id":"s"}
{"type":"tool_call","subtype":"completed","call_id":"a","tool_call":{"readToolCall":{"args":{"path":"README.md"},"result":{"success":{"content":"# Project","totalLines":1}}}},"session_id":"s"}
{"type":"tool_call","subtype":"started","call_id":"b","tool_call":{"writeToolCall":{"args":{"path":"summary.txt","fileText":"# README Summary"}}},"session_id":"s"}
{"type":"tool_call","subtype":"completed","call_id":"b","tool_call":{"writeToolCall":{"args":{"path":"summary.txt","fileText":"# README Summary"},"result":{"success":{"path":"/Users/user/project/summary.txt","linesCreated":1}}}},"session_id":"s"}
{"type":"result","subtype":"success","is_error":false,"result":"done","session_id":"s"}
"""


def test_docs_example_captures_the_write_not_the_read():
    assert touched(_DOCS) == ["summary.txt"]


def test_reads_only_is_empty_not_a_clean_touch_list():
    """A run that only read files did not write. [] here is 'no writes named'."""
    blob = (
        '{"type":"tool_call","subtype":"started","tool_call":'
        '{"readToolCall":{"args":{"path":"README.md"}}}}\n'
    )
    assert touched(blob) == []


def test_malformed_lines_are_skipped():
    mixed = "not json\n" + _DOCS + "\n{nope\n"
    assert touched(mixed) == ["summary.txt"]


def test_seat_file_is_never_a_touchpoint():
    blob = (
        '{"type":"tool_call","subtype":"started","tool_call":'
        '{"writeToolCall":{"args":{"path":".cursor/mcp.json"}}}}\n'
    )
    assert touched(blob) == []


def test_absolute_path_outside_cwd_is_not_a_repo_touchpoint():
    blob = (
        '{"type":"system","subtype":"init","cwd":"/Users/user/project"}\n'
        '{"type":"tool_call","subtype":"started","tool_call":'
        '{"writeToolCall":{"args":{"path":"/etc/passwd"}}}}\n'
    )
    assert touched(blob) == []


def test_edit_is_a_write():
    blob = (
        '{"type":"tool_call","subtype":"started","tool_call":'
        '{"editToolCall":{"args":{"path":"src/app.py"}}}}\n'
    )
    assert touched(blob) == ["src/app.py"]


def test_including_stream_unions_git_and_cursor():
    assert including_stream("cursor-agent", ["git_only.py"], _DOCS) == [
        "git_only.py", "summary.txt",
    ]


def test_including_stream_is_a_no_op_for_vendors_without_a_parser():
    assert including_stream("claude", ["a.py"], _DOCS) == ["a.py"]


def test_from_cursor_stream_is_the_call():
    """Sabotage the CALL: a correct parser that nothing writes back looks healthy."""
    calls: list[tuple] = []

    class Fake:
        def call(self, tool, **kw):
            calls.append((tool, kw))
            return {"ok": True}

    assert from_cursor_stream(Fake(), "GRPH-1", _DOCS) == {"ok": True}
    assert calls == [("update_item", {"id": "GRPH-1", "touchpoints": ["summary.txt"]})]


def test_empty_stream_is_not_a_write():
    calls: list = []

    class Fake:
        def call(self, tool, **kw):
            calls.append(kw)
            return {}

    assert from_cursor_stream(Fake(), "GRPH-1", "") is None
    assert calls == []


def test_launch_asks_for_stream_json_before_the_positional_prompt(tmp_path, git_repo):
    from gbfleet.adapters.claude import POINTER
    from gbfleet.seat import Seat

    tree = create(git_repo, tmp_path / "w", "wave", "1")
    seat = Seat(code="c", server_url="https://x.invalid", api_key="k")
    launch = CursorAgent().launch(seat, tree, tmp_path / "instr", Path("cursor-agent"))
    argv = launch.argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    # A flag after POINTER is prompt text (same reason debug flags are placed here).
    assert argv.index("--output-format") < argv.index(POINTER), argv
