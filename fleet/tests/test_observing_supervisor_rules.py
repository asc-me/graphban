"""GRPH-1778 (P91 S6): an observing supervisor rule applies the pre-rule behaviour and posts
the would-have outcome to the observations route.

Sabotage: drop the `_observe(...)` call in `supervisor._post_would_have` and both posting tests
fail (nothing reaches the fake server).
"""
from gbfleet import observe, supervisor as sup
from gbfleet import touchpoints as tp_mod
from gbfleet.supervisor import Wave, _note_touchpoints

DECLARED = ["backend/app/services"]
GEN = "client/generated/api.ts"


class _Child:
    def __init__(self, branch="gb/w-1", held=("GRPH-1",)):
        self.branch, self.held_items = branch, list(held)
        self.worktree = None
        self.repo = self.base = None
        self.adapter = "x"

    def stdout_text(self):
        return ""


class FakeServer:
    """`get_item_details`, `get_rule_toggles`, and the observations route."""

    def __init__(self, toggles):
        self.toggles, self.posts = toggles, []

    def call(self, tool, **kw):
        if tool == "get_item_details":
            return {"id": kw["id"], "touchpoints": list(DECLARED)}
        return {"toggles": self.toggles}

    def post_observation(self, **payload):
        self.posts.append(payload)
        return {"recorded": True}


def _gen(state):
    return {"rule_id": sup.GENERATED_RULE, "state": state,
            "params": {"value": {"paths": ["client/generated/"]}}}


def _und(state, mode="hold"):
    return {"rule_id": sup.UNDECLARED_RULE, "state": state, "params": {"value": {"mode": mode}}}


def _reap(server, changed):
    wave = Wave()
    wave.touched["gb/w-1"] = ["backend/app/services/x.py", *changed]
    _note_touchpoints(wave, _Child(), server)
    return wave


def test_observing_generated_files_does_not_let_the_file_through_and_posts(monkeypatch):
    monkeypatch.setattr(observe, "emit", lambda *a, **k: None)
    server = FakeServer([_gen("observing")])
    wave = _reap(server, [GEN])
    assert wave.undeclared == {"gb/w-1": [GEN]}  # still held: the rule is not applied
    assert [(p["kind"], p["rule_id"], p["outcome"], p["item_id"]) for p in server.posts] == [
        ("rule_would_have", sup.GENERATED_RULE, "would_allow", "GRPH-1")]
    assert GEN in server.posts[0]["reason"]


def test_active_generated_files_lets_the_file_through_and_posts_nothing(monkeypatch):
    monkeypatch.setattr(observe, "emit", lambda *a, **k: None)
    server = FakeServer([_gen("active")])
    wave = _reap(server, [GEN])
    assert wave.undeclared == {} and server.posts == []


def _gate(server, monkeypatch):
    monkeypatch.setattr(tp_mod, "measure", lambda tree: ["backend/app/services/x.py", "z/other.py"])
    monkeypatch.setattr(tp_mod, "including_stream", lambda adapter, m, out: m)
    monkeypatch.setattr(observe, "emit", lambda *a, **k: None)
    wave = Wave()
    held = sup._held_for_undeclared(wave, _Child(), None, ["GRPH-1"], server)
    return held, wave


def test_observing_undeclared_hold_does_not_hold_and_posts_would_hold(monkeypatch):
    server = FakeServer([_und("observing")])
    held, _ = _gate(server, monkeypatch)
    assert held is False
    assert [(p["kind"], p["rule_id"], p["outcome"]) for p in server.posts] == [
        ("rule_would_have", sup.UNDECLARED_RULE, "would_hold")]
    assert "z/other.py" in server.posts[0]["reason"]


def test_active_undeclared_hold_still_holds(monkeypatch):
    server = FakeServer([_und("active")])
    held, _ = _gate(server, monkeypatch)
    assert held is True
    assert [p["kind"] for p in server.posts] == ["undeclared_held"]


def test_observing_warn_mode_would_not_have_held_so_posts_nothing(monkeypatch):
    server = FakeServer([_und("observing", mode="warn")])
    held, _ = _gate(server, monkeypatch)
    assert held is False and server.posts == []
