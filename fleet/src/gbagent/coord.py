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

from .orient import ORIENTATION_TOOLS

#: Every Graphban tool `gbagent` may call, pinned by `test_gbagent_loop.py`.
#:
#: Two writes and eight reads, and the split is the point.
#:
#: The WRITES are what the loop itself initiates: the give-up path writes a note and releases
#: (D6). The READS are S6's orientation layer, which the MODEL calls — every one of them
#: answers "what is this code and what touches it" and none of them changes server state, so
#: handing them to a weak model needs no further argument.
#:
#: Still absent, deliberately: `claim_next`, `sign_off`, `bounce`, `mint_enrolment`. The
#: server refuses those to this credential anyway (`TOOL_ROLES` and `independent()`), and
#: this set exists so that widening is an edit somebody has to explain rather than a call
#: site added while doing something else.
WORKER_TOOLS: frozenset[str] = frozenset(
    {"update_item", "release_item", *ORIENTATION_TOOLS}
)


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

        **It sends only its own note.** This first read the item and re-sent the existing
        evidence with it, because `update_item` assigned the incoming list straight over the
        stored one — so a stuck agent handing off to a second one that also got stuck would
        have erased the first note, which is the chain D6 is built for. GRPH-494 fixed that at
        the source: `append_evidence` is now the one appender and the record only grows, with
        an identical receipt treated as a retry rather than a second one. Carrying the read
        forward would now be a round trip on the give-up path to re-send rows the server
        already keeps, and a second failure mode on the one call that must not fail.

        Raises `HandoffFailed` rather than degrading: a release that follows a failed write is
        the outcome D6 exists to prevent.
        """
        try:
            return self.client.call(
                "update_item",
                id=self.item_id,
                evidence=[{"kind": "note", "detail": note}],
            )
        except Exception as exc:  # noqa: BLE001 — every failure here has the same consequence
            raise HandoffFailed(f"could not write the handoff note to {self.item_id}: {exc}") from exc

    def release(self) -> dict:
        """Hand the item back. Only ever called AFTER `write_handoff` has returned."""
        arguments = {"id": self.item_id}
        if self.agent_id:
            arguments["agent_id"] = self.agent_id
        return self.client.call("release_item", **arguments)
