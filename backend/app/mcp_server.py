"""MCP endpoint (JSON-RPC 2.0 over HTTP).

Exposes the live Graphban tools to agents, authenticated by a scoped API key
(the count is `LIVE_TOOL_COUNT`, derived from `TOOLS` — never hardcode it).
Every tool calls the shared service layer, so an agent's writes are identical to
what the web app produces — one code path.

Handled methods: `initialize`, `tools/list`, `tools/call`, and the
`notifications/initialized` notification. Single JSON responses (no SSE) keep it
`curl`-friendly while remaining MCP Streamable-HTTP compatible for simple calls.
"""
from __future__ import annotations

import json
import secrets
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import errors
from app.config import settings
from app.db import get_db
from app.models import ApiKey, ArtifactRecommendation, Item, Link, MemoryShard, Project
from app.security import authz
from app.security.deps import get_agent_key
from app.services import clustering as cluster_svc
from app.services import code_graph as code_svc
from app.services import events as events_svc
from app.services import fleet as fleet_svc
from app.services import idempotency as idem_svc
from app.services import insights as insights_svc
from app.services import items as items_svc
from app.services import keys
from app.services import links as links_svc
from app.services import mcp_proxy
from app.services import mcp_stats
from app.services import memory as mem_svc
from app.services import setup as setup_svc
from app.services import quotas
from app.services import artifacts as art_svc
from app.services import prds as prd_svc
from app.services import projects as projects_svc
from app.services import prioritization as prio_svc
from app.services import requests as req_svc
from app.services import upstream as up_svc

import httpx

router = APIRouter(tags=["mcp"])

PROTOCOL_VERSION = "2025-11-25"  # latest finalized MCP spec our tools surface conforms to
# Versions this server is compatible with. Per the spec's negotiation rule we echo the
# client's requested version when it's one of these, else advertise PROTOCOL_VERSION.
SUPPORTED_PROTOCOL_VERSIONS = frozenset({"2025-03-26", "2025-06-18", "2025-11-25"})

logger = logging.getLogger("graphban.mcp")

