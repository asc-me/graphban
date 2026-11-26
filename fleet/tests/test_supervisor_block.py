"""`_block` and `_leave_done` write a blocker the server accepts (GRPH-1965).

GRPH-1960 refuses an `update_item(blocker=...)` that names no item key, person or decision,
typed wait, blocking-fix filing or gbfleet hold mark. The supervisor's fallback when a done
item's merge did not land and `bounce` is refused wrote the bare merge reason, which names
none of those: the write was refused, the item stayed `done`, and the reason only reached
`wave.failures`.
"""
from __future__ import annotations

import re

import pytest

from gbfleet import observe
from gbfleet import propose as propose_mod
from gbfleet import supervisor as sup
from gbfleet.client import NotPermitted, ToolFailed
from gbfleet.supervisor import Merger, Wave

from tests.test_merge_after_sign_off import REPO, _done_item, _Ledger

# The server's acceptance rule (backend/app/services/items.py, `refuse_unnamed_blocker`),
# minus the parts that need a database or the item's tags. Copied, not imported: the fleet
# package does not depend on the backend.
_PERSON_OR_DECISION = re.compile(r"\b(?:person|human|decision)\b", re.IGNORECASE)
_GBFLEET_HOLD = re.compile(r"\[gbfleet [a-z ]+ hold\]")

# A merge reason that names no person, decision, hold, or other item.
REASON = "rebase conflict in backend/app/services/prds.py"


def _server_accepts(blocker: str) -> bool:
    return bool(_PERSON_OR_DECISION.search(blocker) or _GBFLEET_HOLD.search(blocker))


class _Refusing(_Ledger):
    """Bounce is refused with `refusal`; a blocker the server would refuse raises."""

    def __init__(self, items, refusal):
        super().__init__(items)
        self.refusal = refusal

    def call(self, tool, **kw):
        if tool == "bounce":
            self.calls.append((tool, kw))
            raise self.refusal
        if tool == "update_item" and kw.get("blocker") is not None:
            if not _server_accepts(kw["blocker"]):
                self.calls.append(("refused", kw))
                raise ToolFailed("update_item", "conflict",
                                 "a blocker must name what it waits on")
        return super().call(tool, **kw)


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(observe, "emit", lambda event, **kw: None)


def _merger(refusal):
    items = {"GRPH-1440": _done_item("GRPH-1440", branch="gb/p73-118")}
    client = _Refusing(items, refusal)
    merger = Merger(REPO, client, enabled=True, base="origin/main")
    merger.watching["GRPH-1440"] = object()
    return merger, client


def _blocked(client):
    return [kw for kw in client.wrote("update_item") if kw.get("status") == "blocked"]


def test_the_merge_blocker_text_passes_the_servers_rule():
    text = sup.merge_blocker(REASON)
    assert not _server_accepts(REASON)
    assert _server_accepts(text)
    assert REASON in text
    assert _server_accepts(sup.merge_blocker(""))


def test_block_after_a_refused_bounce_writes_an_accepted_blocker():
    merger, client = _merger(ToolFailed("bounce", "conflict", sup.NO_MERGE_PENDING))
    wave = Wave()

    merger._bounce(wave, "GRPH-1440", propose_mod.Merged(item="GRPH-1440", reason=REASON))

    blocked = _blocked(client)
    assert len(blocked) == 1
    assert _server_accepts(blocked[0]["blocker"])
    assert REASON in blocked[0]["blocker"]
    assert "GRPH-1440" not in merger.watching
    assert all("could not block" not in f for f in wave.failures)


def test_leave_done_after_a_refused_bounce_writes_an_accepted_blocker():
    merger, client = _merger(NotPermitted("this credential may not call 'bounce'"))
    wave = Wave()

    merger._leave_done(wave, "GRPH-1440",
                       propose_mod.Merged(item="GRPH-1440", reason=REASON))

    blocked = _blocked(client)
    assert len(blocked) == 1
    assert _server_accepts(blocked[0]["blocker"])
    assert REASON in blocked[0]["blocker"]
    assert "GRPH-1440" not in merger.watching
    assert wave.failures == []
