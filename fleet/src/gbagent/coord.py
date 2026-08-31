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
from gbfleet.record import cleaned_paths, measured as record_measured

from .orient import COORDINATION_TOOLS, ORIENTATION_TOOLS

#: Every Graphban tool `gbagent` may call, pinned by `test_gbagent_loop.py`.
#:
#: Two writes and eight reads, and the split is the point.
#:
#: The WRITES are what the loop itself initiates: the give-up path writes a note and releases
#: (D6). The READS are S6's orientation layer, which the MODEL calls — every one of them
#: answers "what is this code and what touches it" and none of them changes server state, so
#: handing them to a weak model needs no further argument.
#:
#: S7 adds `COORDINATION_TOOLS` — claim, move to review, heartbeat — because an agent that
#: cannot claim its own work is not a fleet member.
#:
#: `heartbeat` is the one the LOOP makes on a timer rather than at a decision point
#: (GRPH-496). Presence is derived from `last_seen_at` and only `heartbeat` refreshes it, so
#: without it the agent read `offline` to the whole fleet 150 seconds after registering, while
#: working, and its item lease was not being extended either. It is in `COORDINATION_TOOLS` as
#: well, because the model may reasonably call it too.
#:
#: Still absent, deliberately: `sign_off`, `bounce`, `claim_review`, `mint_enrolment`,
#: `assign_role`. The server refuses those to this credential anyway (`TOOL_ROLES` and
#: `independent()` on authorship), and this set exists so that widening is an edit somebody
#: has to explain rather than a call site added while doing something else. D5: done is not
#: the agent's word, and the server clamps a worker at `review` regardless.
#: `register_agent` is how the child gets onto the roster at all (GRPH-503). It grants
#: nothing: the SEAT decides the role, and the server refuses whatever the credential may not
#: do regardless of what this set says.
WORKER_TOOLS: frozenset[str] = frozenset(
    {"register_agent", "update_item", "release_item", "heartbeat",
     *ORIENTATION_TOOLS, *COORDINATION_TOOLS}
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

    def adopt(self, item_id: str | None) -> None:
        """Learn the item the MODEL claimed, when the harness was not given one.

        Only ever fills a blank: a coordinator constructed with `--item` is working that item,
        and a claim the model made anyway must not silently redirect the handoff.
        """
        if item_id and not self.item_id:
            self.item_id = item_id

    def write_handoff(self, note: str, touchpoints: list[str] | None = None) -> dict:
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

        **Measured paths ride along when this run changed files** (P30 D10). The server
        unions, so this sends this run's paths only. Empty is omitted — `[]` would
        otherwise read as "no collision".

        Raises `HandoffFailed` rather than degrading: a release that follows a failed write is
        the outcome D6 exists to prevent.
        """
        if not self.item_id:
            raise HandoffFailed(
                "there is no item to write a handoff to. Nothing was claimed, so there is no "
                "row to record this run against."
            )
        arguments: dict = {
            "id": self.item_id,
            "evidence": [{"kind": "note", "detail": note}],
        }
        cleaned = cleaned_paths(touchpoints)
        if cleaned:
            arguments["touchpoints"] = cleaned
        try:
            return self.client.call("update_item", **arguments)
        except Exception as exc:  # noqa: BLE001 — every failure here has the same consequence
            raise HandoffFailed(f"could not write the handoff note to {self.item_id}: {exc}") from exc

    def record_measured(self, paths: list[str]) -> dict | None:
        """Union this run's measured paths onto the item. Empty is not a write (P30 D10)."""
        return record_measured(self.client, self.item_id, paths)

    def release(self) -> dict:
        """Hand the item back. Only ever called AFTER `write_handoff` has returned."""
        arguments = {"id": self.item_id}
        if self.agent_id:
            arguments["agent_id"] = self.agent_id
        return self.client.call("release_item", **arguments)

    def cadence(self) -> dict:
        """Ask the server how often to heartbeat, rather than hardcoding it (GRPH-496).

        `heartbeat` with no `id` is presence-only and answers with
        `heartbeat_interval_seconds` and `presence_ttl_seconds`. That is the whole reason
        this is a separate call: the server's own roster docstring says the intervals travel
        with the answer because "an agent that does not know the heartbeat cadence cannot
        stay alive, and making it read a constant out of documentation is how a fleet ends up
        with members that disagree about what alive means".

        `fleet_status` carries the same two numbers and is NOT used, because it is a roster
        read this credential has no business making — it would widen WORKER_TOOLS to answer a
        question the call we already have to make already answers.

        Called once at start-up, when `idle` is what this agent honestly is.
        """
        return self.client.call("heartbeat", **({"agent_id": self.agent_id}
                                                if self.agent_id else {}))

    def beat(self) -> dict:
        """Keep presence AND the item lease alive, in one call.

        Passing `id` is what makes it both: the server extends the lease and stamps
        `working` in the same write, and its comment says why — "an agent that heartbeats its
        item lease but not its presence would be declared dead while visibly working, and the
        roster would then be reporting the opposite of what is happening".

        So this is not only about being seen. Without it the lease expires mid-build and
        another agent can be handed work this one is actively doing.
        """
        # Empty id is presence-only. Sending `id=""` is the same as omitting it on the
        # server (`if not args.get("id")`), but recording the id we *meant* is how a
        # test catches a run that claimed and never adopted (GRPH-605 / P30 D4).
        arguments: dict = {}
        if self.item_id:
            arguments["id"] = self.item_id
        if self.agent_id:
            arguments["agent_id"] = self.agent_id
        return self.client.call("heartbeat", **arguments)
