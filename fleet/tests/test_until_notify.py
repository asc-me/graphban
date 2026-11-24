"""One drain notice per change of state (GRPH-1912).

The server holds the last notice and answers `changed`; `until` keeps nothing. The fake
below is that server's rule (identical post -> unchanged, `working` clears), so each test
drives whole cycles through `notify_outcome` without a network.
"""
from __future__ import annotations

from gbfleet import until as until_mod
from gbfleet.client import ObservationRefused
from gbfleet.until import Wave, notify_outcome


class FakeServer:
    def __init__(self):
        self.items: list[dict] = []
        self.last = None
        self.notices: list[tuple] = []
        self.down = False

    def call(self, tool, /, **kw):
        if tool != "search_items":
            return {}
        return {"results": list(self.items)}

    def post_drain_notice(self, **p):
        if self.down:
            raise ObservationRefused("unreachable")
        key = (p["outcome"], tuple((o["item"], o["reason"]) for o in p["open_items"]))
        if p["outcome"] == "working":
            changed, self.last = self.last is not None, None
        else:
            changed, self.last = key != self.last, key
        if changed and p["outcome"] != "working":
            self.notices.append(key)
        return {"changed": changed}


def _item(i, status="done", **kw):
    return {"id": i, "status": status, "tags": [], **kw}


def _cycle(server, hooks, reason="idle", spawned=0):
    wave = Wave()
    wave.spawned.extend(object() for _ in range(spawned))
    return notify_outcome(server, wave, reason, "GRPH-P1", on_notice=hooks.append)


def test_two_idle_cycles_over_a_drained_prd_write_one_notice():
    s, hooks = FakeServer(), []
    s.items = [_item("A")]
    _cycle(s, hooks)
    _cycle(s, hooks)
    assert len(s.notices) == 1 and len(hooks) == 1


def test_a_stall_whose_open_set_changes_notifies_again():
    s, hooks = FakeServer(), []
    s.items = [_item("A", "blocked"), _item("B", "blocked")]
    _cycle(s, hooks)
    s.items = [_item("A", "done"), _item("B", "blocked")]
    _cycle(s, hooks)
    _cycle(s, hooks)
    assert len(s.notices) == 2 and len(hooks) == 2


def test_drained_working_drained_writes_two_notices():
    s, hooks = FakeServer(), []
    s.items = [_item("A")]
    _cycle(s, hooks)
    s.items = [_item("A"), _item("B", "next")]
    _cycle(s, hooks, spawned=1)
    s.items = [_item("A"), _item("B")]
    _cycle(s, hooks)
    assert len(s.notices) == 2 and len(hooks) == 2


def test_a_failed_post_is_retried_by_the_next_cycle():
    s, hooks = FakeServer(), []
    s.items = [_item("A")]
    s.down = True
    assert _cycle(s, hooks) is None
    s.down = False
    _cycle(s, hooks)
    _cycle(s, hooks)
    assert len(s.notices) == 1 and len(hooks) == 1


def test_the_hook_does_not_run_on_a_suppressed_duplicate(monkeypatch):
    s, hooks = FakeServer(), []
    s.items = [_item("A")]
    _cycle(s, hooks)
    hooks.clear()
    _cycle(s, hooks)
    assert hooks == []


def test_the_notice_carries_the_hook_json_fields():
    s, hooks = FakeServer(), []
    s.items = [_item("A", "blocked")]
    _cycle(s, hooks)
    assert set(hooks[0]) == {"prd", "outcome", "open", "sent_at"}
    assert hooks[0]["outcome"] == "stalled" and hooks[0]["open"][0]["item"] == "A"


def test_a_failing_hook_does_not_raise():
    s = FakeServer()
    s.items = [_item("A")]

    def boom(_):
        raise RuntimeError("x")

    assert notify_outcome(s, Wave(), "idle", "GRPH-P1", on_notice=boom) is not None


def test_an_exit_of_a_prd_drain_posts_through_finish_and_a_line_drain_does_not():
    """The wiring: `_finish` (every `_leave` of `_loop`) posts for `--prd` only (P99 D5)."""
    posted = []

    class Planner(FakeServer):
        def post_drain_notice(self, **p):
            posted.append(p)
            return super().post_drain_notice(**p)

    s = Planner()
    s.items = [_item("A")]
    hooks = []
    until_mod._finish(Wave(), "idle", 0, 0, s, review=[], waits=[], outside=[],
                      prd="GRPH-P1", on_notice=hooks.append)
    until_mod._finish(Wave(), "idle", 0, 0, s, review=[], waits=[], outside=[],
                      line="L1", on_notice=hooks.append)
    assert [p["outcome"] for p in posted] == ["drained"] and len(hooks) == 1
