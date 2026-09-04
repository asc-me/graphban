"""The timer heartbeat says what the agent is doing (PRD-34 D5/D12).

Presence-only beats kept the row alive and said nothing. Now the beat reads the toolset's
last action and written files AT BEAT TIME, so the Live page shows the current step rather
than the one from when the thread started. A source that raises costs nothing but the
status — never the beat.
"""
from __future__ import annotations

from gbagent.coord import Coordinator
from gbagent.llm import ToolCall
from gbagent.toolset import Toolset, _describe


class RecordingClient:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def call(self, name, **arguments):
        self.calls.append((name, arguments))
        return {"agent_id": "GRPH-A9"}


def test_a_beat_carries_status_and_files_from_the_source():
    client = RecordingClient()
    coord = Coordinator(client=client, item_id="GRPH-1", agent_id="GRPH-A9")
    coord.status_source = lambda: ("running tests", ["a.py", "b.py"])
    coord.beat()
    name, args = client.calls[-1]
    assert name == "heartbeat"
    assert args["status"] == "running tests"
    assert args["files"] == ["a.py", "b.py"]
    assert args["id"] == "GRPH-1" and args["agent_id"] == "GRPH-A9"


def test_no_source_is_a_presence_only_beat():
    client = RecordingClient()
    coord = Coordinator(client=client, item_id="", agent_id="GRPH-A9")
    coord.beat()
    _, args = client.calls[-1]
    assert "status" not in args and "files" not in args


def test_a_source_that_raises_does_not_cost_the_beat():
    client = RecordingClient()
    coord = Coordinator(client=client, item_id="GRPH-1", agent_id="GRPH-A9")

    def boom():
        raise RuntimeError("toolset gone")
    coord.status_source = boom
    coord.beat()
    _, args = client.calls[-1]
    assert args["id"] == "GRPH-1" and "status" not in args


def test_the_source_is_read_at_beat_time(tmp_path):
    """The thread started before the toolset did anything; the status must follow the work."""
    from gbagent.config import VerifyConfig
    toolset = Toolset(root=tmp_path, cfg=VerifyConfig(argv=["true"], cwd=tmp_path, source="x"))
    client = RecordingClient()
    coord = Coordinator(client=client, item_id="GRPH-1", agent_id="GRPH-A9")
    coord.status_source = toolset.activity
    coord.beat()
    assert "status" not in client.calls[-1][1], "nothing done yet: nothing reported"
    (tmp_path / "f.py").write_text("x = 1\n")
    toolset.execute(ToolCall(id="1", name="read_file", input={"path": "f.py"}))
    coord.beat()
    assert client.calls[-1][1]["status"] == "reading f.py"


def test_describe_names_the_tool_and_its_target():
    assert _describe(ToolCall(id="1", name="run_tests", input={})) == "running tests"
    assert _describe(ToolCall(id="1", name="edit_file", input={"path": "a.py", "old": "x", "new": "y"})) == "editing a.py"
    assert len(_describe(ToolCall(id="1", name="grep", input={"pattern": "q" * 400}))) <= 200
