"""GRPH-1831: `_unready` names `missing` from a read that carries the description.

`search_items(fields="full")` returns no `description` key, so reading acceptance from
that row named "acceptance" missing for every item left out of the claim pool.
"""
from __future__ import annotations

from gbfleet import until
from gbfleet.client import ToolFailed

ACCEPT = "Fix it.\n\n## Acceptance\n\n- a test\n"


def _row(rid, touchpoints=("a.py",)):
    # The real fields=full shape: no `description` key at all.
    return {"id": rid, "title": rid, "status": "next", "line_id": "L",
            "touchpoints": list(touchpoints), "tags": [], "evidence": [],
            "claimed_by": None, "blocker": ""}


class _P:
    def __init__(self, rows, details):
        self.rows, self.details, self.reads = rows, details, []

    def call(self, tool, **kw):
        if tool == "collision_clusters":
            return {"clusters": [{"items": ["POOLED"]}]}
        if tool == "search_items":
            return {"results": self.rows if kw.get("status") == "next" else []}
        if tool == "get_item_details":
            self.reads.append(kw["id"])
            got = self.details[kw["id"]]
            if isinstance(got, Exception):
                raise got
            return got
        raise AssertionError(tool)


def test_a_left_out_item_with_acceptance_in_its_details_is_not_missing_acceptance():
    p = _P([_row("A")], {"A": {"id": "A", "description": ACCEPT}})
    assert until._unready(p, "L") == []
    assert p.reads == ["A"]


def test_an_item_without_an_acceptance_list_is_still_missing_acceptance():
    p = _P([_row("B")], {"B": {"id": "B", "description": "prose only"}})
    assert until._unready(p, "L") == [{"id": "B", "missing": ["acceptance"]}]


def test_an_item_with_no_touchpoints_is_still_missing_touchpoints():
    p = _P([_row("C", touchpoints=())], {"C": {"id": "C", "description": ACCEPT}})
    assert until._unready(p, "L") == [{"id": "C", "missing": ["touchpoints"]}]


def test_a_failed_description_read_is_unknown_not_missing_acceptance():
    p = _P([_row("D"), _row("E")],
           {"D": ToolFailed("get_item_details", "not_found", "boom"), "E": {"id": "E"}})
    got = {r["id"]: r for r in until._unready(p, "L")}
    for rid in ("D", "E"):
        assert "acceptance" not in got[rid]["missing"]
        assert got[rid]["missing"] == ["unknown"]


def test_a_pooled_item_is_not_read():
    p = _P([_row("POOLED")], {})
    assert until._unready(p, "L") == [] and p.reads == []
