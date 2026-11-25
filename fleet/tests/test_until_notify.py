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


def _as_service(monkeypatch, tmp_path, name="drain-p1"):
    """Run `notify_outcome` as the installed service named `name` would (GRPH-1916)."""
    from gbfleet import service as service_mod

    monkeypatch.setattr(service_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setenv(until_mod._SERVICE_NAME_ENV, name)
    monkeypatch.setenv(until_mod._SERVICE_EVERY_ENV, "300")
    return service_mod


def test_a_service_drain_records_the_notice_status_reads(monkeypatch, tmp_path):
    """GRPH-1916: the drained notice `until` sends is the one `service status` reads back.

    Sabotage: replace `service_mod.record_notice(...)` in `notify_outcome` with `pass`
    and `last_notice` stays None.
    """
    service_mod = _as_service(monkeypatch, tmp_path)
    s, hooks = FakeServer(), []
    s.items = [_item("A")]
    _cycle(s, hooks)
    got = service_mod.last_notice("drain-p1")
    assert got is not None and got["outcome"] == "drained" and got["prd"] == "GRPH-P1"
    assert got["sent_at"] == hooks[0]["sent_at"]


def test_a_held_stall_records_its_open_items(monkeypatch, tmp_path):
    service_mod = _as_service(monkeypatch, tmp_path)
    s, hooks = FakeServer(), []
    s.items = [_item("A", "blocked", blocker="needs a person")]
    _cycle(s, hooks)
    got = service_mod.last_notice("drain-p1")
    assert got["outcome"] == "stalled"
    assert [o["item"] for o in got["open"]] == ["A"] and got["open"][0]["reason"]


def test_working_overwrites_the_recorded_notice(monkeypatch, tmp_path):
    service_mod = _as_service(monkeypatch, tmp_path)
    s, hooks = FakeServer(), []
    s.items = [_item("A")]
    _cycle(s, hooks)
    s.items = [_item("A"), _item("B", "next")]
    _cycle(s, hooks, spawned=1)
    assert service_mod.last_notice("drain-p1")["outcome"] == "working"


def test_an_unchanged_post_writes_nothing(monkeypatch, tmp_path):
    service_mod = _as_service(monkeypatch, tmp_path)
    s, hooks = FakeServer(), []
    s.items = [_item("A")]
    _cycle(s, hooks)
    path = service_mod.notice_state_path("drain-p1")
    path.unlink()
    _cycle(s, hooks)  # identical post: server answers changed=false
    assert not path.exists()


def test_a_one_off_until_records_nothing(monkeypatch, tmp_path):
    from gbfleet import service as service_mod

    monkeypatch.setattr(service_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.delenv(until_mod._SERVICE_NAME_ENV, raising=False)
    monkeypatch.delenv(until_mod._SERVICE_EVERY_ENV, raising=False)
    s, hooks = FakeServer(), []
    s.items = [_item("A")]
    _cycle(s, hooks)
    assert len(hooks) == 1 and not (tmp_path / "notice").exists()


class DepServer(FakeServer):
    """A server whose items carry dependency links, as `get_item_details` returns them."""

    def __init__(self, details):
        super().__init__()
        self.details = details

    def call(self, tool, /, **kw):
        if tool == "get_item_details":
            return self.details.get(kw["id"], {})
        return super().call(tool, **kw)


def _dep(item, on, status):
    return {"links": [{"type": "dependency", "a": item, "b": on}]}, {on: {"status": status}}


def test_an_item_held_on_an_open_dependency_is_a_stall_naming_it():
    links, dep = _dep("B", "M1", "backlog")
    s = DepServer({"B": links, **dep})
    s.items = [_item("B", "next")]
    _cycle(s, [])
    assert s.notices and s.notices[0][0] == "stalled"
    assert "M1 (backlog)" in s.notices[0][1][0][1]


def test_an_item_whose_dependencies_are_done_stays_working():
    links, dep = _dep("B", "M1", "done")
    s = DepServer({"B": links, **dep})
    s.items = [_item("B", "next")]
    _cycle(s, [])
    assert s.notices == []


def test_a_stall_notice_carries_the_blocked_items_blocker():
    # GRPH-1945. The reason posted names what a person must do, from the row's blocker.
    s, hooks = FakeServer(), []
    s.items = [_item("A", "blocked", blocker="Needs a person: read-only check on staging")]
    _cycle(s, hooks)
    assert len(s.notices) == 1
    outcome, open_items = s.notices[0]
    assert outcome == "stalled"
    assert open_items == (("A", "not ready: blocked: Needs a person: read-only check on staging"),)
    assert "Needs a person" in hooks[0]["open"][0]["reason"]