_STATUS_ENUM = items_svc.STATUSES
_FIDELITY_ENUM = items_svc.FIDELITIES
_LINK_TYPE_ENUM = links_svc.LINK_TYPES
_REQUEST_TYPE_ENUM = req_svc.REQUEST_TYPES
_EFFORT_DESC = "Relative effort estimate (higher = more work). No fixed unit."

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_context",
        "description": (
            "Orient yourself: returns the project this API key writes to, your scopes, and how "
            "many projects and tools exist. Call this first when you start."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_projects",
        "description": "List all projects (id, name, tag, accent, description). `tag` is the short prefix its item/request/PRD keys render with. Use an id as the `project_id` override.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_project",
        "description": (
            "Create a project when the one you need doesn't exist yet. SELF-HOST ONLY, and only "
            "while this instance is not linked to a cloud org. `tag` is derived from the name "
            "when omitted. A HUMAN should still confirm it's the right workspace — you can "
            "create one, but you can't know it's the one they meant."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "tag": {"type": "string", "description": "2-4 chars, e.g. GB. Derived if omitted."},
                "description": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "setup_project",
        "description": (
            "First-run bootstrap: returns an ordered, resumable checklist to take a fresh project "
            "from empty to useful — confirm the project, build the code graph, load memories, "
            "propose work items. Read-only guidance; each step reports done/pending from current "
            "state, so re-run it any time. Call this when get_context flags an empty project."
        ),
        "inputSchema": {"type": "object", "properties": {"project_id": {"type": "string"}}},
    },
    {
        "name": "create_item",
        "description": "Create a tracker item. Returns the created item incl. its id and project_id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string", "description": "Markdown body."},
                "tags": {"type": "array", "items": {"type": "string"}},
                "touchpoints": {"type": "array", "items": {"type": "string"},
                                "description": "Files/globs/modules this affects, e.g. backend/app/routers/*. Powers clustering."},
                "effort": {"type": "integer", "description": _EFFORT_DESC},
                "status": {"type": "string", "enum": _STATUS_ENUM, "description": "Defaults to backlog."},
                "fidelity": {"type": "string", "enum": _FIDELITY_ENUM,
                             "description": "`low` (specifiable now) or `high` (needs a prototype first). Defaults to low."},
                "prd_id": {"type": "string", "description": "The PRD this task implements (traceability)."},
                "prd_section": {"type": "string", "description": "The PRD section this task implements."},
            },
            "required": ["title"],
        },
    },
    {
        "name": "update_item",
        "description": "Patch fields or advance status on an existing item. Returns the updated item.",
        "inputSchema": {
            "type": "object",
            "properties": {
                # ADVERTISED, and it was not — so the role gate on `status: done` was unreachable through the published schema. A worker wrote `done` on the acceptance walk by simply not sending what it had no way to know it should.
                "agent_id": {"type": "string", "description": "You. Required to move an item past `review` when a fleet is running on this credential."},
                "id": {"type": "string"},
                "status": {"type": "string", "enum": _STATUS_ENUM},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "touchpoints": {"type": "array", "items": {"type": "string"},
                                "description": "Files/globs/modules this item affects (for clustering)."},
                "effort": {"type": "integer", "description": _EFFORT_DESC},
                "blocker": {"type": "string", "description": "Free-text blocker; empty string clears it."},
                "fidelity": {"type": "string", "enum": _FIDELITY_ENUM,
                             "description": "`low` or `high` (needs a prototype first)."},
                "prd_id": {"type": "string"},
                "prd_section": {"type": "string"},
                "evidence": {
                    "type": "array",
                    # The kind list that used to live here is dropped, not squeezed: the
                    # `kind` enum below already carries it, and paying manifest tokens twice
                    # for one fact is what the footprint guard exists to catch. Net +1 char
                    # against main, so the ceiling stays exactly as tight as it was.
                    "description": "Proof-on-done receipts matched to the completion claim; "
                                   "kinds below. APPENDS — sending some never removes the rest.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "enum": ["test", "url", "screenshot", "health", "note", "sabotage"]},
                            "detail": {"type": "string", "description": "One-line summary."},
                            "url": {"type": "string", "description": "Link to the artifact."},
                            "claim": {"type": "string", "description": "sabotage: what you broke."},
                            "mutation": {"type": "string", "description": "sabotage: how."},
                            "tests_failed": {"type": "integer", "description": "sabotage: how many failed. ZERO means the test cannot fail — a finding."},
                        },
                    },
                },
                "ack_section_drift": {"type": "boolean",
                                      "description": "Its PRD section changed and this item is "
                                                     "still right; stop flagging until it "
                                                     "changes again."},
            },
            "required": ["id"],
        },
    },
    {
        "name": "search_items",
        "description": "Query the linear stream by free text (title, description, tags), tags, and/or status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Substring matched against title, description, and tags."},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Only items carrying at least one of these tags."},
                "status": {"type": "string", "enum": _STATUS_ENUM},
            },
        },
    },
    {
        "name": "add_memory",
        "description": (
            "Record a memory shard (decision, lesson, or note) on an item or the global scope. "
            "ALWAYS check the returned `status`: a `candidate` will NOT come back from "
            "search_memory unless you pass include_candidates. The project's memory write mode "
            "decides — `review` (default) holds it for a human, `trusted` publishes on write so "
            "you can read it back immediately. Near-duplicates are auto-rejected in every mode."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "scope": {"type": "string", "enum": ["global", "item"]},
                "item_id": {"type": "string"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "search_memory",
        "description": (
            "Semantic search over memory shards — recall past context before acting. Returns "
            "published shards ranked by similarity (with score and `status`); set "
            "`include_candidates: true` to also see unreviewed agent notes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer"},
                "include_candidates": {
                    "type": "boolean",
                    "description": "Also return unpublished candidate shards (default false).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "publish_memory",
        "description": (
            "SUBMIT one of your candidate shards for adjudication — you do not publish it, an "
            "independent judge decides. Returns `{shard, verdict}`; `kept: false` means the "
            "judge rejected it, a normal outcome. Needs the project to allow agent adjudication "
            "and a real chat model, else the shard stays a candidate."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"shard_id": {"type": "string"}},
            "required": ["shard_id"],
        },
    },
    {
        "name": "reject_memory",
        "description": (
            "Discard one of your candidate shards — a note you now know is wrong, "
            "superseded, or noise. Unlike publishing this needs no judge: removing your "
            "own candidate takes nothing out of the trusted pool. The shard is kept for "
            "provenance and never surfaces in search."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "shard_id": {"type": "string"},
                "reason": {"type": "string", "description": "Why it's being discarded."},
            },
            "required": ["shard_id"],
        },
    },
    {
        "name": "get_backlog",
        "description": (
            "Prioritized backlog/next items, ready-first then by composite score. Each row carries "
            "`ready`, `blocked_by` (unfinished deps), `unblocks`, `votes`, `score`. Rows are lean "
            "(id/title/status) by default; `fields=full` for all item fields."
        ),
        "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}},
    },
    {
        "name": "get_item_details",
        "description": (
            "The full record for one item — its description, blockers, dependencies, and linked "
            "memory shards. Call after search_items/get_backlog to read an item before working it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "suggest_next",
        "description": (
            "Advisory: the single best next item to work, WITHOUT claiming it (use claim_next to "
            "lock work in a loop). Returns {item}; item is null when nothing is ready."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "link_items",
        "description": "Create a typed relationship between two items.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "string"},
                "b": {"type": "string"},
                "type": {"type": "string", "enum": _LINK_TYPE_ENUM},
                "reason": {"type": "string"},
            },
            "required": ["a", "b"],
        },
    },
    {
        "name": "unlink_items",
        "description": (
            "Remove a typed relationship between two items — the inverse of link_items. Omit "
            "`type` to remove every link type for the (a, b) pair. Idempotent: returns "
            "`removed` (0 if the link didn't exist)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "string"},
                "b": {"type": "string"},
                "type": {"type": "string", "enum": _LINK_TYPE_ENUM},
            },
            "required": ["a", "b"],
        },
    },
    {
        "name": "extract_lessons",
        "description": "Auto-distill decisions/learnings from an item into memory.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "generate_digest",
        "description": "Compose a periodic progress digest across the project.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "prd_coverage",
        "description": (
            "Spec-to-task rollup for a PRD: per-section task counts by status, coverage %, and "
            "`gaps` (sections with no tasks yet). Read-only."
        ),
        "inputSchema": {"type": "object", "properties": {"prd_id": {"type": "string"}}, "required": ["prd_id"]},
    },
    {
        "name": "decompose_prd",
        "description": (
            "One task per un-covered PRD section, each carrying the PRD's framing prose so it "
            "needs no other reading. `create=true` files them, linked to PRD+section. Framing "
            "sections aren't tasks unless `include_prose=true`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prd_id": {"type": "string"},
                "create": {"type": "boolean", "description": "Create the proposed tasks (default false = dry-run)."},
                "include_prose": {
                    "type": "boolean",
                    "description": "Also propose tasks for framing sections (default false).",
                },
            },
            "required": ["prd_id"],
        },
    },
    {
        "name": "create_prd",
        "description": (
            "Author a PRD (the durable handoff artifact). Use `## ` markdown headings for sections — "
            "decompose_prd turns each into tracked work. Pass `body` for a full draft or `template` "
            "(standard|blank) for a skeleton. Returns the PRD incl. id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string", "description": "Full markdown draft (wins over template). Use `## ` section headings."},
                "template": {"type": "string", "enum": ["standard", "blank"], "description": "Skeleton when no body (default standard)."},
            },
            "required": ["title"],
        },
    },
    {
        "name": "get_prd",
        "description": (
            "The full PRD including its markdown `body`. Read before update_prd, which "
            "REPLACES the body whole — editing without reading deletes what you did not "
            "reproduce."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"prd_id": {"type": "string"}},
            "required": ["prd_id"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "project_id": {"type": "string"},
                           "title": {"type": "string"}, "status": {"type": "string"},
                           "version": {"type": "string"}, "body_hash": {"type": "string"}, "body": {"type": "string"}},
        },
    },
    {
        "name": "update_prd",
        "description": (
            "Patch a PRD's title, status (draft|review), or body. Prefer `section` — it "
            "rewrites one `## ` heading and leaves every other byte alone. A whole-body "
            "replace needs `base_hash` from get_prd. Returns the updated PRD."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prd_id": {"type": "string"},
                "title": {"type": "string"},
                # `approved` stays in the enum deliberately: dropping it makes the
                # generic schema check fire first, so an agent gets "invalid status"
                # instead of the guard's message naming what is still outstanding.
                "status": {"type": "string", "enum": ["draft", "review", "approved"],
                           "description": "`approved` is NOT settable — it is reached by "
                                          "finishing the grill (see answer_grill)."},
                "section": {"type": "string",
                            "description": "A `## ` heading. `body` then replaces ONLY that "
                                           "section; the rest is untouched."},
                "base_hash": {"type": "string",
                              "description": "`body_hash` from get_prd. Required for a "
                                             "whole-body replace; refused if it has moved."},
                "body": {"type": "string",
                         "description": "New markdown — one section's contents with "
                                        "`section`, else the entire body."},
            },
            "required": ["prd_id"],
        },
    },
    {
        "name": "answer_grill",
        "description": (
            "Relay the author's answer to a grill question. Ask them first and record what they "
            "actually said — do NOT answer on their behalf. Returns `outstanding` (dimensions "
            "still unanswered) and `complete`. **`graded: false` means the grader could not be "
            "asked and this answer was NOT judged.** Call grill_prd for the next questions. Recorded "
            "as agent-relayed and visible to whoever reviews later."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prd_id": {"type": "string"},
                "answer": {"type": "string", "description": "What the author said, in their words."},
            },
            "required": ["prd_id", "answer"],
        },
    },
    {
        "name": "grill_prd",
        "description": (
            "Next batch of clarifying questions to sharpen a PRD before building (the 'grill' "
            "technique) — surfaces unstated assumptions, scope edges and failure modes, "
            "favoring ones answerable in words. Markdown list; answer via update_prd."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"prd_id": {"type": "string"}},
            "required": ["prd_id"],
        },
    },
    {
        "name": "related_work",
        "description": (
            "Items related to a given item by shared touchpoints (files/globs/modules it affects) "
            "and typed links, best-first — the code-neighborhood around a task. Read-only."
        ),
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    },
    {
        "name": "next_cluster",
        "description": (
            "Claim a whole code-neighborhood: the best ready item plus its related ready items "
            "(up to max_items), all assigned to you. Returns the claimed batch, seed first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "max_items": {"type": "integer", "description": "Max items to claim in the cluster (default 3)."},
            },
        },
    },
    {
        "name": "claim_next",
        "description": (
            "Claim ONE ready item (unblocked backlog/next) and move it to in_progress. Two agents "
            "never get the same item, but this reserves NO files — prefer claim_cluster in a fleet. "
            "Returns {claimed, item}; item is null when nothing is ready, and `reserved` then names "
            "work held for somebody else."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Who is claiming; defaults to this API key's name."},
                "lease_seconds": {"type": "integer", "description": "Lease length; a claim with no heartbeat within this window is reclaimable (default 600)."},
                "wait_seconds": {"type": "integer", "description": "Block up to N seconds (max 60) instead of returning empty. A directive wakes it early."},
                "skip": {"type": "array", "items": {"type": "string"}, "description": "Ids to pass over."},
            },
        },
    },
    {
        "name": "propose_allocation",
        "description": (
            "What the fleet should look like given who is online and what is ready: how many "
            "workers and reviewers, and which cluster each worker takes. A PROPOSAL — nothing "
            "is assigned until a planner calls assign_role. Agents beyond the free clusters "
            "are proposed as reviewers rather than left for the divvy to refuse."
        ),
        "inputSchema": {"type": "object", "properties": {"project_id": {"type": "string"}}},
    },
    {
        "name": "assign_role",
        "description": (
            "Commit a role change for ANOTHER agent. It reaches them on their next poll as a "
            "`directive` — no reconnect, no re-prime. Refused if their credential is not "
            "eligible for that role. `agent_id` is you, the caller; `target_agent_id` is who "
            "you are re-tasking."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target_agent_id": {"type": "string"},
                "role": {"type": "string", "enum": list(fleet_svc.ROLES)},
                "reason": {"type": "string"},
                "agent_id": {"type": "string"},
            },
            "required": ["target_agent_id", "role"],
        },
    },
    {
        "name": "collision_clusters",
        "description": (
            "Partition ready work into clusters that provably do not share touch-areas — the "
            "divvy a planner allocates from. `predicted: true` means the grouping leaned on "
            "inferred areas rather than declared ones, so treat it as lower confidence."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "string"}, "status": {"type": "string"}},
        },
    },
    {
        "name": "claim_cluster",
        "description": (
            "The way to take work. Claims a whole non-colliding cluster and reserves its "
            "touch-areas against work in flight, so nobody is handed work that collides with "
            "yours. `claimed: false` names who holds the areas and when the earliest frees — "
            "a real answer, not a failure. Write actual `touchpoints` back via update_item "
            "when you finish. A low max_items frees nothing for others: the whole cluster is "
            "reserved either way."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "max_items": {"type": "integer"},
                "lease_seconds": {"type": "integer"},
                "wait_seconds": {"type": "integer", "description": "Block up to N seconds (max 60) instead of returning empty. A directive wakes it early."},
            },
        },
    },
    {
        "name": "claim_review",
        "description": (
            "Lease an item in review that you did NOT build. `claimed` is false when nothing "
            "qualifies — including when the only work in review is your own. `branch` is where "
            "the diff is."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"agent_id": {"type": "string"},
                "wait_seconds": {"type": "integer", "description": "Block up to N seconds (max 60) instead of returning empty. A directive wakes it early."},
                "skip": {"type": "array", "items": {"type": "string"}, "description": "Ids to pass over."},
            },
        },
    },
    {
        "name": "sign_off",
        "description": (
            "Take a reviewed item to `done` with evidence. Refused if you built it, whatever "
            "role you hold — and, above an effort threshold, refused without a `sabotage` "
            "receipt showing something was broken on purpose and a test caught it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "agent_id": {"type": "string"},
                "evidence": {"type": "array"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "bounce",
        "description": (
            "Send a reviewed item back to `next` with a required reason. Reserved for its "
            "author for one lease period — they still hold the worktree — then opens to the "
            "fleet."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "agent_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["id", "reason"],
        },
    },
    {
        "name": "register_agent",
        "description": (
            "Register THIS process as an agent at startup, before claiming work. Two terminals on "
            "one key become two agents. Pass `enrolment_code` if you were given a seat; without "
            "one you are `all-in-one`. Read back `active_role` and `tools_off_limits` — your tool "
            "list was fetched before you had a role, so it still shows tools you will be refused. "
            "Heartbeat at the returned interval or you go offline and your items requeue."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "e.g. 'opus-5 @ macbook:wt-2'. Duplicates allowed."},
                "capabilities": {"type": "object", "description": "{vendor, model, tier, readonly, host}; vendor drives review diversity."},
                "worktree": {"type": "string"},
                "branch": {"type": "string"},
                "role_hint": {"type": "string", "enum": list(fleet_svc.ROLES)},
                "enrolment_code": {"type": "string", "description": "Your seat, e.g. 'WORKER-7F3K'. Grants your role and beats role_hint. Single-use."},
                # "who spawned you" is the phrasing that caused the trouble: a process a
                # supervisor launched has an obvious answer to it, and the answer is wrong.
                # It is a separate PROCESS, not a subagent inside anyone's turn, and
                # declaring a parent would make it and its reviewer one call tree
                # (`independent`), so review across a spawned fleet would silently stop
                # meaning anything. Naming the distinction is the whole job here; the old
                # second sentence explained the consequence in a way that made setting the
                # field sound like the careful choice. Net 5 chars shorter — the manifest
                # budget has no room (test_mcp_footprint).
                "parent_agent_id": {"type": "string", "description": "ONLY if you run inside another agent's turn. A spawned process is not one."},
            },
        },
    },
    {
        "name": "mint_enrolment",
        "description": (
            "PLANNER ONLY. Mint a seat for an agent you are spawning, bounded by your "
            "credential. Returned once — pass it as `enrolment_code`. One seat per agent: "
            "two on one seat cannot review each other."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "role": {"type": "string", "enum": list(fleet_svc.ROLES) + [fleet_svc.ALL_IN_ONE]},
                "wave": {"type": "string"},
            },
            "required": ["agent_id", "role"],
        },
    },
    {
        "name": "retire_wave",
        "description": (
            "PLANNER ONLY. Revoke the seats YOU minted and release what agents on them hold, "
            "in one step. Does NOT stop any process — those keep building against dead seats "
            "until something stops them, and `agents_still_running` names which."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "wave": {"type": "string", "description": "Narrow to one wave."},
            },
            "required": ["agent_id"],
        },
    },
    {
        "name": "fleet_status",
        "description": (
            "Who else is working this project: agents, roles, presence, and what each holds. "
            "Presence is derived from last contact, so a dead agent reads `offline` with nothing "
            "having reported it. `agent_id` adds the seats you minted."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
            },
        },
    },
    {
        "name": "heartbeat",
        "description": "Extend the lease on an item you've claimed so it isn't reclaimed while you work.",
        "inputSchema": {
            "type": "object",
            "properties": {
                # OPTIONAL. Heartbeat does two jobs — extend an item LEASE and extend agent
                # PRESENCE — and only the first needs an item. A planner never holds one, and
                # a reviewer between reviews or a worker between claims holds none either, so
                # requiring it meant presence was maintainable only while mid-work. Found on
                # the PRD-17 walk, alongside the role gate that refused non-workers outright.
                "id": {"type": "string", "description": "Item whose lease to extend. Omit to report presence only."},
                "agent_id": {"type": "string"},
            },
        },
    },
    {
        "name": "release_item",
        "description": "Return a claimed item to the queue (e.g. you can't finish it); moves it back to `next` by default.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "agent_id": {"type": "string"},
                "to_status": {"type": "string", "enum": items_svc.STATUSES},
            },
            "required": ["id"],
        },
    },
    {
        "name": "describe_code",
        "description": (
            "Upsert the codebase's structure as a queryable graph of `nodes` and `edges`. You "
            "have the repo in context, so you are the source of truth. Idempotent per path — "
            "re-describe a changed file with its new `content_hash`. `prune=true` after a whole "
            "subtree marks unseen nodes stale. A `kind` contradicting its path is corrected and "
            "returned in `kind_corrections`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "nodes": {
                    "type": "array",
                    "description": "Code units to upsert.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Repo-relative."},
                            "kind": {"type": "string", "enum": code_svc.NODE_KINDS,
                                     "description": "package | file | `path::name` | prose | settings. Include docs and config."},
                            "name": {"type": "string", "description": "Short label."},
                            "lang": {"type": "string", "description": "python | ts | ... (optional)."},
                            "summary": {"type": "string", "description": "One paragraph: what it is and owns."},
                            "content_hash": {"type": "string", "description": "`sha256:<hex>` of rstripped contents."},
                        },
                        "required": ["path"],
                    },
                },
                "edges": {
                    "type": "array",
                    "description": "Directed, typed relations between paths. A dst need not be a described node yet.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "src": {"type": "string"},
                            "dst": {"type": "string"},
                            "type": {"type": "string", "enum": code_svc.EDGE_TYPES},
                        },
                        "required": ["src", "dst"],
                    },
                },
                "prune": {"type": "boolean", "description": "Mark project nodes absent from this batch as stale (default false)."},
            },
        },
    },
    {
        "name": "get_code_map",
        "description": (
            "The project's code graph: described nodes (path, kind, summary, fresh) and the typed "
            "edges between them. Optionally filter by `kind`. Read the codebase's shape without a checkout."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"kind": {"type": "string", "enum": code_svc.NODE_KINDS}},
        },
    },
    {
        "name": "code_neighbors",
        "description": (
            "The neighborhood around a code path: incoming/outgoing edges by type plus work items "
            "touching it. Answers what depends on this / what it depends on / what work touches it. "
            "Works even for a path that isn't a described node yet."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "graph_query",
        "description": (
            "Structure of the code graph, before you refactor. `hubs`: nodes by INBOUND edges — "
            "what breaks if this changes. `components`: connected groups, largest first, each with "
            "an `anchor`. `path`: shortest route between two paths, walked undirected, each hop "
            "reporting which way the edge points. Deterministic."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"enum": ["hubs", "components", "path"], "type": "string"},
                "a": {"description": "Start path, for `path`.", "type": "string"},
                "b": {"description": "End path, for `path`.", "type": "string"},
                "edge_types": {"items": {"type": "string"}, "type": "array"},
                "limit": {"description": "Rows for `hubs` (10).", "type": "integer"},
            },
            "required": ["query"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "results": {"type": "array"},
                           "returned": {"type": "integer"}, "found": {"type": "boolean"},
                           "hops": {"type": "array"}},
        },
    },
    {
        "name": "search_code",
        "description": "Semantic search over code-node summaries (pgvector cosine). Returns ranked nodes with scores.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "link_code",
        "description": (
            "Link a tracker item or request to a code path — a typed edge between the work and the "
            "code graph. `ref_id` is an item (AL-12) or request (R-31) id; the type is inferred. "
            "Idempotent; surfaces on both the code node (code_neighbors) and the item/request."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref_id": {"type": "string", "description": "Item id (e.g. AL-12) or request id (e.g. R-31)."},
                "path": {"type": "string", "description": "Code path to link to (need not be a described node yet)."},
                "relation": {"type": "string", "enum": code_svc.REF_RELATIONS, "description": "Defaults to affects."},
                "ref_type": {"type": "string", "enum": code_svc.REF_TYPES, "description": "Usually inferred from the id; set to disambiguate."},
            },
            "required": ["ref_id", "path"],
        },
    },
    {
        "name": "unlink_code",
        "description": "Remove links from an item/request to a code path. Omit `relation` to remove all relations for that pair.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref_id": {"type": "string"},
                "path": {"type": "string"},
                "relation": {"type": "string", "enum": code_svc.REF_RELATIONS},
            },
            "required": ["ref_id", "path"],
        },
    },
    # Delivery acceptance (PRD-12 / GRPH-254). Four tools, not eight: the item flags the
    # `tools/list` footprint (AL-146/AL-48), and every READ here is the same question asked
    # of a different surface, so they take a `view` instead of each claiming a name. There
    # is deliberately no invalidation tool — the hold rides on every item an agent reads
    # (GRPH-242/312), because a notice you have to remember to fetch is one you miss.
    {
        "name": "prd_acceptance",
        "description": (
            "Delivery acceptance for a PRD, read-only; `view` picks the surface. "
            "completeness (baselined sections with nothing delivered — the only pass that "
            "surfaces ABSENT work), drift, evidence, close_report (vs ORIGINAL intent, not "
            "the governing baseline), readiness, lineage, verdicts, baseline, "
            "classifications, audit_brief, audit_coverage. None report "
            "'complete' — they describe what happened; the judgement is yours."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prd_id": {"type": "string"},
                "view": {
                    "type": "string",
                    "enum": ["completeness", "drift", "evidence", "close_report",
                             "readiness", "lineage", "verdicts", "baseline",
                             "classifications", "audit_brief", "audit_coverage"],
                },
            },
            "required": ["prd_id", "view"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {"prd_id": {"type": "string"}, "view": {"type": "string"},
                           "result": {"type": "object"}},
        },
    },
    {
        "name": "request_rebaseline",
        "description": (
            "Ask for new frozen intent on an approved PRD, in your OWN words. Does NOT "
            "approve: it re-opens the grill, and the existing baseline governs until a new "
            "one is earned. Cannot ADD sections (that is a sub-PRD); refused on a closed PRD."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prd_id": {"type": "string"},
                "reason_type": {"type": "string",
                                "enum": ["learning", "scope-change", "correction"]},
                "reason": {"type": "string"},
            },
            "required": ["prd_id", "reason_type", "reason"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "status": {"type": "string"},
                           "pending_rebaseline": {"type": "object"}},
        },
    },
    {
        "name": "submit_verdict",
        "description": (
            "Record a sign-off claim about a PRD. Citing nothing, or citing something that "
            "does not resolve, is REJECTED as malformed. Citations are {kind, ref}: code (a "
            "code-graph path), intent (a baseline section — what an ABSENCE finding cites), "
            "evidence (an item key). Signing work you claimed is flagged, not refused."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prd_id": {"type": "string"},
                "section": {"type": "string"},
                "outcome": {"type": "string"},
                "reasoning": {"type": "string"},
                "citations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string",
                                     "enum": ["code", "intent", "evidence"]},
                            "ref": {"type": "string"},
                        },
                        "required": ["kind", "ref"],
                    },
                },
            },
            "required": ["prd_id", "outcome", "citations"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}, "outcome": {"type": "string"},
                           "self_signed": {"type": "boolean"},
                           "separation": {"type": "string"},
                           "baseline_version": {"type": "string"}},
        },
    },
    {
        "name": "close_prd",
        "description": (
            "Close a PRD — terminal and irreversible. Gates on DISPOSITION, not delivery: "
            "every section with nothing delivered (prd_acceptance view=completeness) must "
            "appear exactly once as promoted (to an item or successor PRD) or deferred with a "
            "reason. No edit, rebaseline or reopen after; post-close work is a new PRD."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prd_id": {"type": "string"},
                "verdict": {"type": "string"},
                "dispositions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "section": {"type": "string"},
                            "disposition": {"type": "string",
                                            "enum": ["promoted", "deferred"]},
                            "promote_to": {"type": "string", "enum": ["item", "prd"]},
                            "reason": {"type": "string"},
                            "title": {"type": "string"},
                        },
                        "required": ["section", "disposition"],
                    },
                },
            },
            "required": ["prd_id", "dispositions"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {"mode": {"type": "string"},
                           "baseline_version": {"type": "string"},
                           "disclosure": {"type": "string"},
                           "dispositions": {"type": "array", "items": {"type": "object"}}},
        },
    },
    # The learning loop (PRD-16 / GRPH-310). Two tools, same footprint discipline as the
    # acceptance surface: every read is one question asked of a different surface, so they
    # share a `view` instead of claiming four names.
    {
        "name": "learning_loop",
        "description": (
            "Read the learning loop. `view`: recommendations (pending proposals), artifact "
            "(one, with its draft and whether it may install), usage (uses is null for a "
            "tier whose use cannot be OBSERVED, never 0), stale (only observable tiers — "
            "zero uses elsewhere is not evidence of disuse)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "view": {"type": "string",
                         "enum": ["recommendations", "artifact", "usage", "stale"]},
                "id": {"type": "integer"},
                "project_id": {"type": "string"},
            },
            "required": ["view"],
        },
        "outputSchema": {"type": "object", "properties": {"view": {"type": "string"},
                                                          "result": {"type": "object"}}},
    },
    {
        "name": "review_recommendation",
        "description": (
            "Approve or reject a proposed artifact — the human boundary. Approving writes "
            "nothing: a `shared_surgery` artifact (an edit inside a file others live in) is "
            "only ever proposed, with its contents returned for a human to apply."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "integer"},
                           "decision": {"type": "string", "enum": ["approve", "reject"]}},
            "required": ["id", "decision"],
        },
        "outputSchema": {"type": "object",
                         "properties": {"id": {"type": "integer"},
                                        "status": {"type": "string"},
                                        "install_class": {"type": "string"}}},
    },
    {
        "name": "report_graphban_issue",
        "description": (
            "Report a bug or idea about Graphban ITSELF (not your project) to its maintainers — "
            "a limitation, broken tool, or improvement. Deduped on arrival. Returns the upstream "
            "request id (or matched duplicates)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": _REQUEST_TYPE_ENUM, "description": "Defaults to feedback."},
                "title": {"type": "string"},
                "detail": {"type": "string", "description": "What happened / what you'd want. Include repro if it's a bug."},
            },
            "required": ["title"],
        },
    },
]

