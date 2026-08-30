"""Which tools ship in the manifest by default, and which are opt-in (GRPH-571).

**The manifest had five tokens of headroom.** `MEASURED_TOKENS` was 13595 against a 13600
`CEILING`, and `test_mcp_footprint.py` argues by name against raising it: *"Raising CEILING is
a decision, not a fix. The history below is a series of raises that the docstring itself argues
should have been trims."* GRPH-146 measured what that costs — adding `limit` and `offset` to
**one** tool cost ~73 tokens against 8 of headroom, and fit only by trimming four unrelated
places. The next tool that needs a field has nowhere to put it, which makes this a prerequisite
for new MCP surface rather than an optimisation.

Two levers were already spent. The scope gate (AL-78) roughly halves a read-only key's manifest
and barely moves a write key's. Role narrowing (GRPH-337, E9) takes 15-19% off a key or session
that names one role. Neither touches the number the ratchet defends, which is the FULL manifest
an unrestricted credential pays — and under enrolment (PRD-19) an unrestricted credential is
what every agent is recommended to hold.

## Why the tier lives on the KEY

The same argument that made role gating safe without SSE. `tools/list` is fetched once at
client connect, and this endpoint has no channel to push `notifications/tools/list_changed`
(`mcp_server._visible_tools` says so, and PRD-17 D-b called it a non-goal for that reason). A
tier the caller could widen mid-session would produce a manifest the client never re-fetches —
the widened tools would be invisible until reconnect, which is indistinguishable from the tier
not working. A key's tiers are fixed at mint, so the manifest it is shipped can never go stale.

## The guard, which matters more than the saving

**A tool absent from the manifest still dispatches for an authorised key.** The manifest is a
token optimisation and never an authorisation boundary — `_visible_tools` already states this
about scope and role, and it holds here for the same reason. If tiering could refuse a call,
this would stop being a size change and become a permissions change, which is a different and
much worse thing. `_call_tool` is untouched by this module.

## And the affordance, which is the failure mode this could ship with

A tool nobody can see is a tool nobody uses. That is GRPH-580 exactly — a `gate` scope that
existed, worked, and had no way to be created, so the thing depending on it ran permanently on
its weak path while looking correct. The identical shape here would be a `prd` tier that is
never granted because no agent is told it exists. So `get_context` — which is core, and which
every agent calls first — reports the tiers this key does NOT have and how to get them. The
saving is worth nothing if it costs an agent a capability it cannot discover.
"""
from __future__ import annotations

