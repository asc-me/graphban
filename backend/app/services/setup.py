"""First-run bootstrap guidance (AL-133).

`setup_project` returns an ordered, RESUMABLE checklist that walks a fresh project from empty to
useful — confirm the right project, build the code graph, load memories, propose work items
(v1 steps 1–4). It's a GUIDE, not an executor: each step reports done/pending from the project's
current state and tells the agent how to do it with the existing tools, so re-running just
reflects progress (idempotent). The heavy code-graph pass is local-first (AL-134) — no hosted
call quota. `get_context` flags an empty project and points here.

PRD-P67 (GRPH-1360) makes this module the ONE owner of setup state for two surfaces: the web
quick-start dialog (`GET /api/projects/{id}/setup`, steps ⓪–④) and the `setup_project` MCP
tool, which is now a projection of the same `checklist()` so an old agent sees the shape it
always saw. Each step is `{id, step, status, detail, how}` with status one of
`done` / `pending` / `unknown` — a check that could not run says `unknown` with a `reason`,
never `done`. The only stored facts are human decisions (`confirm()`), audited in `events`;
`users.onboarding` is UI progress and is never read here as evidence.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.errors import NotFound, Validation
from app.models import CodeNode, Item, MemoryShard, Project, User

logger = logging.getLogger("graphban.setup")

STATUSES = ("done", "pending", "unknown")

# Sources are EXTENSIBLE — editor conventions (.cursor/rules, Grok Build GROK.md, Codex
# AGENTS.md) plug in later (AL-133 D2).
_MEMORY_SOURCES = ["AGENTS.md", "CLAUDE.md", "docs/"]
_ITEM_SOURCES = ["TODO/FIXME comments", "PLAN.md", "ROADMAP.md"]


def counts(db: Session, project_id: str) -> tuple[int, int, int]:
    """(code nodes, items, memory shards) for a project — the bootstrap progress signal."""
    nodes = db.scalar(select(func.count()).select_from(CodeNode).where(CodeNode.project_id == project_id)) or 0
    items = db.scalar(select(func.count()).select_from(Item).where(Item.project_id == project_id)) or 0
    shards = db.scalar(select(func.count()).select_from(MemoryShard).where(MemoryShard.project_id == project_id)) or 0
    return nodes, items, shards


def is_empty(db: Session, project_id: str) -> bool:
    """No graph, no items, no memory — the first-run signal get_context surfaces."""
    return counts(db, project_id) == (0, 0, 0)


def _counter(model) -> Callable[[Session, str], int]:
    def count(db: Session, project_id: str) -> int:
        return db.scalar(select(func.count()).select_from(model)
                         .where(model.project_id == project_id)) or 0
    return count


def _guarded(db: Session, check: Callable[[], dict]) -> dict:
    """Run one step's check; a check that raises is `unknown` with the reason, never `done`.

    The savepoint keeps a failed query from poisoning the caller's transaction on Postgres,
    so one broken check cannot take the other steps down with it."""
    try:
        with db.begin_nested():
            out = check()
    except Exception as e:  # noqa: BLE001 — any failure is "could not look", reported as such
        logger.warning("setup check failed: %s", e)
        return {"status": "unknown", "reason": f"the check could not run: {type(e).__name__}: {e}"}
    assert out["status"] in STATUSES, out
    return out


def _decisions(db: Session, project_id: str) -> dict:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFound(f"project {project_id!r} not found")
    return dict(project.setup_decisions or {})


_CONFIRM_HOW = (
    "A HUMAN confirms this is the right workspace before bootstrapping — writing into "
    "the wrong project pollutes another (in hosted mode, another tenant). Don't guess. "
    "This holds even when an agent created the project itself (AL-284): being able to "
    "CREATE a workspace is not the same as knowing it's the one that was wanted, so "
    "the step stays a human confirmation rather than degrading to a notification. "
    "The confirmation is ticked in the web app's quick-start dialog, per person.")


def _where(decisions: dict) -> dict:
    if settings.hosted_mode:
        return {"status": "done",
                "detail": "Hosted: Graphban runs on someone else's server; there is nothing to answer."}
    where = decisions.get("where")
    if not where:
        return {"status": "pending",
                "detail": "Nobody has said where this Graphban server runs yet."}
    if where["answer"] == "dedicated_server":
        return {"status": "done",
                "detail": f"On a dedicated server at {where['server_address']}."}
    return {"status": "done", "detail": "On this computer — the one you code on."}


def _project(decisions: dict, project_id: str, user_id: str | None) -> dict:
    confirmed = decisions.get("project_confirmed") or {}
    if user_id is not None:
        mine = confirmed.get(user_id)
        if mine:
            return {"status": "done",
                    "detail": f"You confirmed '{project_id}' is the workspace you want agents writing to."}
        return {"status": "pending",
                "detail": f"You're set to write into '{project_id}'; nobody has confirmed it as you."}
    # No human behind the caller (an unowned key): any person's confirmation is the best
    # evidence there is, and the detail says whose it was.
    if confirmed:
        return {"status": "done",
                "detail": f"Confirmed by {len(confirmed)} person(s) as the workspace to write into."}
    return {"status": "pending", "detail": f"You're set to write into '{project_id}'."}


def _model(db: Session, project_id: str, decisions: dict) -> dict:
    from app.services import platform  # lazy: platform imports providers

    resolved = platform.resolve_chat(db, project_id)
    if resolved.provider_id != "stub":
        return {"status": "done",
                "detail": f"Chat runs on {resolved.provider_id}"
                          + (f" ({resolved.model})." if resolved.model else ".")}
    if decisions.get("stay_offline"):
        return {"status": "done",
                "detail": "You chose to stay offline: chat is canned and nothing gets graded."}
    return {"status": "pending",
            "detail": "No model chosen: the offline stub is running by default, not by decision."}


def _connect() -> dict:
    # No signal exists yet that an agent connected: `api_keys.last_used_at` is PRD-P67 S2 and
    # machine reports are S7. Until then the honest answer is "could not look", not pending
    # (which would claim we looked and saw nothing) and never done.
    return {"status": "unknown",
            "reason": "no connection signal exists yet — a key's first call (S2) and machine "
                      "reports (S7) are not built, so this cannot be checked"}


def _count_step(n: int, noun: str) -> dict:
    return {"status": "done" if n else "pending", "detail": f"{n} {noun}."}


# The full step set, in dialog order. `mcp_step` is the number `setup_project` has always
# shown for that step (None: no MCP step — PRD-P67 §2, model and connect are dialog-only).
# A step with an `mcp_step` is SHARED, and the ratchet in tests/test_setup_project.py holds
# its id and status equal on both surfaces.
_STEPS = (
    {"id": "where", "step": 0, "name": "Where is Graphban running?", "mcp_step": None,
     "how": "Answer once in the quick-start dialog: on this computer, or on a dedicated server "
            "(with its address). Later steps' commands read the answer."},
    {"id": "project", "step": 1, "name": "Confirm the project", "mcp_step": 1,
     "how": _CONFIRM_HOW},
    {"id": "model", "step": 2, "name": "Choose a model", "mcp_step": None,
     "how": "Pick offline (stub), local (Ollama) or cloud (Claude) in the quick-start dialog or "
            "Settings → AI providers. Staying offline is a recorded decision, not a default."},
    {"id": "connect", "step": 3, "name": "Connect your agent", "mcp_step": None,
     "how": "Run `gban setup` from the repository (or paste a key into your harness), then "
            "restart the agent."},
    {"id": "code_graph", "step": 4, "name": "Build the code graph", "mcp_step": 2,
     "how": "describe_code across the repo (link_code to bridge items↔paths). LOCAL-FIRST: run "
            "on the local instance so the pass never hits the hosted call quota (AL-134); "
            "cloud-only, request a dry-run size/cost estimate + a budget first."},
    {"id": "memories", "step": 4, "name": "Load memories", "mcp_step": 3,
     "how": "Read the convention files and add_memory each durable fact as a CANDIDATE "
            "(human-reviewed, never auto-published).",
     "sources": _MEMORY_SOURCES},
    {"id": "items", "step": 4, "name": "Propose work items", "mcp_step": 4,
     "how": "Scan the sources and create_item to PROPOSE work (deduped against existing items, "
            "capped) — never bulk-create a flood.",
     "sources": _ITEM_SOURCES},
)

SHARED_STEP_IDS = tuple(s["id"] for s in _STEPS if s["mcp_step"] is not None)


def checklist(db: Session, project_id: str, *, user_id: str | None = None) -> dict:
    """The setup checklist for steps ⓪–④ — the one owner both surfaces read.

    `user_id` is the human asking (the logged-in user, or the API key's owner): the project
    confirmation is per person. Every step's status is derived from server state or a stored
    human decision; nothing here reads `users.onboarding`."""
    decisions = _decisions(db, project_id)
    checks: dict[str, Callable[[], dict]] = {
        "where": lambda: _where(decisions),
        "project": lambda: _project(decisions, project_id, user_id),
        "model": lambda: _model(db, project_id, decisions),
        "connect": _connect,
        "code_graph": lambda: _count_step(_counter(CodeNode)(db, project_id), "code node(s) described"),
        "memories": lambda: _count_step(_counter(MemoryShard)(db, project_id), "memory shard(s)"),
        "items": lambda: _count_step(_counter(Item)(db, project_id), "item(s)"),
    }
    steps = []
    for spec in _STEPS:
        if spec["id"] == "where" and settings.hosted_mode:
            continue  # hosted skips step ⓪ (PRD-P67 §4): the answer is always "someone else's"
        out = _guarded(db, checks[spec["id"]])
        steps.append({**{k: v for k, v in spec.items() if k != "mcp_step"},
                      "shared": spec["mcp_step"] is not None, **out})
    return {"project_id": project_id, "steps": steps}


def project_setup(db: Session, project_id: str, *, user_id: str | None = None) -> dict:
    """`setup_project` (MCP): a projection of `checklist()` in the shape it has always had —
    `step` 1–4, `name`, `status`, `detail`, `how`, `sources` — plus each step's shared `id`."""
    full = {s["id"]: s for s in checklist(db, project_id, user_id=user_id)["steps"]}
    steps = []
    for spec in sorted((s for s in _STEPS if s["mcp_step"] is not None), key=lambda s: s["mcp_step"]):
        src = full[spec["id"]]
        step = {"step": spec["mcp_step"], "id": spec["id"], "name": src["name"],
                "status": src["status"], "detail": src.get("detail", ""), "how": src["how"]}
        if "reason" in src:
            step["reason"] = src["reason"]
        if "sources" in src:
            step["sources"] = src["sources"]
        steps.append(step)
    nodes, items, shards = counts(db, project_id)
    # Step 1 is a human confirmation; completeness is about steps 2–4 having content.
    complete = all(s["status"] == "done" for s in steps[1:])
    return {
        "project_id": project_id,
        "empty": (nodes, items, shards) == (0, 0, 0),
        "complete": complete,
        "steps": steps,
        "note": "Resumable + idempotent — re-run any time; done steps reflect current state.",
    }


# ---- Stored human decisions (PRD-P67 §6) -------------------------------------------------

DECISIONS = ("project_confirmed", "stay_offline", "where")
WHERE_ANSWERS = ("this_computer", "dedicated_server")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def confirm(db: Session, project_id: str, user: User, body: dict) -> dict:
    """Store ONE human decision and audit it. The only three accepted:

    - `{"decision": "project_confirmed"}` — P1: this is the workspace I want agents writing to
    - `{"decision": "stay_offline"}` — the model step's "offline, on purpose"
    - `{"decision": "where", "answer": "this_computer" | "dedicated_server",
       "server_address": "..."}` — step ⓪ (address required for, and only for, a dedicated server)

    Anything else — another decision, an extra key, a missing address — is refused, so this
    endpoint cannot become a general store for UI progress. Returns the refreshed checklist."""
    if not isinstance(body, dict):
        raise Validation("body must be an object")
    decision = body.get("decision")
    if decision not in DECISIONS:
        raise Validation(f"decision must be one of {', '.join(DECISIONS)}",
                         hint="only human decisions are stored; step status comes from server state")
    allowed = {"decision"} | ({"answer", "server_address"} if decision == "where" else set())
    extra = sorted(set(body) - allowed)
    if extra:
        raise Validation(f"unexpected field(s) for {decision!r}: {', '.join(extra)}")
    if decision == "where" and settings.hosted_mode:
        raise Validation("hosted has no step ⓪: Graphban always runs on someone else's server")

    project = db.get(Project, project_id)
    if project is None:
        raise NotFound(f"project {project_id!r} not found")
    decisions = dict(project.setup_decisions or {})
    stamp = {"at": _now_iso(), "by": user.id}
    meta: dict = {"decision": decision}
    if decision == "project_confirmed":
        confirmed = dict(decisions.get("project_confirmed") or {})
        confirmed[user.id] = stamp
        decisions["project_confirmed"] = confirmed
    elif decision == "stay_offline":
        decisions["stay_offline"] = stamp
    else:
        answer = body.get("answer")
        if answer not in WHERE_ANSWERS:
            raise Validation(f"answer must be one of {', '.join(WHERE_ANSWERS)}")
        address = body.get("server_address")
        if answer == "dedicated_server":
            if not isinstance(address, str) or not address.strip():
                raise Validation("server_address is required for a dedicated server")
            address = address.strip()
        elif address is not None:
            raise Validation("server_address is only accepted with answer 'dedicated_server'")
        where = {"answer": answer, **stamp}
        if address:
            where["server_address"] = address
        decisions["where"] = where
        meta.update({k: v for k, v in where.items() if k in ("answer", "server_address")})
    project.setup_decisions = decisions  # reassign: a mutated JSON dict is not change-tracked
    db.commit()

    from app.services import events as events_svc

    events_svc.record_user(db, user, action="setup_confirm", target_type="project",
                           target_id=project_id, project_id=project_id, meta=meta)
    return checklist(db, project_id, user_id=user.id)
