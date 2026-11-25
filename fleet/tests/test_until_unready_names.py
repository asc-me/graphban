"""GRPH-1831: `_unready` names `missing` from a read that carries the description.

`search_items(fields="full")` returns no `description` key, so reading acceptance from
that row named "acceptance" missing for every item left out of the claim pool.

GRPH-1839: a server whose `fleet_status` carries `line_unready` has already said which
line items are unready; `_unready` reports that list and guesses nothing.
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
    def __init__(self, rows, details, roster=None):
        self.rows, self.details, self.reads = rows, details, []
        # An older server: `fleet_status` without `line_unready`.
        self.roster = roster if roster is not None else {"agents": []}

    def call(self, tool, **kw):
        if tool == "fleet_status":
            return self.roster
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


def test_the_servers_line_unready_is_reported_exactly_and_nothing_is_guessed():
    server = [{"id": "B", "missing": ["touchpoints"]}, {"id": "Z", "missing": ["acceptance"]}]
    # A and B are left out of the pool; A's description has no acceptance list, and the
    # fallback would have named it. The server did not list A, so nothing names A.
    p = _P([_row("A"), _row("B")],
           {"A": {"id": "A", "description": "prose only"}, "B": {"id": "B"}},
           roster={"agents": [], "line_unready": server})
    got = until._unready(p, "L")
    assert got == server
    assert "A" not in {r["id"] for r in got}
    assert p.reads == []


def test_an_empty_server_list_is_the_servers_verdict_not_a_cue_to_guess():
    p = _P([_row("A")], {"A": {"id": "A", "description": "prose only"}},
           roster={"agents": [], "line_unready": []})
    assert until._unready(p, "L") == [] and p.reads == []


def test_a_review_hold_is_still_named_beside_the_servers_list():
    class _Held(_P):
        def call(self, tool, **kw):
            if tool == "collision_clusters":
                return {"clusters": [{"items": ["POOLED"], "held_because": [
                    {"by": "R-1", "held": ["H"]}]}]}
            return super().call(tool, **kw)

    p = _Held([_row("H")], {}, roster={"line_unready": [{"id": "B", "missing": ["acceptance"]}]})
    assert until._unready(p, "L") == [{"id": "B", "missing": ["acceptance"]},
                                      {"id": "H", "held_by": "R-1"}]