#: Tool -> the tier that must be granted for it to appear in the manifest.
#:
#: Chosen conservatively, and the principle is worth stating because a tempting split failed
#: it: **everything on the path of doing one piece of work stays core.** The measured
#: clusters that would have saved most — putting `claim_next`/`heartbeat`/`release_item` in a
#: "loop lifecycle" tier, or the review verbs in a "fleet" one — would take the claim loop
#: away from a solo agent and review away from a plain reviewer credential. Saving 70% by
#: making the default key unable to finish work is not a saving.
#:
#: What is left is genuinely specialist: authoring PRDs, WRITING the code graph (reading it is
#: core, since GRPH-463 wants it to be the default read path), administering a fleet, and a
#: tail of tools that are rare and none of which appear in the middle of a task.
TOOL_TIERS: dict[str, str] = {
    # ---- prd: AUTHORING a spec, not reading one --------------------------------------------
    # "A coding agent READS a spec and works to it; it does not write one" was in this comment
    # from the first draft — and the first draft tiered `get_prd` anyway, on the excuse that
    # "the whole suite moves together". `test_mcp_annotations` caught it, and its docstring is
    # the argument verbatim: `get_prd` once could not be called by a read-only key, and
    # "absent from a manifest looks like a tool that does not exist" — for the tool whose
    # entire purpose is letting an agent read a PRD. Recreating that would have been this
    # ticket's own subject one level up. The READS are core; authoring is the tier.
    **{name: "prd" for name in (
        "create_prd", "update_prd", "grill_prd", "answer_grill", "decompose_prd",
        "close_prd", "request_rebaseline", "submit_verdict",
    )},

    # ---- codegraph: WRITING the graph ------------------------------------------------------
    # Reads (`search_code`, `get_code_map`, `code_neighbors`, `graph_query`) are core and
    # deliberately so. Describing and linking symbols is an indexing job, run by the sync
    # path and by a human curating the graph, not by an agent in the middle of a change.
    **{name: "codegraph" for name in ("describe_code", "link_code", "unlink_code")},

    # ---- fleet: running a fleet, not being in one -------------------------------------------
    # The distinction that decides this list: an agent that IS in a fleet needs `register_agent`
    # and `heartbeat` (core — gating either deadlocks or de-rosters it, which PRD-17's walk
    # already proved once), and the work verbs it holds by role. An agent that RUNS a fleet
    # needs allocation, enrolment and waves. `fleet.mint` grants this tier for the roles that
    # need it, so no fleet credential has to ask.
    # This list is SHORTER than the obvious one, and the existing role work is what shortened
    # it. `next_cluster`, `claim_cluster`, `fleet_status` and `collision_clusters` were in the
    # first draft and the role-manifest suite rejected all four within a minute: the cluster
    # verbs are `TOOL_ROLES`-gated to the WORKER, so tiering them takes the parallel path away
    # from the one role that uses it, and the other two are in `fleet.OPEN_TOOLS` with the
    # reason written out — "a read; every role must be able to see the board it works on" and
    # "a worker that could not see it would have to be told which files are safe". Being in a
    # fleet is core; RUNNING one is the tier.
    **{name: "fleet" for name in (
        "propose_allocation", "assign_role", "mint_enrolment", "retire_wave",
    )},

    # ---- misc: rare, and none of it mid-task -----------------------------------------------
    **{name: "misc" for name in (
        "extract_lessons", "generate_digest", "report_graphban_issue",
        "learning_loop", "create_project", "publish_memory", "reject_memory",
    )},
}

