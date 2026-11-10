"""GRPH-1499: Graphban.execute writes the claim manifest on a claim and clears it on release."""
import json

from gbagent.llm import ToolCall
from gbagent.orient import Orientation


class FakeClient:
    def __init__(self, payload):
        self.payload = payload

    def call(self, name, **kw):
        return self.payload


def test_claim_cluster_writes_and_release_removes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    claim = tmp_path / ".cursor" / "graphban-claim.json"
    monkeypatch.setenv("GRAPHBAN_CLAIM_FILE", str(claim))
    o = Orientation(client=FakeClient({"claimed": True, "items": [
        {"id": "X-1", "touchpoints": ["a"]}, {"id": "X-2", "touchpoints": ["b"]}]}))
    o.execute(ToolCall(id="1", name="claim_cluster", input={}))
    assert json.loads(claim.read_text())["touchpoints"] == ["a", "b"]
    o.client = FakeClient({"id": "X-1", "status": "next"})
    o.execute(ToolCall(id="2", name="release_item", input={"id": "X-1"}))
    assert not claim.exists()