# Project-scoped tools accept an optional `project_id` that overrides the key's project.
_PROJECT_SCOPED = {
    "create_item", "search_items", "add_memory", "search_memory",
    "get_backlog", "suggest_next", "generate_digest", "link_items", "unlink_items", "claim_next", "next_cluster",
    "describe_code", "get_code_map", "code_neighbors", "search_code", "graph_query",
    "link_code", "unlink_code", "create_prd",
}
# Tools that ACCEPT `project_id`, which is a different question from the one above.
#
# `_PROJECT_SCOPED` answers "cannot run at all without a project in scope" — the dispatcher
# refuses those when `pid` is None. It was also driving the MANIFEST, so a tool that quietly
# uses `pid` while not being in that set took a parameter it never advertised. Eight did
# (GRPH-474), and seven of them CHOOSE WHAT TO ACT ON by project: `claim_review` picks work
# from `pid`, `fleet_status` reports on it, `register_agent` registers into it. An agent
# reading `tools/list` — the only contract it has — could not discover the parameter, so on a
# key spanning several projects it fell through to `allowed[0]`, an arbitrary ordering.
#
# `sign_off` is here for a weaker reason, stated so it is not mistaken for the others: its
# only use of `pid` is the audit event's attribution. The item itself comes from
# `_scoped_item(..., allowed)` and lands where it was named.
#
# `test_manifest_declares_the_project_parameter` derives this from the source rather than
# trusting the set, so a NEW tool that starts reading `pid` fails until it is declared.
_TAKES_PROJECT = _PROJECT_SCOPED | {
    "claim_cluster", "claim_review", "fleet_status", "get_context",
    "mint_enrolment", "register_agent", "retire_wave", "sign_off",
}
# Creates accept an idempotency key so a retried call returns the original resource.
_IDEMPOTENT_CREATES = {"create_item", "add_memory", "link_items"}
# Writes that are idempotent by their own natural key (no idempotency token needed).
_IDEMPOTENT_WRITES = {"describe_code", "link_code", "review_recommendation"}
# Paged reads accept limit + offset and return {results, total, limit, offset, has_more}.
_PAGED = {"search_items", "get_backlog"}
# Item-list reads return a lean row by default (id/title/status) and the full item
# only on fields="full" — cuts the per-scan payload sharply (AL-78).
_LEAN_LIST = {"search_items", "get_backlog"}
# Write tools whose target is a tracker item (for audit target_type labeling).
_ITEM_WRITE_TOOLS = {"create_item", "update_item", "claim_next", "heartbeat", "release_item"}
# Read-only tools never mutate state.
_READ_ONLY = {
    "get_context", "list_projects", "setup_project", "search_items", "search_memory",
    "get_backlog", "get_item_details", "suggest_next", "generate_digest", "related_work",
    "prd_coverage", "grill_prd", "get_code_map", "code_neighbors", "search_code",
    "graph_query",
    "prd_acceptance", "learning_loop", "fleet_status",
    # Added with the tool itself missing from this set (GRPH-519 -> GRPH-48). It looks a PRD
    # up and returns fields; it writes nothing, and its own literal declared
    # `readOnlyHint: True` — which the build loop then overwrote to False, because THIS is
    # where read-ness is decided. The wrong hint was the visible half. The half that
    # mattered: scope gating ships a read-only key exactly this set, so a read-only
    # credential could not call the tool that exists to let an agent read a PRD.
    "get_prd",
}

_PAGE_META = {  # shared output shape for paged reads (#9)
    "type": "object",
    "properties": {
        "results": {"type": "array"},
        "total": {"type": "integer"},
        "limit": {"type": "integer"},
        "offset": {"type": "integer"},
        "has_more": {"type": "boolean"},
    },
}

# --- Output schemas (#8): every tool's structuredContent shape. ---
_STR = {"type": "string"}
_NULLABLE_STR = {"type": ["string", "null"]}
_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "id": _STR,
        "project_id": _NULLABLE_STR,
        "title": _STR,
        "status": {"type": "string", "enum": _STATUS_ENUM},
        "tags": {"type": "array", "items": _STR},
        "touchpoints": {"type": "array", "items": _STR},
        "effort": {"type": "integer"},
        "assignee": _STR,
        "claimed_by": _NULLABLE_STR,
        "prd_id": _NULLABLE_STR,
        "prd_section": _STR,
        "fidelity": {"type": "string", "enum": _FIDELITY_ENUM},
        "evidence": {"type": "array", "items": {"type": "object"}},
    },
}
_SHARD_SCHEMA = {
    "type": "object",
    "properties": {
        "id": _STR, "text": _STR, "scope": _STR,
        "item_id": _NULLABLE_STR, "project_id": _NULLABLE_STR, "status": _STR,
    },
}

_PRD_SCHEMA_REF = {
    "type": "object",
    "properties": {
        "id": _STR, "project_id": _NULLABLE_STR, "title": _STR, "status": _STR,
        "version": _STR, "sections": {"type": "array", "items": _STR},
        "linked": {"type": "array", "items": _STR}, "body": _STR,
    },
}

_OUTPUT_SCHEMAS: dict[str, dict] = {
    "get_context": {
        "type": "object",
        "properties": {
            "project_id": _NULLABLE_STR,
            "project_name": _NULLABLE_STR,
            "key_project_id": _NULLABLE_STR,
            "scopes": {"type": "array", "items": _STR},
            "project_count": {"type": "integer"},
            "tool_count": {"type": "integer"},
            "empty": {"type": "boolean"},
        },
    },
    "list_projects": {
        "type": "object",
        "properties": {"results": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": _STR, "name": _STR, "tag": _STR, "accent": _STR,
                           "description": _STR},
        }}},
    },
    "setup_project": {
        "type": "object",
        "properties": {
            "project_id": _STR,
            "empty": {"type": "boolean"},
            "complete": {"type": "boolean"},
            "steps": {"type": "array", "items": {"type": "object"}},
            "note": _STR,
        },
    },
    "create_item": _ITEM_SCHEMA,
    "update_item": _ITEM_SCHEMA,
    "suggest_next": {  # stable {item: <item|null>} wrapper — never a bare null
        "type": "object",
        "properties": {"item": {**_ITEM_SCHEMA, "type": ["object", "null"]}},
    },
    "create_project": {
        "type": "object",
        "properties": {
            "id": _STR, "name": _STR, "tag": _STR, "description": _STR,
            "usable_by_this_key": {"type": "boolean"}, "confirm": _STR,
        },
    },
    "add_memory": _SHARD_SCHEMA,
    "publish_memory": {  # the VERDICT is the payload — the caller didn't decide this
        "type": "object",
        "properties": {
            "shard": _SHARD_SCHEMA,
            "verdict": {
                "type": "object",
                "properties": {
                    "kept": {"type": "boolean"},
                    "quality": {"type": "number"},
                    "reason": _STR,
                },
            },
        },
    },
    "reject_memory": _SHARD_SCHEMA,
    "search_memory": {
        "type": "object",
        "properties": {
            "results": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "id": _STR, "text": _STR, "scope": _STR, "score": {"type": "number"},
                    "item_id": _NULLABLE_STR, "source": _STR, "project_id": _NULLABLE_STR, "status": _STR,
                },
            }},
            "returned": {"type": "integer"},
            "top_k": {"type": "integer"},
        },
    },
    "get_item_details": {
        "type": "object",
        "properties": {
            "id": _STR, "title": _STR, "description": _STR,
            "status": {"type": "string", "enum": _STATUS_ENUM},
            "tags": {"type": "array", "items": _STR},
            "effort": {"type": "integer"}, "blocker": _STR,
            "pr": {"type": ["object", "null"]},
            "linked_shards": {"type": "array", "items": {"type": "object"}},
            "linked_requests": {"type": "array", "items": {"type": "object"}},
        },
    },
    "link_items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "a": _STR, "b": _STR, "type": _STR},
    },
    "unlink_items": {
        "type": "object",
        "properties": {"removed": {"type": "integer"}},
    },
    "extract_lessons": {
        "type": "object",
        "properties": {"results": {"type": "array", "items": {
            "type": "object", "properties": {"id": _STR, "text": _STR},
        }}},
    },
    "generate_digest": {
        "type": "object",
        "properties": {"digest": _STR},
    },
    "claim_next": {
        "type": "object",
        "properties": {"claimed": {"type": "boolean"}, "item": {"type": ["object", "null"]}},
    },
    "propose_allocation": {
        "type": "object",
        "properties": {
            "workers": {"type": "integer"}, "reviewers": {"type": "integer"},
            "mapping": {"type": "array"}, "rationale": {"type": "string"},
        },
    },
    "assign_role": {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string"}, "active_role": {"type": "string"},
            "pending": {"type": "boolean"},
        },
    },
    "collision_clusters": {
        "type": "object",
        "properties": {"clusters": {"type": "array"}, "total": {"type": "integer"}},
    },
    "claim_cluster": {
        "type": "object",
        "properties": {
            "claimed": {"type": "boolean"}, "items": {"type": "array"},
            "areas": {"type": "array"}, "predicted": {"type": "boolean"},
            "reason": {"type": "string"},
        },
    },
    "claim_review": {
        "type": "object",
        "properties": {
            "claimed": {"type": "boolean"}, "item": {"type": ["object", "null"]},
            "branch": {"type": "string"}, "worker_agent": {"type": ["string", "null"]},
            "reason": {"type": "string"},
        },
    },
    "sign_off": _ITEM_SCHEMA,
    "bounce": _ITEM_SCHEMA,
    "mint_enrolment": {
        "type": "object",
        "properties": {
            "enrolment_code": {"type": "string"}, "role": {"type": "string"},
            "seat_id": {"type": "string"}, "expires_at": {"type": "string"},
        },
    },
    "register_agent": {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string"}, "key": {"type": "string"},
            "active_role": {"type": "string"}, "eligible_roles": {"type": "array"},
            "heartbeat_interval_seconds": {"type": "integer"},
            "presence_ttl_seconds": {"type": "integer"},
        },
    },
    "retire_wave": {
        "type": "object",
        "properties": {
            "seats_revoked": {"type": "integer"}, "agents": {"type": "integer"},
            "leases_released": {"type": "integer"},
            "reservations_released": {"type": "integer"},
            # Never omitted. `{"seats_revoked": 4}` alone reads as "the wave is over",
            # which is the misreading that leaves four children building in the dark.
            "agents_still_running": {"type": "array"},
            "stopped_no_processes": {"type": "boolean"},
        },
    },
    "fleet_status": {
        "type": "object",
        "properties": {
            "agents": {"type": "array"}, "seats": {"type": "array"},
            "online": {"type": "integer"},
            "total": {"type": "integer"}, "roles": {"type": "array"},
            "presence_ttl_seconds": {"type": "integer"},
            "heartbeat_interval_seconds": {"type": "integer"},
        },
    },
    "heartbeat": _ITEM_SCHEMA,
    "release_item": _ITEM_SCHEMA,
    "related_work": {"type": "object", "properties": {"results": {"type": "array"}}},
    "next_cluster": {
        "type": "object",
        "properties": {"claimed": {"type": "integer"}, "cluster": {"type": "array"}},
    },
    "prd_coverage": {
        "type": "object",
        "properties": {
            "prd_id": _STR, "sections": {"type": "array"}, "gaps": {"type": "array"},
            "total_items": {"type": "integer"}, "done_items": {"type": "integer"},
            "percent_done": {"type": "integer"},
        },
    },
    "decompose_prd": {
        "type": "object",
        "properties": {"prd_id": _STR, "proposals": {"type": "array"}, "created": {"type": "array"}},
    },
    "create_prd": _PRD_SCHEMA_REF,
    "update_prd": _PRD_SCHEMA_REF,
    "answer_grill": {
        "type": "object",
        "properties": {
            "prd_id": _STR,
            "complete": {"type": "boolean"},
            "outstanding": {"type": "array", "items": _STR},
            "deferred": {"type": "array", "items": _STR},
            "answers": {"type": "integer"},
            # FALSE means this answer was NOT judged — the grader could not be asked — and
            # `outstanding` is therefore the previous round's. Without this an agent
            # relaying answers cannot tell a grader outage from a thin answer, and keeps
            # answering into a void (GRPH-485).
            "graded": {"type": "boolean"},
            "ungraded_reason": _STR,
        },
    },
    "grill_prd": {
        "type": "object",
        "properties": {"prd_id": _STR, "questions": _STR,
                       "retried": {"type": "boolean"}},
    },
    "describe_code": {
        "type": "object",
        "properties": {
            "nodes_upserted": {"type": "integer"},
            "edges_upserted": {"type": "integer"},
            "marked_stale": {"type": "integer"},
            "upserted_paths": {"type": "array", "items": _STR},
            "stale_paths": {"type": "array", "items": _STR},
        },
    },
    "get_code_map": {
        "type": "object",
        "properties": {
            "nodes": {"type": "array"}, "edges": {"type": "array"},
            "node_count": {"type": "integer"}, "edge_count": {"type": "integer"},
        },
    },
    "code_neighbors": {
        "type": "object",
        "properties": {
            "path": _STR, "node": {"type": ["object", "null"]},
            "outgoing": {"type": "array"}, "incoming": {"type": "array"},
            "items_touching": {"type": "array"},
            "linked_items": {"type": "array"}, "linked_requests": {"type": "array"},
        },
    },
    "search_code": {
        "type": "object",
        "properties": {
            "results": {"type": "array"}, "returned": {"type": "integer"}, "top_k": {"type": "integer"},
        },
    },
    "link_code": {
        "type": "object",
        "properties": {
            "id": {"type": "integer"}, "ref_type": _STR, "ref_id": _STR, "path": _STR, "relation": _STR,
        },
    },
    "unlink_code": {
        "type": "object",
        "properties": {"removed": {"type": "integer"}},
    },
    "report_graphban_issue": {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"}, "request_id": _NULLABLE_STR,
            "target": _STR, "duplicates": {"type": "array"},
        },
    },
}