#: Why a tool is CORE — shipped to every key, tier or no tier.
#:
#: Every tool appears in exactly one of these two maps, and `test_tool_tiers.py` fails if one
#: appears in neither or in both. Copied from `fleet.OPEN_TOOLS`, which exists because
#: `TOOL_ROLES` had no such guard and "forty tools reached that default without anyone arguing
#: for it. Some are certainly right; nobody could tell which from the file." A new tool must
#: now argue for core rather than land in it by being unlisted — which is the direction that
#: keeps this working, since silently defaulting to core is exactly how the manifest grows
#: back to what it was.
CORE_TOOLS: dict[str, str] = {
    # ---- what an agent needs to do one piece of work ---------------------------------------
    "get_context": "the first call every agent makes, and where it learns the tiers exist",
    "search_items": "finding the work",
    "get_item_details": "reading the work",
    "get_backlog": "choosing the work",
    "suggest_next": "choosing the work",
    "related_work": "not repeating work already done",
    "create_item": "recording work that needs doing",
    "update_item": "the work verb; without it a key can do nothing at all",
    "link_items": "a statement about the work",
    "unlink_items": "the inverse of link_items",

    # ---- the claim loop: a solo agent has no fleet to grant it a tier -----------------------
    "claim_next": "taking the work. In a 'loop lifecycle' tier this is the single biggest "
                  "saving available and it is not takeable: a solo agent on a plain key "
                  "would be unable to claim anything, which is not a manifest optimisation",
    "heartbeat": "keeps the agent on the roster as well as the lease alive. PRD-17's walk "
                 "already proved what gating it costs — reviewers and planners registered "
                 "fine and vanished 150s later",
    "release_item": "handing back a hold; an agent that can take work and not give it back "
                    "leaks claims",
    "register_agent": "MUST stay core for the same reason it is ungated: a caller cannot hold "
                      "anything before it registers",

    # ---- review: a plain credential is how a human reviews -----------------------------------
    "claim_review": "review is not fleet administration. Tiering these would mean a reviewer "
                    "credential minted by hand could not see the verbs it exists for",
    "sign_off": "as claim_review",
    "bounce": "as claim_review — and a reviewer that could sign off but not bounce is worse "
              "than one that can do neither",

    # ---- memory: context is what makes the rest work ----------------------------------------
    "add_memory": "writing context mid-task",
    "search_memory": "reading context mid-task",
    "get_lessons": "reading whether a lesson is still worth following mid-task; following a "
                   "contradicted shard is worse than not finding it. Same class as "
                   "search_memory",

    # ---- being IN a fleet, as opposed to running one ---------------------------------------
    "next_cluster": "a worker's verb — `TOOL_ROLES` gates it to the worker, so tiering it "
                    "removes the parallel path from the only role that has it",
    "claim_cluster": "as next_cluster",
    "fleet_status": "in `fleet.OPEN_TOOLS`: a read every role must have to see the board it "
                    "works on",
    "collision_clusters": "in `fleet.OPEN_TOOLS`: a worker that could not see the partition "
                          "would have to be told which files are safe to touch",
    "review_recommendation": "the read a reviewer's job is built on, and the enrolment path is "
                             "why it is core rather than `fleet`: under PRD-19 a seat is taken "
                             "on a HAND-MINTED credential, which has no tiers, so a reviewer "
                             "enrolling on one would have lost this. Reviewing is doing a "
                             "piece of work; only ADMINISTERING a fleet is the tier",

    # ---- reading a spec, as opposed to writing one -----------------------------------------
    "get_prd": "the tool whose entire purpose is letting an agent read a PRD. "
               "`test_mcp_annotations` already had to fix this once, when scope gating left "
               "it out of a read-only manifest",
    "prd_coverage": "a read; an agent working to a spec needs to see what it covers",
    "prd_acceptance": "a read, and the one an agent consults to know what `done` requires",

    # ---- first run -------------------------------------------------------------------------
    "setup_project": "`get_context` returns `empty: true` and points here. An agent told to "
                     "call a tool that is not in its manifest is worse off than one told "
                     "nothing",
    "list_projects": "a global key passes `project_id` per call, and this is how it learns "
                     "what the ids are",

    # ---- code graph READS ---------------------------------------------------------------
    # GRPH-463 wants the graph to be the default read path for a working agent. Tiering the
    # reads would make that impossible by construction.
    "search_code": "reading the graph is the point of having one",
    "get_code_map": "as search_code",
    "code_neighbors": "as search_code",
    "graph_query": "as search_code",
}

#: The tiers that exist, in a fixed order so error messages and `get_context` agree.
TIERS: tuple[str, ...] = ("prd", "codegraph", "fleet", "misc")

#: What each tier is FOR, in one line, shown by `get_context` to a key that lacks it. This is
#: the affordance: a tier nobody is told about is a tier nobody asks for.
TIER_PURPOSE: dict[str, str] = {
    "prd": "authoring and grilling PRDs — write specs, run a grill, decompose, close",
    "codegraph": "writing the code graph — describe symbols and link them",
    "fleet": "running a fleet — allocation, roles, enrolment codes, waves",
    "misc": "occasional tools — projects, digests, lessons, memory review",
}


def visible(names: list[str], granted: list[str] | None) -> list[str]:
    """Filter tool NAMES to core plus the granted tiers.

    `granted` of `None` means "no tiers", not "all tiers". A key minted before this shipped
    has no tiers, and defaulting those to everything would make the change a no-op on exactly
    the keys already in the wild — which is every key on a running deployment.
    """
    allow = set(granted or ())
    return [n for n in names if n not in TOOL_TIERS or TOOL_TIERS[n] in allow]


def missing(granted: list[str] | None) -> list[str]:
    """Tiers this key does NOT hold, in `TIERS` order."""
    allow = set(granted or ())
    return [t for t in TIERS if t not in allow]


def unknown(requested: list[str]) -> list[str]:
    """Requested tiers that do not exist.

    Validated on the way in rather than ignored, for the reason `API_KEY_SCOPES` gives about
    scopes: an unrecognised tier would silently produce a key that never widens, and the
    failure surfaces much later as a tool the agent cannot see.
    """
    return [t for t in requested if t not in TIERS]
