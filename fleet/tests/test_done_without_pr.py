"""A `done` item whose branch has no PR is proposed, or waited on by name (GRPH-1457).

`until --merge` logged `{"item": "GRPH-1440", "skipped": true, "detail": "no PR for
gb/p73-118"}` and nothing else: the item read `done` while its code was on no PR.
"""
from __future__ import annotations

from pathlib import Path

from gbfleet import observe
from gbfleet import propose as propose_mod
from gbfleet import supervisor as sup
from gbfleet.supervisor import Merger, Wave

from tests.test_merge_after_sign_off import REPO, _done_item, _Ledger
from tests.test_propose import REVIEWED


class _Client(_Ledger):
    def __init__(self, items):
        super().__init__(items)
        self.observed = []

    def post_observation(self, **payload):
        self.observed.append(payload)
        return {}


def _wire(monkeypatch, *, ahead=2, url="https://github.com/o/r/pull/7"):
    events = []
    monkeypatch.setattr(propose_mod, "find", lambda: "/usr/bin/gh")
    monkeypatch.setattr(propose_mod, "view",
                        lambda repo, sel: (None, f"no PR for {sel}: none") if "/pull/" not in sel
                        else (None, "later"))
    monkeypatch.setattr(sup.wt_mod, "commits_beyond_base", lambda *a, **k: ahead)
    monkeypatch.setattr(propose_mod, "title_subject", lambda *a, **k: "subject")
    proposals = []

    def fake_propose(repo, branch, base, **kw):
        proposals.append(branch)
        return propose_mod.Proposed(branch=branch, url=url, ok=bool(url),
                                    reason="" if url else "gh pr create failed")
    monkeypatch.setattr(propose_mod, "propose", fake_propose)
    monkeypatch.setattr(sup, "_branch_head", lambda *a: REVIEWED)
    monkeypatch.setattr(observe, "emit", lambda event, **kw: events.append((event, kw)))
    return events, proposals


def _item():
    row = _done_item("GRPH-1440", branch="gb/p73-118")
    return {"GRPH-1440": row}


def test_a_done_item_with_a_branch_and_no_pr_gets_a_pr(monkeypatch):
    events, proposals = _wire(monkeypatch)
    client = _Client(_item())
    merger = Merger(REPO, client, enabled=True, base="origin/main")

    merger.tick(Wave())

    assert proposals == ["gb/p73-118"]
    assert client.observed and client.observed[0]["url"].endswith("/pull/7")
    assert "GRPH-1440" in merger.watching, "proposed, so the next tick merges it"


def test_when_it_cannot_be_proposed_a_wait_names_the_item_never_a_silent_skip(monkeypatch):
    events, _ = _wire(monkeypatch, url="")
    client = _Client(_item())
    merger = Merger(REPO, client, enabled=True, base="origin/main")
    wave = Wave()

    merger.tick(wave)

    waits = [kw for kind, kw in events if kind == "wait"]
    assert any(w.get("item") == "GRPH-1440" and "no PR" in w["detail"] for w in waits), events
    assert not any(kind == "merge" and kw.get("skipped") for kind, kw in events), events
    assert "GRPH-1440" in merger.watching and not wave.merged["GRPH-1440"].final


def test_a_branch_with_nothing_beyond_the_base_waits_rather_than_proposing(monkeypatch):
    events, proposals = _wire(monkeypatch, ahead=0)
    merger = Merger(REPO, _Client(_item()), enabled=True, base="origin/main")

    merger.tick(Wave())

    assert proposals == []
    assert any(kind == "wait" and kw.get("item") == "GRPH-1440" for kind, kw in events)