#: The spec's own defaults for `ToolAnnotations` (MCP 2026-07-28). A field ABSENT from the
#: manifest means exactly this value, so emitting it says nothing a client did not already
#: know — and it is said 54 times.
_ANNOTATION_DEFAULTS = {"readOnlyHint": False, "destructiveHint": True,
                        "idempotentHint": False, "openWorldHint": True}


def _lean_annotations(hints: dict) -> dict:
    """Emit only the hints that DIFFER from the spec default (GRPH-48).

    Worth ~388 tokens off a manifest that had two to spare, and it is lossless by the
    spec's own definition rather than by our judgement: absent IS the default, so no
    client can distinguish the two, however naively it reads them.

    Deliberately NOT the wider trim available here. The spec also says `destructiveHint`
    and `idempotentHint` are "meaningful only when `readOnlyHint == false`", which would
    let us drop both from all 19 read-only tools for another ~238 tokens. That one is
    lossless only for a client that honours the conditional; a client reading the field
    anyway would flip from our `true` to the default `false`. The extra 238 tokens are not
    worth a behaviour that depends on how carefully somebody else read the spec.
    """
    return {k: v for k, v in hints.items() if _ANNOTATION_DEFAULTS[k] != v}


for _t in TOOLS:
    _name = _t["name"]
    props = _t["inputSchema"].setdefault("properties", {})
    if _name in _TAKES_PROJECT:
        props["project_id"] = {
            "type": "string",
            "description": "Overrides the key's default project.",
        }
    if _name in _IDEMPOTENT_CREATES:
        props["idempotency_key"] = {
            "type": "string",
            "description": "Opaque token; a repeat call with the same key returns the original resource.",
        }
    if _name in _LEAN_LIST:
        props["fields"] = {
            "type": "string",
            "enum": ["lean", "full"],
            "description": "`lean` (default) id/title/status; `full` every item field. The reply's `fields` lists what a row carries — one absent there is unreported, not empty.",
        }
    if _name in _PAGED:
        props["limit"] = {"type": "integer", "description": "Max results (default 25)."}
        props["offset"] = {"type": "integer", "description": "Results to skip for paging (default 0)."}
        _t["outputSchema"] = _PAGE_META
    elif _name in _OUTPUT_SCHEMAS:
        _t["outputSchema"] = _OUTPUT_SCHEMAS[_name]
    # MCP annotations so an agent can reason about safety (#7).
    #
    # Computed HERE and only here. Seven tools used to carry a hand-written `annotations`
    # block in the literal above, which this loop then overwrote — six agreed with what it
    # computes and were merely noise, but `review_recommendation` declared
    # `idempotentHint: true` and shipped `false`, so the manifest advertised the opposite of
    # what the author wrote and nothing said so. One source of truth instead, with the
    # disagreement resolved in the data: it sets a status and commits, so a second identical
    # call changes nothing further and the hand-written value was the correct one.
    _ro = _name in _READ_ONLY
    _t["annotations"] = _lean_annotations({
        "readOnlyHint": _ro,
        "destructiveHint": _name == "update_item",
        # read-only + update_item are naturally idempotent; creates become idempotent with a key.
        "idempotentHint": _ro or _name in ({"update_item"} | _IDEMPOTENT_CREATES | _IDEMPOTENT_WRITES),
        "openWorldHint": False,
    })

LIVE_TOOL_COUNT = len(TOOLS)
_SCHEMA_BY_NAME: dict[str, dict] = {t["name"]: t["inputSchema"] for t in TOOLS}


def _visible_tools(key: ApiKey, role: str | None = None) -> list[dict]:
    """The manifest a given key should see, gated by SCOPE and then by ROLE.

    Scope (AL-78): a key without `write` gets a `Forbidden` on every mutating tool, so
    shipping it those schemas is pure token cost — and misleading. That roughly halves the
    manifest for a read-only key and keeps `tools/list` honest: you only see what you can
    call.

    Role (PRD-17 D-b): a key whose `roles` name a single role never gets to call the other
    roles' tools either, so those are dead weight in exactly the same way. A reviewer
    credential carries no `claim_next`; a worker credential carries no `sign_off`.

    **This gates on the KEY's ceiling, not on the agent's ACTIVE role, and the distinction is
    the whole reason it is safe without SSE.** PRD-17 rules out trimming per active role, and
    correctly: `tools/list` is fetched once at client connect, before `register_agent` has
    run, and this endpoint has no channel to push `notifications/tools/list_changed` when a
    role is later assigned. But a key's eligible roles are fixed at mint and cannot change
    under a live connection — so gating on them is static, needs no push, and is what D-b
    actually prescribes ("the manifest advertises the union of the key's eligible roles").

    The call gate stays the enforcement point regardless. A manifest can only fail to mention
    a tool; the gate refuses it. This is a token optimisation, never a security boundary —
    which is why an unregistered agent on a full-ceiling key still sees, and may call,
    everything.
    """
    from app.services import fleet as fleet_svc

    tools = TOOLS if "write" in (key.scopes or []) else [
        t for t in TOOLS if t["name"] in _READ_ONLY]
    allowed = set(fleet_svc.eligible_roles(key))
    # E9b: the SESSION's role, when this connection carries exactly one registered agent, is
    # narrower than the credential and is what the agent will actually be judged by. It never
    # widens — an intersection, so a worker seat on a reviewer-only key still sees neither
    # role's extra tools rather than gaining the worker's.
    if role and role != fleet_svc.ALL_IN_ONE:
        allowed = allowed & {role}
    if allowed >= set(fleet_svc.ROLES):
        return tools           # unrestricted credential — the pre-PRD-17 manifest, unchanged
    return [t for t in tools
            if not (req := fleet_svc.TOOL_ROLES.get(t["name"])) or allowed.intersection(req)]

# JSON-schema primitive -> (python type, label). bool is excluded from int on purpose.
_JSON_TYPES: dict[str, tuple[type | tuple[type, ...], str]] = {
    "string": (str, "a string"),
    "integer": (int, "an integer"),
    "array": (list, "an array"),
    "boolean": (bool, "a boolean"),
    "object": (dict, "an object"),
}


def _validate_args(name: str, args: dict[str, Any]) -> None:
    """Check args against the tool's declared inputSchema BEFORE dispatch, so a
    bad call becomes an actionable `validation` error instead of a KeyError or a
    silently-accepted junk value (AL-47). Required fields, enums, and primitive
    types only — deliberately lightweight, no external validator dependency."""
    schema = _SCHEMA_BY_NAME.get(name)
    if schema is None:
        return  # unknown tool handled by the dispatcher
    props: dict = schema.get("properties", {})
    required: list = schema.get("required", [])

    missing = [f for f in required if args.get(f) in (None, "")]
    if missing:
        raise errors.Validation(
            f"{name!r} is missing required argument{'s' if len(missing) > 1 else ''}: "
            f"{', '.join(missing)}",
            hint=f"required: {', '.join(required)}",
        )

    for field, value in args.items():
        spec = props.get(field)
        if not spec or value is None:
            continue  # unknown extras are ignored; None means "absent"
        enum = spec.get("enum")
        if enum is not None and value not in enum:
            raise errors.Validation(
                f"invalid {field}: {value!r}",
                hint=f"allowed values: {', '.join(map(str, enum))}",
            )
        expected = spec.get("type")
        if expected in _JSON_TYPES:
            py_type, label = _JSON_TYPES[expected]
            ok = isinstance(value, py_type) and not (expected == "integer" and isinstance(value, bool))
            if not ok:
                raise errors.Validation(
                    f"{field} must be {label}", hint=f"got {type(value).__name__}"
                )


def _item_dict(item) -> dict:
    # `key`/`prd_key` render from the project's CURRENT tag; the stored id is frozen and
    # internal, and an agent that quotes a rendered key back is resolved by services/keys
    # (PRD-13). Emitting the stored id here would leak a retired tag straight into agent
    # memory, where it would outlive the rename by months.
    out = {
        "id": item.key,
        "project_id": item.project_id,
        "title": item.title,
        "status": item.status,
        "tags": item.tags,
        "touchpoints": item.touchpoints or [],
        "effort": item.effort,
        "assignee": item.assignee,
        "claimed_by": item.claimed_by,
        "prd_id": item.prd_key,
        "prd_section": item.prd_section,
        "fidelity": item.fidelity,
        "evidence": item.evidence or [],
        # Authorship, distinct from the lease above (GRPH-379). It is the input the review
        # independence rule is decided on, and it was readable nowhere — so an agent could not
        # tell whose work it was about to review, nor explain a refusal it received.
        "built_by": item.built_by,
        "reviewed_by": item.reviewed_by,
    }
    out.update(items_svc.bounce_fields(item))
    # In-flight invalidation (GRPH-242/312). Present only when this item's PRD rebaselined
    # after work on it started — so it costs nothing on the overwhelming majority of reads
    # and is impossible to miss on the ones that matter. Delivered here rather than on the
    # claim path because an agent can complete an item without ever claiming it, and that
    # was the hole: its work then gets classified against intent it never saw move.
    from sqlalchemy.orm import object_session

    from app.services import prds as prd_svc

    # Same degradation as `models._key_of`: a detached object has no session to ask, and
    # serialization must not raise over a field that is absent on nearly every row.
    session = object_session(item)
    hold = prd_svc.intent_hold(session, item) if session is not None else None
    if hold:
        out["intent_hold"] = hold
    return out


def _readable_prd(db, prd_id: str, readable):
    prd = prd_svc.get_prd(db, prd_id)
    if prd is None:
        raise errors.NotFound(f"prd not found: {prd_id}")
    if prd.project_id not in readable:
        raise authz.Forbidden(f"prd {prd_id!r} is outside this key's project scope")
    return prd


def _writable_prd(db, prd_id: str, allowed):
    """Scope-gated like every other write tool. Checked BEFORE the service call, so a key
    without write scope cannot learn whether a PRD exists by reading the error."""
    prd = prd_svc.get_prd(db, prd_id)
    if prd is None:
        raise errors.NotFound(f"prd not found: {prd_id}")
    if prd.project_id not in allowed:
        raise authz.Forbidden(f"prd {prd_id!r} is outside this key's write scope")
    return prd


def _verdict_dicts(rows) -> list[dict]:
    return [
        {"id": v.id, "outcome": v.outcome, "reasoning": v.reasoning,
         "citations": v.citations or [], "signed_by": v.signed_by,
         "baseline_version": v.baseline_version, "self_signed": v.self_signed,
         "separation": v.separation, "self_signed_items": v.self_signed_items or []}
        for v in rows
    ]


def _ref_key(db, stored_id: str) -> str:
    """Render a link endpoint, which may be an item OR a request (`links.a`/`b` are
    untyped strings). Falls back to the stored id so a dangling edge still serializes."""
    for kind in ("item", "request"):
        row = db.get(keys.MODELS[kind], stored_id)
        if row is not None:
            return row.key
    return stored_id


