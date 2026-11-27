"""`until --prd` exits `stuck`, not `idle`, while its PRD has unfinished items (GRPH-1962).

Found draining GRPH-P92: two backlog items kept stale blocker text after what they waited
on merged, and the drain printed `reason: idle` every cycle for hours. `idle` now means
every build item is done or parked on a person; anything else is `stuck`, named with
why, read from the server rows.
"""
from __future__ import annotations

import json

from gbfleet import drain_outcome as outcome_mod
from gbfleet import service as service_mod
from gbfleet import until as until_mod
from gbfleet.until import Wave

ACCEPT = "Fix it.\n\n## Acceptance\n- [ ] works"


class FakeServer:
    """search_items + get_item_details, and the server's notice rule (identical -> unchanged)."""

    def __init__(self, items: list[dict], details: dict | None = None):
        self.items = items
        self.details = details or {}
        self.last = None
        self.notices: list[tuple] = []

    def call(self, tool, /, **kw):
        if tool == "search_items":
            return {"results": list(self.items)}
        if tool == "get_item_details":
            return self.details.get(kw.get("id"), {})
        return {}

    def post_drain_notice(self, **p):
        key = (p["outcome"], tuple((o["item"], o["reason"]) for o in p["open_items"]))
        if p["outcome"] == "working":
            changed, self.last = self.last is not None, None
        else:
            changed, self.last = key != self.last, key
        if changed and p["outcome"] != "working":
            self.notices.append(key)
        return {"changed": changed}


def _item(i, status="done", **kw):
    return {"id": i, "status": status, "tags": [], "description": ACCEPT,
            "touchpoints": ["a.py"], **kw}


def _p92() -> list[dict]:
    """GRPH-P92 as the box saw it: three done, two backlog with stale blocker text."""
    return [
        _item("GRPH-1949"), _item("GRPH-1951"), _item("GRPH-1952"),
        _item("GRPH-1950", "backlog",
              blocker="needs GRPH-1949 (D1 parsers) and GRPH-1951 (D3 command)"),
        _item("GRPH-1953", "backlog",
              blocker="Needs the `gban rules import` command and findings pipeline"),
    ]


def _exit(server, reason="idle", wave=None, waits=None):
    return until_mod._finish(wave or Wave(), reason, 0, 0, server, review=[],
                             waits=list(waits or []), outside=[], prd="GRPH-P92")


def test_the_p92_shape_exits_stuck_naming_each_item_and_its_blocker():
    report = _exit(FakeServer(_p92()))
    assert report.reason == "stuck" and report.exit == 1 and report.ok is False
    by_item = {row["item"]: row["reason"] for row in report.stuck}
    assert set(by_item) == {"GRPH-1950", "GRPH-1953"}
    assert by_item["GRPH-1950"] == (
        "not ready: backlog: needs GRPH-1949 (D1 parsers) and GRPH-1951 (D3 command)")
    assert "findings pipeline" in by_item["GRPH-1953"]
    body = report.as_json()
    assert body["reason"] == "stuck" and body["stuck"] == report.stuck


def test_idle_with_waits_is_stuck_too():
    assert _exit(FakeServer(_p92()), reason="idle-with-waits").reason == "stuck"


def test_a_prd_whose_items_are_all_done_still_exits_idle():
    report = _exit(FakeServer([_item("A"), _item("B")]))
    assert report.reason == "idle" and report.exit == 0 and report.stuck is None
    assert "stuck" not in report.as_json()


def test_a_typed_wait_on_a_person_is_parked_not_stuck():
    s = FakeServer([_item("A"), _item("B", "blocked", tags=["wait:decision"],
                                      blocker="which schema")])
    assert _exit(s).reason == "idle"


def test_a_merge_wait_is_not_a_person_and_is_stuck():
    s = FakeServer([_item("A"), _item("B", "blocked", tags=["wait:merge"])])
    assert _exit(s).reason == "stuck"


