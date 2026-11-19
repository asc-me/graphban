"""`until --line L --merge` looks up only line L's done items (GRPH-1719).

Sabotage anchor: drop the `line_id` filter in `Merger._discover_candidates` and
`test_a_line_merge_looks_up_only_the_lines_items` fails.
"""
from __future__ import annotations

from pathlib import Path

from gbfleet import propose as propose_mod
from gbfleet.supervisor import Merger, Wave

from tests.test_merge_after_sign_off import _done_item, _Ledger

REPO = Path("/tmp/does-not-matter")


def _lookups(monkeypatch) -> list[str]:
    asked: list[str] = []

    def attempt(self, wave, item_id, release=""):
        asked.append(item_id)
        return None
    monkeypatch.setattr(Merger, "_attempt", attempt)
    monkeypatch.setattr(Merger, "_release", lambda self: "")
    return asked


def _row(item_id: str, **scope) -> dict:
    row = _done_item(item_id)
    row["pr"] = f"https://github.com/o/r/pull/{item_id[-1]}"
    row.update(scope)
    return row


def test_a_line_merge_looks_up_only_the_lines_items(monkeypatch):
    asked = _lookups(monkeypatch)
    ledger = _Ledger({
        "G-1": _row("G-1", line_id="L1"),
        "G-2": _row("G-2", prd_id="P1"),
        "G-3": _row("G-3"),
        "G-4": _row("G-4", line_id="L2"),
    })
    merger = Merger(REPO, ledger, enabled=True, line_id="L1")
    merger.tick(Wave())
    assert asked == ["G-1"]


def test_a_hold_dependency_outside_the_line_is_still_looked_up(monkeypatch):
    asked = _lookups(monkeypatch)
    ledger = _Ledger({"G-1": _row("G-1", line_id="L1"), "G-2": _row("G-2", prd_id="P1")})
    merger = Merger(REPO, ledger, enabled=True, line_id="L1")
    merger.note_hold([{"id": "G-2"}])
    merger.tick(Wave())
    assert sorted(asked) == ["G-1", "G-2"]


def test_a_recorded_merge_is_not_looked_up_after_a_restart(monkeypatch):
    asked = _lookups(monkeypatch)
    item = _row("G-1", line_id="L1")
    item["evidence"].append({
        "kind": "url", "detail": "merged by gbfleet: squash of x landed as abc123def456",
        "url": "https://github.com/o/r/pull/1", "commit": "abc123def456" + "0" * 28})
    ledger = _Ledger({"G-1": item})
    for _ in range(2):  # a fresh Merger each cycle, as launchd restarts the process
        Merger(REPO, ledger, enabled=True, line_id="L1").tick(Wave())
    assert asked == []


def test_a_second_cycle_makes_no_lookup_for_an_item_the_first_recorded(monkeypatch, forge=None):
    ledger = _Ledger({"G-1": _row("G-1", line_id="L1")})
    asked: list[str] = []

    def attempt(self, wave, item_id, release=""):
        asked.append(item_id)
        got = propose_mod.Merged(item=item_id, ok=True, commit="f" * 40,
                                 url="https://github.com/o/r/pull/1", selector="x")
        return self._record(wave, got)
    monkeypatch.setattr(Merger, "_attempt", attempt)
    monkeypatch.setattr(Merger, "_release", lambda self: "")
    Merger(REPO, ledger, enabled=True, line_id="L1").tick(Wave())
    Merger(REPO, ledger, enabled=True, line_id="L1").tick(Wave())
    assert asked == ["G-1"]