#: What the lean projection carries, named so a caller can tell a field this payload does
#: not report from one the item does not have (GRPH-440).
LEAN_FIELDS = ("id", "title", "status")


def _lean_item(item) -> dict:
    """The scanning shape: just enough to recognize an item and decide whether to
    open it. List reads (search_items, get_backlog) return this by default and the
    fat fields (touchpoints, assignee, claimed_by, prd_*, fidelity, effort) only on
    `fields="full"` — an agent picking work calls get_item_details once it chooses,
    so paying for 12 fields × N rows on every scan is waste (AL-78).

    The compact payload is defensible. What was not is that it looked COMPLETE: a consumer
    asking a row for `built_by` got nothing back, and in every client language absent arrives
    as null — `.get()` in Python, `undefined` in TS. So "nobody built this" and "this payload
    does not say" were the same answer, on the exact field a reviewer consults to decide what
    it may take. It was misread twice in one day from two different tools, minutes after
    `update_item` had returned the author for the same items (GRPH-440).

    Fixed on the ENVELOPE rather than the row: `_paginate` names the projection once per page,
    so the cost is one short array per response instead of four more nulls per row.
    """
    return {"id": item.key, "title": item.title, "status": item.status}


def _full(args: dict) -> bool:
    return args.get("fields") == "full"


def _shard_dict(shard) -> dict:
    return {
        "id": shard.id, "text": shard.text, "scope": shard.scope,
        "item_id": shard.item_key, "project_id": shard.project_id,
        "status": shard.status,
        # Auto-triage outcome (AL-227): if the scorer acted on write, the agent sees
        # the final status here plus how it was decided.
        "scoring_source": shard.scoring_source,
        "auto_confidence": shard.auto_confidence,
    }


def _prd_dict(prd) -> dict:
    return {
        "id": prd.key,
        "project_id": prd.project_id,
        "title": prd.title,
        "status": prd.status,
        "version": prd.version,
        "sections": prd_svc.parse_sections(prd.body),  # the `## ` headings, in order
        "linked": prd.linked_keys,
        "body": prd.body,
    }


def _paginate(rows: list, args: dict, *, fields: tuple[str, ...] | None = None) -> dict:
    """Slice a full result list to a page and report totals (#9).

    `fields`, when given, names what each row carries (GRPH-440). A projection that does not
    say what it left out is indistinguishable from a complete one, and the consumer's only
    recourse is to guess — which is how `built_by: null` on a lean row was read as "nobody
    built this" rather than "this payload does not report authorship".
    """
    limit = int(args.get("limit", 25))
    offset = int(args.get("offset", 0))
    total = len(rows)
    page = rows[offset : offset + limit]
    out = {
        "results": page,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < total,
    }
    if fields is not None:
        out["fields"] = list(fields)
    return out


def _idempotent_get(db: Session, args: dict, tool: str, model) -> Any | None:
    """If the call carries an idempotency_key already seen, return the original
    resource. A key remembered for a DIFFERENT tool is a conflict, not a silent
    duplicate (AL-47) — the agent reused a token across logical operations."""
    prior = idem_svc.lookup(db, args.get("idempotency_key") or "")
    if prior is None:
        return None
    if prior.tool != tool:
        raise errors.Conflict(
            f"idempotency_key was already used for {prior.tool!r}, not {tool!r}",
            hint="use a fresh idempotency_key for each distinct create",
        )
    return db.get(model, prior.resource_id)


def _idempotent_remember(db: Session, args: dict, tool: str, resource_id: str) -> None:
    idem_svc.remember(db, args.get("idempotency_key") or "", tool, resource_id)


def _scoped_item(db: Session, item_id: str, scope_ids: list[str]) -> Item:
    """Load an item and require it inside the key's project scope. The refusal
    deliberately does not reveal which project an off-scope item belongs to."""
    item = db.get(Item, keys.resolve_item(db, item_id) or item_id)
    if item is None:
        raise errors.NotFound(f"item not found: {item_id}", hint="use search_items to find a valid id")
    if item.project_id not in scope_ids:
        raise authz.Forbidden(f"item {item_id!r} is outside this key's project scope")
    return item


# Retired tool names, kept dispatchable forever. NOT in TOOLS — an alias must not appear
# in tools/list or inflate the counts asserted by tests/test_api.py and test_phase4.py.
# Agents cache tool names in memory and in committed configs, so a name that ever worked
# has to keep working (AL-262).
TOOL_ALIASES = {"report_agentledger_issue": "report_graphban_issue"}


