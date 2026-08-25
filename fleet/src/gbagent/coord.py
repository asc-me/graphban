"""What the agent may say to Graphban, and the two calls the give-up path makes (S3, D6).

**A worker is not a supervisor.** `gbfleet.client.ALLOWED_TOOLS` is the supervisor's set —
two reads, because PRD-22 §4 says the supervisor may not claim work or sign off. `gbagent`
holds a *seat* credential and does claim work, so it needs a different set. It gets one by
passing its own to the same checked call, rather than by widening the supervisor's.

**The server is still the authority.** This allowlist is not a security boundary any more than
the supervisor's is: a seat's `eligible_roles` binds server-side, and `sign_off` refuses the
author whatever this file says (D5). What it buys is the same thing it buys there — widening
the agent's reach is a deliberate edit to a named set with a test in front of it.
"""
from __future__ import annotations

from dataclasses import dataclass

from gbfleet.client import Graphban

#: Every Graphban tool `gbagent` may call, pinned by `test_gbagent_loop.py`.
#:
#: This is S3's set and it is small on purpose: the give-up path (D6) reads the item, writes a
#: note and releases, and that is all the loop itself initiates. The tools the MODEL calls to
#: claim work and move an item to review arrive with the slice that wires the coordination
#: layer — and they should arrive by editing this line, which is the point of having it.
WORKER_TOOLS: frozenset[str] = frozenset({"get_item_details", "update_item", "release_item"})


class HandoffFailed(RuntimeError):
    """The handoff could not be written, so the item must NOT be released.

    Releasing without it is the one outcome D6 exists to prevent: `built_by` clears on an
    untouched row, and the next agent inherits a dirty worktree with no record of who made it
    or why they stopped.
    """


@dataclass
class Coordinator:
    """The agent's side of the item row.

    Named calls rather than `call("update_item", ...)` at the call site, for the reason
    `test_client.py` gives about the supervisor's helpers: a string is what the next widening
    gets spelled with.
    """

    client: Graphban
    item_id: str
    agent_id: str = ""

    @classmethod
    def connect(cls, base_url: str, api_key: str, item_id: str, agent_id: str = "", **kw):
        return cls(
            client=Graphban(base_url=base_url, api_key=api_key, allowed=WORKER_TOOLS, **kw),
            item_id=item_id,
            agent_id=agent_id,
        )

    def write_handoff(self, note: str) -> dict:
        """Record what happened, as a substantive write to the ITEM ROW.

        This is the call `built_by` survives on. `release_item` clears authorship when
        `updated_at <= claimed_at` (GRPH-434, `services/items.py`), and writing to the worktree
        does not touch the row — so without this a release would clear authorship and leave a
        salvage branch nobody is recorded as having made.

        **Existing evidence is read and carried, because `update_item` REPLACES it.**
        `item.evidence = normalize_evidence(fields["evidence"])` is an assignment, not an
        append. A stuck agent handing off to a second one that also gets stuck would otherwise
        erase the first one's note — which is precisely the chain D6 is built for. The read is
        not racy in any way that matters: we hold the lease.

        Raises `HandoffFailed` rather than degrading. Writing a note that destroys the previous
        one is worse than not writing it, and a release that follows a failed write is worse
        than both.
        """
        try:
            existing = self.client.call("get_item_details", id=self.item_id).get("evidence") or []
        except Exception as exc:  # noqa: BLE001 — every failure here has the same consequence
            raise HandoffFailed(
                f"could not read {self.item_id} to preserve its evidence: {exc}"
            ) from exc

        try:
            return self.client.call(
                "update_item",
                id=self.item_id,
                evidence=[*existing, {"kind": "note", "detail": note}],
            )
        except Exception as exc:  # noqa: BLE001
            raise HandoffFailed(f"could not write the handoff note to {self.item_id}: {exc}") from exc

    def release(self) -> dict:
        """Hand the item back. Only ever called AFTER `write_handoff` has returned."""
        arguments = {"id": self.item_id}
        if self.agent_id:
            arguments["agent_id"] = self.agent_id
        return self.client.call("release_item", **arguments)
