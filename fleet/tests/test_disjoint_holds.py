"""A member in review, or merged but unsigned, holds its own files — not its cluster (GRPH-951).

SA-576's review claim, held by a reviewer that had exited, idled a wave of screens whose files
it never named: the loop skipped the whole cluster because one member was occupied. The server
now splits review holds per file; the merged-but-unsigned case is one only the loop can see
(it knows the base), so the loop judges it by the same rule, mirrored in `touchpoints.collide`.
"""
from __future__ import annotations

from pathlib import Path

from gbfleet import touchpoints as tp
from gbfleet import until

BRIEF = {"lane": {"value": "frontend"}, "tier": {"value": "cheap"}, "blocked_by": []}


class _Planner:
    def __init__(self, clusters, details):
        self.clusters = clusters
        self.details = details
        self.delegated: list[str] = []

    def call(self, tool, **kw):
        if tool == "collision_clusters":
            return {"clusters": self.clusters}
        if tool == "get_item_details":
            return dict(self.details[kw["id"]])
        if tool == "related_work":
            return {"results": []}
        if tool == "delegate":
            self.delegated.append(kw["id"])
            return {"enrolment_code": "WORKER-X"}
        raise AssertionError(tool)


def _item(key, status, touchpoints, branch="", pr=""):
    return {"id": key, "status": status, "touchpoints": touchpoints, "branch": branch,
            "brief": BRIEF, "evidence": [], "pr": pr}


def test_two_named_files_in_one_directory_do_not_collide():
    """The rule itself: named files are exact claims; a directory or glob is a vague one."""
    assert tp.collide(["web/src/NeedsYou.tsx"], ["web/src/CueQueue.tsx"]) == []
    assert tp.collide(["web/src/NeedsYou.tsx"], ["web/src/NeedsYou.tsx"])
    assert tp.collide(["web/src/NeedsYou.tsx"], ["web/src/views"])
    assert tp.collide(["web/src/NeedsYou.tsx"], ["web/src/*.tsx"])
    assert tp.collide(["web/src/a/x.tsx"], ["web/src"])
    # Declared nothing is not disjoint.
    assert tp.collide([], ["web/src/CueQueue.tsx"])
    assert tp.collide(["web/src/CueQueue.tsx"], [])


def test_a_merged_member_holds_only_its_own_files(tmp_path: Path, monkeypatch):
    """THE CALL. MERGED is in base and unsigned; SIB shares its directory but not a file. SIB
    is the seed.

    Sabotage: hold the whole cluster whenever a member is occupied (the GRPH-886 behaviour).
    This fails: seed is None.
    """
    from gbfleet import worktree as wt

    monkeypatch.setattr(wt, "reaches", lambda repo, base, ref: ref == "gb/merged")
    planner = _Planner(
        [{"items": ["MERGED", "SIB"]}],
        {"MERGED": _item("MERGED", "next", ["web/src/CueQueue.tsx", "web/src/styles/queue.css"],
                         branch="gb/merged", pr="https://github.com/o/r/pull/1"),
         "SIB": _item("SIB", "next", ["web/src/NeedsYou.tsx", "web/src/styles/needs.css"],
                      branch="gb/sib")},
    )
    seed, _code, _want = until._delegate_next(
        planner, "GRPH-A1", "w", set(), None, repo=tmp_path, base="origin/main",
    )
    assert seed == "SIB"
    assert planner.delegated == ["SIB"]


def test_a_sibling_that_shares_a_file_stays_held(tmp_path: Path, monkeypatch):
    """The other half: an overlapping sibling is still held, and so is the occupier itself."""
    from gbfleet import worktree as wt

    monkeypatch.setattr(wt, "reaches", lambda repo, base, ref: ref == "gb/merged")
    planner = _Planner(
        [{"items": ["MERGED", "SIB"]}],
        {"MERGED": _item("MERGED", "next", ["web/src/CueQueue.tsx"], branch="gb/merged", pr="https://github.com/o/r/pull/1"),
         "SIB": _item("SIB", "next", ["web/src/CueQueue.tsx", "web/src/Other.tsx"],
                      branch="gb/sib")},
    )
    seed, _code, _want = until._delegate_next(
        planner, "GRPH-A1", "w", set(), None, repo=tmp_path, base="origin/main",
    )
    assert seed is None
    assert planner.delegated == []


def test_a_review_member_holds_only_its_own_files(tmp_path: Path):
    """Same for review: a disjoint sibling in the same cluster is buildable."""
    planner = _Planner(
        [{"items": ["REV", "SIB"]}],
        {"REV": _item("REV", "review", ["web/src/CueQueue.tsx"]),
         "SIB": _item("SIB", "next", ["web/src/NeedsYou.tsx"])},
    )
    seed, _code, _want = until._delegate_next(
        planner, "GRPH-A1", "w", set(), None, repo=tmp_path, base="",
    )
    assert seed == "SIB"


class _Server:
    def __init__(self, clusters):
        self.payload = {"clusters": clusters}

    def call(self, tool, **kw):
        return self.payload


def test_the_dry_run_names_the_held_files_and_the_joining_rule():
    """"SA-576 holds CueQueue.tsx, styles/queue.css" — the sentence the operator needed."""
    held = {
        "items": ["SA-576"], "held_by": ["review"], "free_in": None,
        "held_because": [{"area": "", "reserved": "review", "by": "SA-576",
                          "holds": ["CueQueue.tsx", "styles/queue.css"], "held": [],
                          "rule": "SA-576 is in review and holds CueQueue.tsx, styles/queue.css"}],
    }
    free = {"items": ["SA-577", "SA-578"], "areas": ["web/views", "web/views/a.tsx"],
            "because": [{"items": ["SA-577", "SA-578"],
                         "on": [{"a": "web/views/a.tsx", "b": "web/views", "rule": "directory"}]}]}
    got = until.plan(_Server([free, held]), prd=None, max_workers=3)

    assert got["held"][0]["holds"] == ["SA-576 holds CueQueue.tsx, styles/queue.css"]
    assert got["free"][0]["joined"] == ["SA-577 + SA-578: directory (web/views/a.tsx ~ web/views)"]
    assert "SA-576 holds CueQueue.tsx, styles/queue.css" in until._waiting([held])