def _call_tool(db: Session, name: str, args: dict[str, Any], key: ApiKey,
               session_id: str | None = None, defer=None) -> Any:
    name = TOOL_ALIASES.get(name, name)
    # Authority: a key's declared scopes ∩ its owner's memberships bound every call
    # (a key never out-ranks the user who minted it). `project_id` args can select
    # among in-scope projects but can no longer escape the scope.
    writes = name not in _READ_ONLY
    if writes and "write" not in (key.scopes or []):
        raise authz.Forbidden(
            f"api key {key.name!r} has scopes {key.scopes} but {name!r} mutates state; "
            "mint a key with the 'write' scope or use a read-only tool"
        )
    readable = authz.key_readable_ids(db, key)
    allowed = authz.key_writable_ids(db, key) if writes else readable
    requested = args.get("project_id")
    if requested and requested not in allowed:
        raise authz.Forbidden(
            f"project {requested!r} is outside this key's {'write' if writes else 'read'} scope "
            f"(in scope: {', '.join(allowed) or 'none'})"
        )
    pid = (
        requested
        or (key.project_id if key.project_id in allowed else None)
        or (allowed[0] if allowed else None)
    )

    # Role gate (PRD-17 D2). AFTER scope, because "this key cannot reach that project" is a
    # more fundamental refusal than "this role cannot make that call", and reporting the role
    # first would tell an off-scope caller which tools exist inside a project it cannot see.
    #
    # Enforced here rather than by trimming the manifest: `tools/list` is fetched once at
    # client connect, before `register_agent` has run, and this endpoint has no SSE channel to
    # push a changed manifest when a role is later assigned.
    _agent_id = args.get("agent_id") or None
    try:
        fleet_svc.check_tool_role(db, tool=name, api_key=key, agent_id=_agent_id, args=args)
    except authz.Forbidden as refusal:
        # Refusals are audited with the agent AND the human principal behind the key, both
        # stamped server-side — a compromised client still produces a correctly attributed
        # trail, because none of it comes from anything the client sent.
        count = fleet_svc.record_refusal(db, agent_id=_agent_id)
        meta = {"tool": name, "reason": str(refusal), "agent_id": _agent_id,
                "consecutive_refusals": count}
        if _agent_id and count >= fleet_svc.QUARANTINE_AFTER_REFUSALS:
            meta["quarantine"] = fleet_svc.quarantine(db, _agent_id)
        events_svc.record_key(db, key, action="role_refused", target_type="agent",
                              target_id=_agent_id or "", project_id=pid, meta=meta)
        raise
    # Consecutive is the property that matters: three refusals spread across a productive hour
    # is one stale code path, not an agent that has stopped listening.
    fleet_svc.clear_refusals(db, _agent_id)
    if pid is None and name in _PROJECT_SCOPED:
        raise authz.Forbidden(
            f"no project in scope for {name!r}: the key's owner has no "
            f"{'write-access ' if writes else ''}project memberships; "
            "ask a project owner to grant access"
        )

    # Rate/quota gates (hosted only; attributed to the org owning the target project;
    # calls with no project in scope are exempt). Burst cap first — it's the cheap
    # check that protects the monthly counter's DB write under a flood — then meter the
    # call against the org's monthly plan allowance.
    _org_id = quotas.org_id_for_project(db, pid)
    quotas.enforce_org_rate(_org_id)
    quotas.meter_call(db, _org_id)

    if name == "get_context":
        proj = db.get(Project, pid) if pid else None
        return {
            "project_id": pid,
            "project_name": proj.name if proj else None,
            "key_project_id": key.project_id,  # None => global key; agent should pass project_id
            "scopes": key.scopes,
            "readable_projects": readable,
            "writable_projects": authz.key_writable_ids(db, key),
            "project_count": db.scalar(select(func.count()).select_from(Project)),
            # The count the agent can actually call with this key — matches the
            # scope-gated manifest it received, not the server-wide total (AL-78).
            "tool_count": len(_visible_tools(key)),
            # First-run signal (AL-133): an empty project → call setup_project for a bootstrap.
            "empty": setup_svc.is_empty(db, pid) if pid else False,
        }
    if name == "create_project":
        # An AUTHORITY gate, narrowly opened (PRD-14 D4). Both refusals below are the
        # same rule: a project may only be conjured where doing so can't reach anyone
        # else's tenant. `link_status` resolves the DB link then the env link, so a box
        # linked from the UI, from env, OR from the CLI (AL-281) all answer truthfully —
        # before AL-281 a CLI-linked instance reported unlinked and this would have
        # FAILED OPEN on exactly the instances the gate exists for.
        from app.services import code_sync

        if settings.hosted_mode:
            raise authz.Forbidden(
                "projects are created by an operator in hosted mode, not by an agent; "
                "ask an org owner to create it"
            )
        link = code_sync.link_status(db)
        if link["linked"]:
            raise authz.Forbidden(
                f"this instance is linked to a cloud org ({link['cloud_url']}, source: "
                f"{link['source']}), so a project created here would reach that org's "
                "tenant space and consume its quota; an org owner creates it"
            )
        try:
            project = projects_svc.create_project(
                db, name=args["name"], owner_user_id=key.user_id,
                tag=args.get("tag"), description=args.get("description", ""),
            )
        except ValueError as e:
            raise errors.Validation(str(e)) from e
        events_svc.record(
            db, actor_type="agent", actor_label=f"agent:{key.name or key.id}", surface="mcp",
            action="create_project", target_type="project", target_id=project.id,
            project_id=project.id, meta={"name": project.name, "tag": project.tag},
        )
        return {
            "id": project.id, "name": project.name, "tag": project.tag,
            "description": project.description,
            # A key PINNED to another project still can't write here — it was minted for
            # one project and creating a second doesn't widen it. Say so, or the next
            # call 403s and reads like a bug.
            "usable_by_this_key": key.project_id is None,
            "confirm": "ask a human to confirm this is the right workspace before "
                       "bootstrapping into it",
        }
    if name == "setup_project":
        return setup_svc.checklist(db, pid)
    if name == "list_projects":
        return {"results": [
            {"id": p.id, "name": p.name, "tag": p.tag, "accent": p.accent,
             "description": p.description}
            for p in db.scalars(select(Project).order_by(Project.name)).all()
            if p.id in readable
        ]}
    if name == "create_item":
        cached = _idempotent_get(db, args, "create_item", Item)
        if cached is not None:
            return _item_dict(cached)
        item = items_svc.create_item(
            db,
            title=args["title"],
            description=args.get("description", ""),
            tags=args.get("tags", []),
            effort=args.get("effort", 0),
            status=args.get("status", "backlog"),
            fidelity=args.get("fidelity", "low"),
            project_id=pid,
            touchpoints=args.get("touchpoints"),
            prd_id=args.get("prd_id"),
            prd_section=args.get("prd_section", ""),
            reporter={"name": "Agent", "handle": "mcp", "avatar": "#a78bfa"},
        )
        _idempotent_remember(db, args, "create_item", item.id)
        return _item_dict(item)
    if name == "update_item":
        _scoped_item(db, args["id"], allowed)
        item = items_svc.update_item(
            db,
            args["id"],
            # The judge and the lesson extractor are MODEL calls, and an agent completing an
            # item is single-threaded: waiting on them stops its heartbeat (GRPH-399).
            defer=defer,
            status=args.get("status"),
            title=args.get("title"),
            description=args.get("description"),
            tags=args.get("tags"),
            effort=args.get("effort"),
            blocker=args.get("blocker"),
            fidelity=args.get("fidelity"),
            touchpoints=args.get("touchpoints"),
            prd_id=args.get("prd_id"),
            prd_section=args.get("prd_section"),
            ack_section_drift=args.get("ack_section_drift"),
            evidence=args.get("evidence"),
        )
        if item is None:
            raise errors.NotFound(f"item not found: {args['id']}")
        return _item_dict(item)
    if name == "search_items":
        rows = items_svc.search_items(
            db, args.get("query", ""), status=args.get("status"), project_id=pid,
            tags=args.get("tags"), limit=10_000,
        )
        lean = not _full(args)
        shape = _lean_item if lean else _item_dict
        return _paginate([shape(i) for i in rows], args,
                         fields=LEAN_FIELDS if lean else None)
    if name == "add_memory":
        cached = _idempotent_get(db, args, "add_memory", MemoryShard)
        if cached is not None:
            return _shard_dict(cached)
        if args.get("item_id"):
            _scoped_item(db, args["item_id"], allowed)
        quotas.enforce_shard_quota(db, quotas.org_id_for_project(db, pid))
        # Agent-written memory enters as a CANDIDATE — it reaches the trusted
        # retrieval path only after a human publishes it (AL-49).
        shard = mem_svc.add_memory(
            db,
            text_body=args["text"],
            scope=args.get("scope", "global"),
            item_id=args.get("item_id"),
            project_id=pid,
            status="candidate",
            origin=f"agent:{key.name or key.id}",
        )
        _idempotent_remember(db, args, "add_memory", shard.id)
        return _shard_dict(shard)
    if name in ("publish_memory", "reject_memory"):
        shard = db.get(MemoryShard, args["shard_id"])
        if shard is None or shard.project_id not in allowed:
            # Same message either way: whether a shard exists in a project you can't
            # write to is not something a key should be able to probe.
            raise errors.NotFound(f"shard not found: {args['shard_id']}")
        if not mem_svc.agent_adjudication_enabled(db, shard.project_id):
            raise authz.Forbidden(
                f"project {shard.project_id!r} does not allow agents to adjudicate memory; "
                "a human publishes candidates from Memory review, or an owner can enable "
                "agent adjudication in project settings"
            )
        if shard.status != "candidate":
            raise errors.Conflict(
                f"shard {shard.id} is already {shard.status}; only a candidate is adjudicated"
            )
        origin = f"agent:{key.name or key.id}"
        if name == "reject_memory":
            return _shard_dict(mem_svc.agent_reject(db, shard, origin=origin))
        try:
            shard, verdict = mem_svc.agent_publish(db, shard, origin=origin)
        except mem_svc.AdjudicationUnavailable as e:
            # Degrade to the human boundary; never fall through to publishing.
            raise errors.Unavailable(
                str(e),
                hint="the shard is unchanged and still a candidate; ask an operator to "
                     "configure a chat provider, or leave it for human review",
            ) from e
        return {
            "shard": _shard_dict(shard),
            "verdict": {"kept": shard.status == "published",
                        "quality": verdict["quality"], "reason": verdict.get("reason", "")},
        }
    if name == "search_memory":
        top_k = args.get("top_k", 5)
        hits = mem_svc.search_memory(
            db, args["query"], top_k=top_k, project_id=pid,
            include_candidates=bool(args.get("include_candidates", False)),
        )
        results = [
            {
                "id": s.id, "text": s.text, "scope": s.scope, "score": round(score, 4),
                "item_id": s.item_key, "source": s.source, "project_id": s.project_id,
                "status": s.status,
            }
            for s, score in hits
        ]
        return {"results": results, "returned": len(results), "top_k": top_k}
    if name == "get_backlog":
        ranked = prio_svc.prioritized(db, pid, statuses=("backlog", "next"), include_blocked=True)
        # The ranking signal (ready/blocked_by/unblocks/votes/score) is the reason to
        # call get_backlog, so it stays; only the fat item fields are opt-in (AL-78).
        lean = not _full(args)
        shape = _lean_item if lean else _item_dict
        ranking = ("ready", "blocked_by", "unblocks", "votes", "score")
        rows = [
            {**shape(r["item"]), "ready": r["ready"],
             "blocked_by": [_ref_key(db, d) for d in r["blocked_by"]],
             "unblocks": r["unblocks"], "votes": r["votes"], "score": r["score"]}
            for r in ranked
        ]
        # The ranking fields are on every row whichever projection is in play, so they belong
        # in the declaration too — a caller checking `fields` must see what it will actually
        # get, not what the item shape alone carries.
        return _paginate(rows, args,
                         fields=(LEAN_FIELDS + ranking) if lean else None)
    if name == "get_item_details":
        _scoped_item(db, args["id"], readable)
        details = items_svc.get_item_details(db, args["id"])
        if details is None:
            raise errors.NotFound(f"item not found: {args['id']}")
        return details
    if name == "suggest_next":
        item = items_svc.suggest_next(db, project_id=pid)
        # Stable shape whether or not the backlog has a candidate (parallels
        # claim_next's {claimed, item}) — never a bare null (AL-47).
        return {"item": _item_dict(item) if item else None}
    if name == "related_work":
        item = _scoped_item(db, args["id"], readable)
        rel = cluster_svc.related_items(db, item, item.project_id)
        return {"results": [
            {**_item_dict(r["item"]), "score": r["score"], "shared": r["shared"], "link_types": r["link_types"]}
            for r in rel
        ]}
    if name == "next_cluster":
        agent = fleet_svc.caller_identity(args.get("agent_id"), key)
        batch = cluster_svc.next_cluster(db, agent, project_id=pid, max_items=args.get("max_items", 3))
        return {"claimed": len(batch), "cluster": [
            {**_item_dict(b["item"]), "seed": b["seed"], "shared": b["shared"], "link_types": b["link_types"]}
            for b in batch
        ]}
    if name == "link_items":
        cached = _idempotent_get(db, args, "link_items", Link)
        if cached is not None:
            return {"id": cached.id, "a": cached.a, "b": cached.b, "type": cached.type}
        # Both endpoints must exist and be in scope — also stops dangling links
        # from poisoning get_backlog's blocked_by.
        _scoped_item(db, args["a"], allowed)
        _scoped_item(db, args["b"], allowed)
        link = links_svc.create_link(
            db, a=args["a"], b=args["b"], type_=args.get("type", "dependency"),
            reason=args.get("reason", ""), project_id=pid,
        )
        _idempotent_remember(db, args, "link_items", link.id)
        return {"id": link.id, "a": _ref_key(db, link.a), "b": _ref_key(db, link.b),
                "type": link.type}
    if name == "unlink_items":
        # Both endpoints must be in scope — same guard as link_items, so a key can't
        # probe or mutate links that touch items outside its project scope.
        _scoped_item(db, args["a"], allowed)
        _scoped_item(db, args["b"], allowed)
        removed = links_svc.delete_link(
            db, a=args["a"], b=args["b"], type_=args.get("type"), project_id=pid,
        )
        return {"removed": removed}
    if name == "claim_next":
        agent = fleet_svc.caller_identity(args.get("agent_id"), key)
        # A caller already holding something gets a refusal it can act on rather than a second
        # item to abandon (GRPH-504). Conflict, not internal: the call was well-formed and the
        # remedy is a verb the agent already has.
        held = items_svc.live_claim(
            db, agent, lease_seconds=args.get("lease_seconds", items_svc.DEFAULT_LEASE_SECONDS))
        if held is not None:
            raise errors.Conflict(
                f"you already hold {held.key} — one worker, one worktree (PRD-17 D-g)",
                hint=f"release_item({held.key}) if you are not working it, then claim again",
            )
        item = fleet_svc.park(
            db,
            lambda s: items_svc.claim_next(
                s, agent, project_id=pid,
                lease_seconds=args.get("lease_seconds", items_svc.DEFAULT_LEASE_SECONDS),
                skip=args.get("skip")),
            agent_id=args.get("agent_id"), wait_seconds=args.get("wait_seconds"),
        )
        out = {"claimed": item is not None, "item": _item_dict(item) if item else None}
        if item is None:
            # "Nothing for you" and "nothing at all" were the same response (GRPH-379). They
            # call for opposite behaviour — wait and retry, or stop asking — so an agent that
            # cannot tell them apart is guessing at the one decision this response drives.
            if reserved := items_svc.reserved_elsewhere(db, agent, project_id=pid):
                out["reserved"] = reserved
        return out
    if name == "propose_allocation":
        return fleet_svc.propose_allocation(db, pid)
    if name == "assign_role":
        try:
            # `agent_id` is the CALLER everywhere on this surface — the role gate reads it to
            # decide what the caller may do. Naming the target with the same key would have a
            # planner judged by the role of the agent it is re-tasking, and refused for it.
            agent = fleet_svc.assign_role(
                db, agent_id=args["target_agent_id"], role=args["role"],
                reason=args.get("reason", ""))
        except ValueError as e:
            raise errors.Validation(str(e))
        return {"agent_id": agent.id, "active_role": agent.active_role,
                # Issued, not yet collected. The Fleet view renders that distinction, and an
                # agent that never polls again should look pending rather than done.
                "pending": fleet_svc.pending_directive(agent) is not None}
    if name == "collision_clusters":
        from app.services import collision as collision_svc

        clusters = collision_svc.clusters_for_project(db, pid, args.get("status"))
        # Rendered keys, not stored ids — an agent quotes these back and `services/keys`
        # resolves them (PRD-13). The service layer works in stored ids because that is what
        # is frozen; the boundary is where they become the tag-rendered form.
        out = []
        for c in clusters:
            rows = [db.get(Item, i) for i in c.get("items") or []]
            out.append({**c, "items": [r.key for r in rows if r is not None]})
        return {"clusters": out, "total": len(out)}
    if name == "claim_cluster":
        agent = fleet_svc.caller_identity(args.get("agent_id"), key)
        # The last refusal, kept so it can be RETURNED. The park needs a falsy answer to keep
        # waiting, and translating the miss to None threw away the only thing the caller could
        # act on — the service names who holds the areas and when they free, and this handler
        # replaced all of it with a fixed string. Found by asserting through this surface
        # rather than against the service, which is how the `update_item` gate hid too.
        miss: dict = {}

        def _attempt(s):
            out = fleet_svc.claim_cluster(
                s, agent_id=agent, project_id=pid, max_items=args.get("max_items", 3),
                lease_seconds=args.get("lease_seconds", items_svc.DEFAULT_LEASE_SECONDS),
            )
            if out["claimed"]:
                return out
            miss["last"] = out
            return None

        got = fleet_svc.park(db, _attempt, agent_id=args.get("agent_id"),
                             wait_seconds=args.get("wait_seconds"))
        return got or miss.get("last") or {
            "claimed": False, "items": [], "areas": [], "predicted": False, "held_by": [],
            "reason": "nothing ready to claim"}
    if name == "claim_review":
        agent = fleet_svc.caller_identity(args.get("agent_id"), key)
        item = fleet_svc.park(
            db, lambda s: fleet_svc.claim_review(s, agent_id=agent, project_id=pid,
                                                 skip=args.get("skip")),
            agent_id=args.get("agent_id"), wait_seconds=args.get("wait_seconds"))
        if item is None:
            # Not an error. With one agent in the fleet this is the CORRECT answer, and
            # phrasing it as a failure would send a solo agent hunting for a bug.
            # The reason matters more than the refusal: "nothing waiting" and "waiting, but
            # you are not independent of it" send an operator to opposite places.
            return {"claimed": False, "item": None, "branch": "", "worker_agent": None,
                    "reason": fleet_svc.review_block_reason(
                        db, agent_id=args.get("agent_id") or agent, project_id=pid)}
        return {"claimed": True, "item": _item_dict(item), "branch": item.branch or "",
                "worker_agent": item.claimed_by, "reason": ""}
    if name == "sign_off":
        _scoped_item(db, args["id"], allowed)
        agent = fleet_svc.caller_identity(args.get("agent_id"), key)
        try:
            item = fleet_svc.sign_off(
                db, item_id=keys.resolve_item(db, args["id"]) or args["id"],
                agent_id=agent, evidence=args.get("evidence"), api_key=key)
        except fleet_svc.NotInReview as e:
            # Conflict, not unauthorized, for the same reason the evidence gate below is: the
            # caller is permitted to sign this off, the work simply has not been handed over.
            raise errors.Conflict(str(e), hint=(
                "wait for the agent working it to call update_item(status='review'), or take "
                "other work with claim_review"))
        except fleet_svc.SelfReview as e:
            raise authz.Forbidden(
                str(e), hint="another agent takes it from review; call fleet_status to see who")
        except fleet_svc.MissingAdversarialEvidence as e:
            # Audited on the REFUSAL path, because the dispatcher only audits successes — and a
            # gate whose refusals leave no trace cannot be examined for whether it is being
            # routed around, which is the exact failure that kept GRPH-321 parked.
            events_svc.record_key(
                db, key, action="sign_off_refused", target_type="item",
                target_id=args.get("id", ""), project_id=pid,
                meta={"reason": str(e), "agent_id": args.get("agent_id")})
            # Conflict, not unauthorized: the caller IS permitted to sign this off — the work
            # simply is not accounted for yet. `unauthorized` would send a reviewer hunting a
            # permissions problem it does not have.
            raise errors.Conflict(str(e), hint=(
                "dispatch two opposing-lens critics, or run the passes yourself, and record "
                "each as evidence {kind: sabotage, claim, mutation, tests_failed}"))
        out = _item_dict(item)
        if fleet_svc.is_credential(agent):
            # Say it in the response, not only in the column. A caller that never registered
            # has no way to notice that the verdict it just recorded is attributed to a key
            # rather than to it — and four items were signed off that way before anybody did.
            out["signed_by"] = agent
            out["attribution"] = (
                "recorded against this CREDENTIAL, not an agent — no agent id was passed. "
                "Call register_agent and pass agent_id so the verdict names who made it")
        return out
    if name == "bounce":
        _scoped_item(db, args["id"], allowed)
        agent = fleet_svc.caller_identity(args.get("agent_id"), key)
        try:
            item = fleet_svc.bounce(
                db, item_id=keys.resolve_item(db, args["id"]) or args["id"],
                agent_id=agent, reason=args["reason"])
        except fleet_svc.NotInReview as e:
            raise errors.Conflict(str(e), hint=(
                "a bounce is a review verdict; an item still being worked on is released with "
                "release_item, not bounced"))
        except ValueError as e:
            raise errors.Validation(str(e))
        return _item_dict(item)
    if name == "register_agent":
        try:
            agent = fleet_svc.register_agent(
                db, project_id=pid, api_key=key, label=args.get("label", ""),
                capabilities=args.get("capabilities") or {}, worktree=args.get("worktree", ""),
                branch=args.get("branch", ""), role_hint=args.get("role_hint"),
                parent_agent_id=args.get("parent_agent_id"),
                enrolment_code=args.get("enrolment_code"),
            )
        except fleet_svc.EnrolmentError as e:
            # `unauthorized`, not `validation`: the code was understood and refused. An agent
            # that already branches on refusals needs no new case.
            raise authz.Forbidden(str(e), hint="ask for a seat in the Fleet view, or register "
                                               "without one to work as all-in-one")
        # E9a: remember which connection this agent arrived on, so a later `tools/list` over
        # the same one can be answered for THIS agent instead of for the shared credential.
        # Written after the refusal path above, because an agent that was refused does not
        # exist to bind.
        if session_id:
            agent.mcp_session_id = session_id
            db.commit()
        return {
            "agent_id": agent.id, "key": agent.key, "active_role": agent.active_role,
            "eligible_roles": list(fleet_svc.eligible_roles(key)),
            # Stated rather than inferred from `active_role`: `all-in-one` is BOTH the granted
            # seat role and what an un-enrolled agent gets, so a client cannot tell the
            # deliberate case from the forgotten one without being told (PRD-19 A2).
            "enrolled": agent.enrolment_id is not None,
            # The boundary, stated once, at the moment the role is granted. The MANIFEST cannot
            # say this: `tools/list` is fetched at connect, before any role exists, so a fleet
            # agent holds the full list all session and finds the edge by walking into it — and
            # three refusals in a row is how `quarantine` decides an agent has stopped
            # listening, so discovering it by trial is not free.
            #
            # Advisory, exactly like the trimmed manifest: the call-time gate is what enforces
            # this, and an agent that ignores the list is no less constrained.
            "tools_off_limits": fleet_svc.tools_off_limits(agent.active_role),
            # The cadence travels with the identity: an agent that has to read a constant out
            # of documentation to stay alive is one that eventually does not.
            "heartbeat_interval_seconds": fleet_svc.heartbeat_interval_seconds(),
            "presence_ttl_seconds": fleet_svc.presence_ttl_seconds(),
        }
    if name == "mint_enrolment":
        try:
            row, code = fleet_svc.mint_enrolment_as(
                db, minter_id=args["agent_id"], project_id=pid, role=args["role"],
                api_key=key, wave=args.get("wave"))
        except ValueError as e:
            raise errors.Validation(str(e))
        # Returned ONCE, like every other credential-shaped thing here.
        return {"enrolment_code": code, "role": row.role, "seat_id": row.id,
                "expires_at": row.expires_at.isoformat() if row.expires_at else None}
    if name == "fleet_status":
        minted_by = None
        if args.get("agent_id"):
            try:
                minted_by = fleet_svc.minter_for(db, args["agent_id"], key)
            except fleet_svc.NotYourAgent as e:
                raise errors.Validation(str(e))
        return fleet_svc.fleet_status(db, pid, minted_by=minted_by)
    if name == "retire_wave":
        try:
            minter = fleet_svc.minter_for(db, args["agent_id"], key)
        except fleet_svc.NotYourAgent as e:
            raise errors.Validation(str(e))
        return fleet_svc.retire_wave(db, minter_id=minter, project_id=pid,
                                     wave=args.get("wave"))
    if name == "heartbeat":
        agent = fleet_svc.caller_identity(args.get("agent_id"), key)
        if not args.get("id"):
            # Presence only. The roster's question is "who is out there", and an agent between
            # tasks is still out there — it just has no lease to extend.
            live = fleet_svc.touch(db, agent, state="idle")
            if live is None:
                raise errors.Validation(
                    f"no registered agent {agent!r}",
                    hint="call register_agent first; heartbeat keeps an existing one alive")
            return {"agent_id": live.id, "state": live.state,
                    "heartbeat_interval_seconds": fleet_svc.heartbeat_interval_seconds(),
                    "presence_ttl_seconds": fleet_svc.presence_ttl_seconds()}
        _scoped_item(db, args["id"], allowed)  # raises the precise error first
        # One call keeps BOTH alive (PRD-17 D1). An agent that heartbeats its item lease but
        # not its presence would be declared dead while visibly working — and the roster
        # would then be reporting the opposite of what is happening.
        fleet_svc.touch(db, agent, state="working")
        item = items_svc.heartbeat(db, args["id"], agent)
        if item is None:
            raise errors.Conflict(
                f"not the lease holder for {args['id']!r}",
                hint="another agent holds the lease; claim_next for fresh work",
            )
        return _item_dict(item)
    if name == "release_item":
        _scoped_item(db, args["id"], allowed)
        agent = fleet_svc.caller_identity(args.get("agent_id"), key)
        item = items_svc.release_item(db, args["id"], agent, to_status=args.get("to_status", "next"))
        if item is None:
            raise errors.Conflict(
                f"not the lease holder for {args['id']!r}",
                hint="the lease expired or another agent holds it",
            )
        return _item_dict(item)
    if name == "extract_lessons":
        _scoped_item(db, args["id"], allowed)
        return {"results": insights_svc.extract_lessons(db, args["id"])}
    if name == "generate_digest":
        return {"digest": insights_svc.generate_digest(db, project_id=pid)}
    if name == "prd_coverage":
        prd = prd_svc.get_prd(db, args["prd_id"])
        if prd is None:
            raise errors.NotFound(f"prd not found: {args['prd_id']}")
        if prd.project_id not in readable:
            raise authz.Forbidden(f"prd {args['prd_id']!r} is outside this key's project scope")
        return prd_svc.coverage(db, prd)
    if name == "decompose_prd":
        prd = prd_svc.get_prd(db, args["prd_id"])
        if prd is None:
            raise errors.NotFound(f"prd not found: {args['prd_id']}")
        if prd.project_id not in allowed:
            raise authz.Forbidden(f"prd {args['prd_id']!r} is outside this key's project scope")
        return prd_svc.decompose(
            db, prd,
            create=bool(args.get("create", False)),
            include_prose=bool(args.get("include_prose", False)),
        )
    if name == "create_prd":
        prd = prd_svc.create_prd(
            db, title=args["title"], template=args.get("template", "standard"),
            project_id=pid, body=args.get("body"),
        )
        return _prd_dict(prd)
    if name == "get_prd":
        # The read that was missing (GRPH-519). `answer_grill` reports `body_absorbed: false`,
        # and the only tool that could fix it replaces the body WHOLE — so without this an
        # agent's sole route to absorbing its own answers was to rewrite the document from
        # memory. That is the defect GRPH-515 fixed for `write_file`, and here no guard existed
        # at all: it would have silently succeeded and dropped every unreproduced section.
        prd = prd_svc.get_prd(db, args["prd_id"])
        if prd is None:
            raise errors.NotFound(f"prd not found: {args['prd_id']}")
        if prd.project_id not in allowed:
            raise authz.Forbidden(f"prd {args['prd_id']!r} is outside this key's project scope")
        return {
            "id": prd.key,
            "project_id": prd.project_id,
            "title": prd.title,
            "status": prd.status,
            "version": prd.version,
            # The token that makes a full-body replace safe (GRPH-357). NOT `version`, which
            # only moves on an explicit snapshot and so cannot say whether the body changed.
            "body_hash": prd_svc.body_hash(prd.body or ""),
            "body": prd.body or "",
        }

    if name == "update_prd":
        prd = prd_svc.get_prd(db, args["prd_id"])
        if prd is None:
            raise errors.NotFound(f"prd not found: {args['prd_id']}")
        if prd.project_id not in allowed:
            raise authz.Forbidden(f"prd {args['prd_id']!r} is outside this key's project scope")
        # THE GUARD (GRPH-357). A full-body replace with no proof of a read is refused here
        # rather than in the service, because the two callers are not alike: the REST caller
        # is a human editing a textarea they are looking at, and demanding a token from them
        # breaks the UI for no safety gain. An agent has no such guarantee — it may be forty
        # turns deep in a compacted context — and this is the call that silently deletes
        # every section it failed to reproduce.
        #
        # `section` is exempt on purpose: it cannot lose what it did not read, because it
        # rewrites one span and splices the rest back byte for byte.
        if args.get("body") is not None and args.get("section") is None \
                and not args.get("base_hash"):
            raise errors.Validation(
                "replacing a PRD body whole requires `base_hash` from get_prd — without it "
                "an unread replace silently deletes every section you did not reproduce. "
                "Call get_prd, or edit one section with `section` instead.")
        try:
            updated = prd_svc.update_prd(
                db, args["prd_id"],
                title=args.get("title"), status=args.get("status"), body=args.get("body"),
                section=args.get("section"), base_hash=args.get("base_hash"),
            )
        except prd_svc.StaleBody as e:
            raise errors.Conflict(str(e)) from e
        except (prd_svc.SectionNotFound, prd_svc.AmbiguousSection) as e:
            raise errors.Validation(str(e)) from e
        except prd_svc.RebaselineExpandsScope as e:
            raise errors.Conflict(str(e)) from e
        except prd_svc.ApprovalNotEarned as e:
            # `conflict`, not `validation`: the call was well-formed and permitted, the
            # PRD simply is not there yet. The message names what is still outstanding
            # so the agent knows what to go ask about.
            raise errors.Conflict(str(e)) from e
        return _prd_dict(updated)
    if name == "answer_grill":
        prd = prd_svc.get_prd(db, args["prd_id"])
        if prd is None:
            raise errors.NotFound(f"prd not found: {args['prd_id']}")
        if prd.project_id not in allowed:
            raise authz.Forbidden(f"prd {args['prd_id']!r} is outside this key's write scope")
        answer = (args.get("answer") or "").strip()
        if not answer:
            raise errors.Validation("answer is empty; relay what the author actually said")
        # Current interrogation only (GRPH-322): this is fed straight back into
        # `record_grill_turns`, which appends whatever is past what it already holds.
        history = prd_svc.grill_history(db, prd.id, since=prd_svc.grill_window(db, prd.id))
        prd_svc.record_grill_turns(
            db, prd.id, history + [{"role": "user", "text": answer}],
            via="agent", actor=f"agent:{key.name or key.id}",
        )
        done = prd_svc.classify_grill(db, prd)
        # Told HERE, at the moment the gap opens, rather than left for somebody to discover
        # downstream (GRPH-430). An answer is recorded against a body that does not yet say
        # it, and the agent that just relayed it is the one positioned to fix that.
        absorption = prd_svc.grill_absorption(db, prd.id)
        return {
            "prd_id": prd.key,
            "complete": done["complete"],
            "outstanding": done["outstanding"],
            "deferred": done["deferred"],
            "answers": done["answers"],
            "body_absorbed": absorption["absorbed"],
            "answers_body_has_not_absorbed": absorption["answers_behind"],
            # Declared in this tool's outputSchema and promised by its description since
            # GRPH-485, and dropped here until the conformance ratchet (GRPH-495) noticed.
            # Without them a relaying agent still cannot tell a grader outage from a thin
            # answer — the incident GRPH-485 exists to end.
            "graded": done["graded"],
            "ungraded_reason": done["ungraded_reason"],
        }
    if name == "grill_prd":
        prd = prd_svc.get_prd(db, args["prd_id"])
        if prd is None:
            raise errors.NotFound(f"prd not found: {args['prd_id']}")
        if prd.project_id not in readable:
            raise authz.Forbidden(f"prd {args['prd_id']!r} is outside this key's project scope")
        questions, retried = prd_svc.ai_command_detail(db, prd.id, "grill")
        # `retried` is reported rather than logged only: a caller whose grill was slow can
        # tell contention from a hung server without reading the server's logs (GRPH-505).
        return {"prd_id": prd.key, "questions": questions, "retried": retried}
    if name == "prd_acceptance":
        prd = _readable_prd(db, args["prd_id"], readable)
        view = args["view"]
        if view == "baseline":
            base = prd_svc.baseline_of(db, prd.id)
            result = ({"governed": False} if base is None else
                      {"governed": True, "version": base.version, "body": base.body,
                       "grill_outcomes": base.grill_outcomes or {}})
        else:
            result = {
                "completeness": prd_svc.completeness,
                "drift": prd_svc.scope_drift,
                "evidence": prd_svc.evidence_rollup,
                "close_report": prd_svc.close_report,
                "readiness": prd_svc.close_readiness,
                "lineage": prd_svc.lineage,
                "classifications": lambda d, p: {"classifications": prd_svc.classifications(d, p)},
                "audit_brief": prd_svc.audit_brief,
                "audit_coverage": prd_svc.audit_coverage,
            }.get(view, lambda d, p: {"verdicts": _verdict_dicts(prd_svc.verdicts(d, p))})(db, prd)
        return {"prd_id": prd.key, "view": view, "result": result}
    if name == "request_rebaseline":
        prd = _writable_prd(db, args["prd_id"], allowed)
        try:
            prd_svc.request_rebaseline(
                db, prd, reason_type=args["reason_type"], reason=args["reason"],
                requested_by=f"agent:{key.name or key.id}")
        except prd_svc.PrdClosed as e:
            raise errors.Conflict(str(e), hint="promote the intent into a successor PRD")
        except ValueError as e:
            raise errors.Validation(str(e))
        return {"id": prd.key, "status": prd.status,
                "pending_rebaseline": prd.pending_rebaseline or {}}
    if name == "submit_verdict":
        prd = _writable_prd(db, args["prd_id"], allowed)
        try:
            v = prd_svc.record_verdict(
                db, prd, outcome=args["outcome"], citations=args["citations"],
                reasoning=args.get("reasoning", ""), section=args.get("section"),
                signed_by=f"agent:{key.name or key.id}", api_key_id=key.id)
        except prd_svc.MalformedVerdict as e:
            # Validation, not conflict: the verdict is malformed and a retry of the same
            # payload fails identically. The message names which citation did not resolve.
            raise errors.Validation(str(e))
        return {"id": v.id, "outcome": v.outcome, "self_signed": v.self_signed,
                "separation": v.separation, "self_signed_items": v.self_signed_items or [],
                "baseline_version": v.baseline_version}
    if name == "close_prd":
        prd = _writable_prd(db, args["prd_id"], allowed)
        try:
            return prd_svc.close_prd(
                db, prd, dispositions=args["dispositions"], verdict=args.get("verdict", ""),
                closed_by=f"agent:{key.name or key.id}")
        except (prd_svc.CloseRefused, prd_svc.PrdClosed) as e:
            # Conflict, not validation: the request is well-formed and permitted — the PRD
            # simply is not accounted for yet, and the message says what is outstanding.
            raise errors.Conflict(str(e), hint="disposition the outstanding sections first")
    if name == "learning_loop":
        from app.routers.artifacts import _rec_dict

        view, pid_ = args["view"], args.get("project_id") or pid
        if view == "artifact":
            rec = db.get(ArtifactRecommendation, args.get("id") or 0)
            if rec is None:
                raise errors.NotFound(f"no such artifact: {args.get('id')}")
            if rec.project_id not in readable and rec.project_id is not None:
                raise authz.Forbidden("that artifact is outside this key's project scope")
            try:
                plan = art_svc.install_plan(db, rec)
            except art_svc.InstallRefused as e:
                plan = {"allowed": False, "reason": str(e), "contents": "", "path": ""}
            result = _rec_dict(rec) | {"draft": rec.draft, "install": plan}
        elif view == "usage":
            result = art_svc.usage_report(db, pid_)
        elif view == "stale":
            result = {"stale": [_rec_dict(r) for r in art_svc.stale_artifacts(db, pid_)]}
        else:
            result = {"recommendations": [_rec_dict(r) for r in art_svc.pending(db, pid_)]}
        return {"view": view, "result": result}
    if name == "review_recommendation":
        from app.routers.artifacts import _rec_dict

        rec = db.get(ArtifactRecommendation, args["id"])
        if rec is None:
            raise errors.NotFound(f"no such recommendation: {args['id']}")
        if rec.project_id not in allowed and rec.project_id is not None:
            raise authz.Forbidden("that recommendation is outside this key's write scope")
        rec.status = "approved" if args["decision"] == "approve" else "rejected"
        db.commit()
        db.refresh(rec)
        return _rec_dict(rec)
    if name == "describe_code":
        return code_svc.describe_code(
            db, project_id=pid,
            nodes=args.get("nodes", []),
            edges=args.get("edges", []),
            prune=bool(args.get("prune", False)),
        )
    if name == "get_code_map":
        return code_svc.get_code_map(db, pid, kind=args.get("kind"))
    if name == "code_neighbors":
        return code_svc.neighbors(db, pid, args["path"])
    if name == "graph_query":
        q = args["query"]
        types = args.get("edge_types") or None
        if types:
            unknown = sorted(t for t in types if t not in code_svc.EDGE_TYPES)
            if unknown:
                # Refuse rather than narrow silently: a filtered-to-nothing answer is
                # indistinguishable from a real empty one.
                raise ValueError(
                    f"unknown edge type(s): {', '.join(unknown)}; valid: {', '.join(code_svc.EDGE_TYPES)}"
                )
        if q == "hubs":
            rows = code_svc.hubs(db, pid, edge_types=types, limit=args.get("limit", 10))
            return {"query": q, "results": rows, "returned": len(rows)}
        if q == "components":
            rows = code_svc.components(db, pid, edge_types=types)
            return {"query": q, "results": rows, "returned": len(rows)}
        if q == "path":
            if not args.get("a") or not args.get("b"):
                raise ValueError("path needs both `a` and `b`")
            return {"query": q, **code_svc.path(db, pid, args["a"], args["b"], edge_types=types)}
        raise ValueError(f"unknown query: {q}; valid: hubs, components, path")
    if name == "search_code":
        top_k = args.get("top_k", 5)
        hits = code_svc.search_code(db, args["query"], project_id=pid, top_k=top_k)
        results = [{**code_svc.node_dict(n), "score": round(score, 4)} for n, score in hits]
        return {"results": results, "returned": len(results), "top_k": top_k}
    if name == "link_code":
        ref = code_svc.link_code(
            db, project_id=pid, ref_id=args["ref_id"], path=args["path"],
            relation=args.get("relation", "affects"), ref_type=args.get("ref_type"),
        )
        return code_svc.ref_dict(ref)
    if name == "unlink_code":
        removed = code_svc.unlink_code(
            db, project_id=pid, ref_id=args["ref_id"], path=args["path"], relation=args.get("relation"),
        )
        return {"removed": removed}
    if name == "report_graphban_issue":
        try:
            result = up_svc.submit_upstream(
                type_=args.get("type", "feedback"), title=args["title"],
                detail=args.get("detail", ""), source="mcp-agent",
            )
        # HTTPStatusError first — it subclasses HTTPError, and conflating the two reported
        # every permanent 4xx as "unreachable, retry later", which sends the next agent
        # chasing dead hosts instead of reading its own config.
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            if code == 404:
                raise errors.Conflict(
                    "upstream accepted the connection but could not resolve its project "
                    "(404). A hosted intake honours only the share token — set "
                    "UPSTREAM_FEEDBACK_TOKEN. The same 404 also covers a project that has "
                    "not enabled public sharing; the intake makes the two "
                    "indistinguishable on purpose.",
                    hint="configuration, not transient — retrying will not help",
                )
            raise errors.Conflict(
                f"upstream rejected the report: HTTP {code}",
                hint=("configuration, not transient — retrying will not help"
                      if code < 500 else "upstream server error — retry later"),
            )
        except httpx.HTTPError as e:
            raise errors.Conflict(f"upstream unreachable: {e}", hint="retry later")
        req = result.get("request", {})
        return {
            "ok": True, "request_id": req.get("id"),
            "target": up_svc.target_host(), "duplicates": result.get("duplicates", []),
        }
    raise errors.Validation(f"unknown tool: {name}", hint="call tools/list for the available tools")


