"""A dry run says what is held, not just what is free (GRPH-833).

`--max-workers 3` yielding one running child is a symptom the operator can see; why, was
readable nowhere. Mid-wave, with the repository open and the touchpoints in hand, the operator
of the reported wave concluded an item was being wrongly held and inferred directory-level
clustering from the symptom — a later spawn into a genuinely disjoint cluster disproved it.
The absence of this surface did not slow the diagnosis down, it produced a confident wrong one.

The plan already listed free clusters and held ones. Two things were missing and both are
here: WHY a held cluster is held (which area, whose reservation, under which rule), and the
reservation table itself — which is the one that matters, because a cluster leaves the
partition the moment its item is claimed, taking its still-blocking reservation off every read.
"""
from __future__ import annotations

from gbfleet import until


class Server:
    """Answers `collision_clusters`, and records what it was asked for."""

    allowed = frozenset({"collision_clusters"})

    def __init__(self, clusters, holds=None):
        self.payload = {"clusters": clusters, "total": len(clusters)}
        if holds is not None:
            self.payload["holds"] = holds
        self.asked: list[dict] = []

    def call(self, tool, **kw):
        self.asked.append(kw)
        return self.payload


HELD = {
    "items": ["SA-418"], "areas": ["platform-models/route.ts"],
    "held_by": ["SA-A39"], "free_in": 412,
    "held_because": [{"area": "platform-models/route.ts",
                      "reserved": "platform-models/list.ts",
                      "by": "SA-A39", "rule": "directory"}],
}
FREE = {"items": ["SA-420"], "areas": ["src/other.ts"]}
HOLDS = [{"area": "platform-models/list.ts", "agent_id": "SA-A39", "holder_state": "working",
          "item": "SA-412", "predicted": False, "free_in": 412, "blocking": True}]


def test_the_plan_asks_for_the_holds():
    """A dry run is the read an operator does INSTEAD of spending a wave, so it should not be
    the one read that leaves the question unanswered."""
    server = Server([FREE, HELD], holds=HOLDS)

    until.plan(server, prd=None, max_workers=3)

    assert server.asked and server.asked[0].get("holds") is True


def test_a_held_cluster_carries_the_rule_that_held_it():
    """THE ONE THAT MATTERS. "Held by SA-A39" sends the reader looking for SA-A39; the rule
    name is what lets them say whether the collision is real work or an artefact of the
    directory heuristic."""
    got = until.plan(Server([FREE, HELD], holds=HOLDS), prd=None, max_workers=3)

    assert got["held"][0]["because"][0]["rule"] == "directory"
    assert got["held"][0]["because"][0]["by"] == "SA-A39"


def test_the_reservation_table_is_carried_through():
    """Keyed on the RESERVATION rather than the cluster, so a hold whose item has been claimed
    — and whose cluster is therefore not in the partition at all — is still visible."""
    got = until.plan(Server([FREE, HELD], holds=HOLDS), prd=None, max_workers=3)

    assert got["holds"] == HOLDS


def test_an_older_server_that_does_not_answer_holds_still_plans():
    """`holds` is a fleet-tier argument an older server drops silently. A dry run that broke on
    the missing key would take the one read that costs nothing away from exactly the operator
    who has not upgraded yet."""
    got = until.plan(Server([FREE, HELD]), prd=None, max_workers=3)

    assert got["holds"] == []
    assert got["would_delegate"] == ["SA-420"]


def test_a_free_cluster_carries_no_reason():
    """The control: `because` on everything would make every plan read as contention."""
    got = until.plan(Server([FREE], holds=[]), prd=None, max_workers=3)

    assert got["free"] and got["held"] == []


def test_the_contention_line_still_names_the_merge_rule():
    """`_waiting` is what a running wave prints once per state change. It reports the rule that
    made the items ONE CLUSTER (GRPH-810); the plan reports the rule that made the cluster
    unavailable. Different questions, and both are now answerable."""
    said = until._waiting([HELD])

    assert "SA-A39" in said and "412s" in said
    assert "Not spawning into work that cannot be claimed" in said