def test_each_reason_is_read_from_the_server_row():
    items = [
        _item("DEP", "in_progress", claimed_by="GRPH-A1"),
        _item("WAITS", "next"),
        _item("NOACC", "next", description="prose only", blocker=""),
        _item("NOTP", "backlog", touchpoints=[]),
        _item("READY", "next"),
    ]
    details = {
        "WAITS": {"links": [{"type": "dependency", "a": "WAITS", "b": "DEP"}]},
        "DEP": {"status": "in_progress"},
    }
    report = _exit(FakeServer(items, details))
    by_item = {row["item"]: row["reason"] for row in report.stuck}
    assert by_item == {
        "DEP": "held elsewhere: in_progress, held by GRPH-A1",
        "WAITS": "held on dependency: DEP (in_progress)",
        "NOACC": "not ready: next: missing acceptance",
        "NOTP": "not ready: backlog: missing touchpoints",
        "READY": "ready, not claimed this cycle",
    }


def test_a_row_that_does_not_carry_the_fields_is_not_missing_them():
    s = FakeServer([{"id": "A", "status": "backlog", "tags": []}])
    assert _exit(s).stuck == [{"item": "A", "reason": "not ready: status backlog"}]


def test_a_failed_prd_read_keeps_the_reason_it_had():
    from gbfleet.client import ToolFailed

    class Down(FakeServer):
        def call(self, tool, /, **kw):
            if tool == "search_items":
                raise ToolFailed("search_items", "error", "down")
            return {}

        def post_drain_notice(self, **p):
            raise AssertionError("no notice without a read")

    assert _exit(Down([])).reason == "idle"


def test_a_line_drain_is_not_judged_by_a_prd():
    s = FakeServer(_p92())
    report = until_mod._finish(Wave(), "idle", 0, 0, s, review=[], waits=[], outside=[],
                               line="L1")
    assert report.reason == "idle"


def test_stuck_notifies_once_per_distinct_set_not_every_cycle():
    s = FakeServer(_p92())
    for _ in range(3):
        _exit(s)
    assert len(s.notices) == 1
    assert s.notices[0][0] == "stalled"
    assert [i for i, _ in s.notices[0][1]] == ["GRPH-1950", "GRPH-1953"]
    s.items = [r if r["id"] != "GRPH-1950" else _item("GRPH-1950") for r in s.items]
    _exit(s)
    _exit(s)
    assert len(s.notices) == 2 and [i for i, _ in s.notices[1][1]] == ["GRPH-1953"]


def test_classify_does_not_call_a_stuck_exit_with_a_ready_item_working():
    items = [outcome_mod.OpenItem(id="A", status="next", ready=True)]
    assert outcome_mod.classify("idle", items).state == outcome_mod.WORKING
    found = outcome_mod.classify("stuck", items)
    assert found.state == outcome_mod.STALLED
    assert found.reasons == {"A": "ready, not claimed this cycle"}


def test_service_status_shows_stuck_distinctly_from_idle(monkeypatch, tmp_path):
    monkeypatch.setattr(service_mod, "CONFIG_DIR", tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    summary = {"ok": False, "reason": "stuck", "exit": 1,
               "stuck": [{"item": "GRPH-1950", "reason": "not ready: backlog: needs X"}]}
    (logs / "drain-p92.log").write_text(json.dumps(summary) + "\n", encoding="utf-8")
    reason, _, phrase = service_mod.last_run("drain-p92")
    assert reason == "stuck" and phrase == "GRPH-1950 not ready: backlog: needs X"
    st = service_mod.Status(name="drain-p92", kind="launchd", installed=True, running=False,
                            last_exit=1, run_reason=reason, stall=phrase)
    assert st.idle is False
    assert st.line().startswith("drain-p92: stuck: GRPH-1950 not ready: backlog: needs X")
    # Even a clean exit carrying `stuck` is not idle between runs.
    assert service_mod.Status(name="d", kind="launchd", installed=True, running=False,
                              last_exit=0, run_reason="stuck").idle is False