def _audit_tool(db: Session, key: ApiKey, name: str, result: Any) -> None:
    """Best-effort audit of an accepted agent mutation. Pulls the target id and
    project from the tool result where present (most write tools echo them)."""
    target_id, project_id, meta = "", None, None
    if isinstance(result, dict):
        target_id = str(result.get("id") or result.get("request_id") or "")
        project_id = result.get("project_id")
        # Proof-on-done (AL-53): carry the receipts into the ledger so a completion
        # is auditable against its evidence, not just its green check.
        if result.get("evidence"):
            meta = {"status": result.get("status"), "evidence": result["evidence"]}
    events_svc.record_key(
        db, key, action=name,
        target_type="item" if name in _ITEM_WRITE_TOOLS else "",
        target_id=target_id, project_id=project_id, meta=meta,
    )


def _attach_directive(db: Session, result: Any, args: dict) -> Any:
    """Ride the agent's outstanding directive back on this response, and ack it.

    Acked on DELIVERY rather than on the agent's next call: a second round trip to confirm is
    a round trip that can be lost, and a directive redelivered forever is worse than one
    delivered once — the agent would keep re-adopting a role it already holds.

    Only ever added to a dict result, and never overwrites a key a tool already returned.
    """
    if not isinstance(result, dict) or "directive" in result:
        return result
    directive = fleet_svc.collect_directive(db, args.get("agent_id"))
    if directive is not None:
        result = {**result, "directive": directive}
    return result


def _session_role(db: Session, request: Request) -> str | None:
    """The role of the agent registered on THIS connection, or None (PRD-19 E9a/E9b).

    None means "cannot say", and every caller treats that as today's behaviour rather than as
    a restriction — a client that never sends the header, one that predates it, or a connection
    with no registered agent all land here.

    **Two agents on one connection resolve to None as well**, and that case is real: an
    orchestrator and the subagent it spawns can share a transport. Guessing between them would
    hand somebody a manifest trimmed for the other one, and the whole value of this is that a
    wrong answer costs nothing — so it declines to answer instead.
    """
    from app.models import Agent
    from app.services import fleet as fleet_svc

    sid = request.headers.get("mcp-session-id")
    if not sid:
        return None
    rows = db.scalars(select(Agent).where(Agent.mcp_session_id == sid,
                                          Agent.dismissed_at.is_(None))).all()
    live = [a for a in rows if fleet_svc.presence_state(a) != "offline"]
    if len(live) != 1:
        return None
    agent = live[0]
    # An expired seat grants no role, so it must not narrow the manifest either — the agent
    # needs `fleet_status` to collect the directive telling it to re-enrol.
    if fleet_svc.session_expired(db, agent):
        return None
    return agent.active_role


def _rpc_result(id_: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _rpc_error(id_: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _run_deferred(jobs: list) -> None:
    """Run the work scheduled off the response path. One failure must not eat the rest, and
    none of it may reach the client — the response has already been sent."""
    for job in jobs:
        try:
            job()
        except Exception:  # noqa: BLE001
            logger.exception("deferred post-completion work failed")


def _success(id_: Any, result: Any) -> dict:
    """Wrap a tool result. Objects are also returned as `structuredContent` (typed,
    no JSON-in-a-text-block); text mirrors it for back-compat (#8)."""
    payload: dict[str, Any] = {"content": [{"type": "text", "text": json.dumps(result)}]}
    if isinstance(result, dict):
        payload["structuredContent"] = result
    return _rpc_result(id_, payload)


def _tool_error(id_: Any, code: str, message: str, hint: str | None = None) -> dict:
    """A tool-level failure. Reported via isError so the agent sees it, with a stable
    machine-readable `code` in structuredContent to branch on and an optional `hint`
    naming the corrective action (AL-47)."""
    err: dict[str, Any] = {"code": code, "message": message}
    if hint:
        err["hint"] = hint
    text = f"{code}: {message}" + (f" ({hint})" if hint else "")
    return _rpc_result(
        id_,
        {
            "content": [{"type": "text", "text": text}],
            "structuredContent": {"error": err},
            "isError": True,
        },
    )


@router.post("/mcp")
async def mcp_endpoint(
    request: Request,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(get_agent_key),
):
    # Body parsing is inside the guard now — a malformed or non-object body is a
    # JSON-RPC parse error, not a raw HTTP 500 that escapes the envelope (AL-47).
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _rpc_error(None, -32700, "parse error: request body is not valid JSON")
    if not isinstance(body, dict):
        return _rpc_error(None, -32600, "invalid request: body must be a JSON object")
    method = body.get("method")
    id_ = body.get("id")

    # Notifications (no id) get a 202 with no body.
    if method == "notifications/initialized":
        return Response(status_code=202)

    if method == "initialize":
        requested = body.get("params", {}).get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
        out = JSONResponse(_rpc_result(
            id_,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "graphban", "version": "0.1.0"},
            },
        ))
        # E9a. A client that echoes this back lets a later `tools/list` be attributed to ONE
        # agent rather than only to a credential several agents share. A client that ignores it
        # is unaffected: every path below falls back to the credential.
        out.headers["Mcp-Session-Id"] = secrets.token_urlsafe(18)
        return out

    if method == "tools/list":
        # E9b. Narrowed to the registered agent's role when this connection carries exactly one
        # — otherwise the credential's ceiling, which is today's answer.
        # A probe recorded `tools_list_refetched` here until 2026-08-20, to settle whether a
        # real client re-fetches its manifest unprompted. It does: Grok Build shell asked again
        # within 20 seconds of registering and took the narrowed list. That closed E9c — no
        # `tools/list_changed` push, no SSE — so the instrument came out rather than costing an
        # event row per narrowed fetch for the rest of the product's life.
        #
        # What it could NOT answer is worth carrying: it recorded only successful narrowing, so
        # on a multiplexed client — where `_session_role` declines and nothing narrows — a
        # re-fetch and no fetch look identical. Anyone measuring that case needs to record the
        # ATTEMPT and its binding state, which is a different instrument, not this one revived.
        return _rpc_result(id_, {"tools": _visible_tools(key, role=_session_role(db, request))})

    if method == "tools/call":
        params = body.get("params", {})
        name = params.get("name")
        args = params.get("arguments", {}) or {}
        try:
            # Validate arguments against the declared schema before dispatch, so a
            # bad call is an actionable error rather than a KeyError or a silently
            # accepted junk value (AL-47).
            _validate_args(name, args)
            # Hybrid proxy (AL-138): when linked to a cloud tenant, non-graph tools run on the
            # cloud (authoritative for items/claims/memory), graph tools stay local. The cloud
            # applies its own authz + metering + audit, so we return its result verbatim and
            # skip the local metering/audit below.
            if mcp_proxy.should_proxy(name):
                cloud = await run_in_threadpool(mcp_proxy.forward, name, args)
                if "result" in cloud:
                    return _rpc_result(id_, cloud["result"])
                err = cloud.get("error") or {}
                return _tool_error(id_, "internal", err.get("message", "cloud proxy error"),
                                   hint="safe to retry once; if it persists, report it")
            # Run tool dispatch (sync DB + any outbound IO like report_graphban_issue) off
            # the event loop, so a slow/hanging tool never blocks the async server — and a
            # same-host upstream loop-back can still be served concurrently.
            deferred: list = []
            result = await run_in_threadpool(
                _call_tool, db, name, args, key,
                request.headers.get("mcp-session-id"), deferred.append)
            # The DOWNLINK (PRD-17 D-e). MCP is client→server: the server cannot wake an idle
            # terminal, so the orchestrator's intent rides back on whatever the agent polls
            # next. A role change is not an error and does not arrive as one — the agent's
            # loop prompt says "if a response carries a directive, adopt it and continue", so
            # reassignment lands with no reconnect, no re-prime, and no new transport.
            #
            # Collected here rather than inside each handler: every fleet tool would otherwise
            # need to remember, and the one that forgot would strand a directive silently.
            result = await run_in_threadpool(_attach_directive, db, result, args)
        except authz.Forbidden as e:
            # Authenticated but out of scope: distinct code so agents can branch
            # (retry won't help — a different key or membership grant will).
            return _tool_error(id_, "unauthorized", str(e), getattr(e, "hint", None))
        except errors.AppError as e:
            # Expected, agent-correctable failure: not_found | validation | conflict.
            return _tool_error(id_, e.code, str(e), e.hint)
        except ValueError as e:
            # A service rejected the input (bad enum, unknown project, etc.).
            return _tool_error(id_, "validation", str(e))
        except KeyError as e:
            # A required arg slipped past validation (belt and braces).
            return _tool_error(id_, "validation", f"missing argument: {e}")
        except httpx.HTTPError as e:
            # A provider adapter that hasn't been wrapped yet (providers/base.py
            # `provider_errors` gives a far better message where it is applied). Still
            # far better than the generic handler below, whose "safe to retry once" is
            # actively wrong for a misconfiguration.
            logger.warning("provider transport failure in %r: %s", name, e)
            return _tool_error(
                id_, "unavailable", f"AI provider unreachable: {type(e).__name__}",
                hint="check the provider base URL, key and model in Settings -> AI "
                     "providers; this is configuration, not a transient failure",
            )
        except Exception:  # noqa: BLE001 — never leak a raw 500 to a JSON-RPC client
            logger.exception("MCP tool %r failed", name)
            db.rollback()
            return _tool_error(id_, "internal", f"internal error executing {name!r}",
                               hint="safe to retry once; if it persists, report it")
        # Meter only successful calls, after dispatch — failed/unknown-tool calls no
        # longer inflate the MCP Tools dashboard (AL-47).
        mcp_stats.increment(db, name)
        # Audit every accepted agent mutation, attributed to the key (AL-43).
        if name not in _READ_ONLY:
            _audit_tool(db, key, name, result)
        if deferred:
            # AFTER the response, not before it (GRPH-399). Completion schedules the judge and
            # the lesson extractor here; on the live instance those are two calls to a 24B
            # model, and an agent that waits on them stops heartbeating. Starlette runs a
            # background task once the response is sent — and, under the test client, before
            # the call returns, so the tests that drive extraction through the status
            # transition stay deterministic.
            return JSONResponse(_success(id_, result),
                                background=BackgroundTask(_run_deferred, deferred))
        return _success(id_, result)

    return _rpc_error(id_, -32601, f"method not found: {method}")
