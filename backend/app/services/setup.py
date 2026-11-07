"""First-run bootstrap guidance (AL-133).

`setup_project` returns an ordered, RESUMABLE checklist that walks a fresh project from empty to
useful — confirm the right project, build the code graph, load memories, propose work items
(v1 steps 1–4). It's a GUIDE, not an executor: each step reports done/pending from the project's
current state and tells the agent how to do it with the existing tools, so re-running just
reflects progress (idempotent). The heavy code-graph pass is local-first (AL-134) — no hosted
call quota. `get_context` flags an empty project and points here.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import CodeNode, Item, MemoryShard, Project

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


_WHERE = ("this_computer", "dedicated_server")
STATUSES = ("done", "pending", "unknown")


def _decisions(db: Session, project_id: str) -> dict:
    project = db.get(Project, project_id)
    return dict((project.setup_decisions if project else None) or {})


def confirm(db: Session, project_id: str, decision: str, *, where: str | None = None,
            address: str = "") -> dict:
    """Store ONE human decision. Only `workspace`, `offline` and `server` exist; anything else
    is refused here, not by the caller, so every surface shares the same boundary."""
    project = db.get(Project, project_id)
    if project is None:
        raise ValueError("project not found")
    d = dict(project.setup_decisions or {})
    if decision == "workspace":
        d["workspace_confirmed_at"] = datetime.now(timezone.utc).isoformat()
    elif decision == "offline":
        d["offline"] = True
    elif decision == "server":
        if where not in _WHERE:
            raise ValueError(f"where must be one of {', '.join(_WHERE)}")
        if where == "dedicated_server" and not address.strip():
            raise ValueError("a dedicated server needs an address")
        d["server"] = {"where": where, "address": address.strip() if where == "dedicated_server" else ""}
    else:
        raise ValueError("decision must be one of: workspace, offline, server")
    project.setup_decisions = d  # reassign: a mutated JSON dict is not change-tracked
    db.commit()
    return d


def _count_step(step: int, name: str, how: str, run, label: str, **extra) -> dict:
    """A server-state step. A count that could not run is `unknown` with its reason — never
    `done`, because a failed check is not evidence of anything."""
    try:
        n = run()
    except Exception as e:  # noqa: BLE001
        return {"step": step, "name": name, "status": "unknown",
                "detail": f"could not check: {type(e).__name__}", "reason": str(e)[:200],
                "how": how, **extra}
    return {"step": step, "name": name, "status": "done" if n else "pending",
            "detail": f"{n} {label}.", "how": how, **extra}


def checklist(db: Session, project_id: str) -> dict:
    """Steps ⓪–④. ✓ comes from server state (counts, stored human decisions), never from UI
    progress. `steps` holds all five; `setup_project` projects ①–④ via `agent_view`."""
    d = _decisions(db, project_id)
    server = d.get("server")
    steps = [
        {"step": 0, "name": "Where Graphban runs", "status": "done" if server else "pending",
         "detail": (f"{server['where']}" + (f" at {server['address']}" if server.get("address") else ""))
         if server else "Not answered yet.",
         "how": "A human answers: this computer, or a dedicated server (with its address)."},
        {"step": 1, "name": "Confirm the project",
         "status": "done" if d.get("workspace_confirmed_at") else "pending",
         "needs_human": True,
         "detail": f"You're set to write into '{project_id}'.",
         "how": "A HUMAN confirms this is the right workspace before bootstrapping — writing into "
                "the wrong project pollutes another (in hosted mode, another tenant). Don't guess. "
                "This holds even when an agent created the project itself (AL-284): being able to "
                "CREATE a workspace is not the same as knowing it's the one that was wanted, so "
                "the step stays a human confirmation rather than degrading to a notification."},
        _count_step(2, "Build the code graph",
                    "describe_code across the repo (link_code to bridge items↔paths). LOCAL-FIRST: run "
                    "on the local instance so the pass never hits the hosted call quota (AL-134); "
                    "cloud-only, request a dry-run size/cost estimate + a budget first.",
                    lambda: _n(db, CodeNode, project_id), "code node(s) described"),
        _count_step(3, "Load memories",
                    "Read the convention files and add_memory each durable fact as a CANDIDATE "
                    "(human-reviewed, never auto-published).",
                    lambda: _n(db, MemoryShard, project_id), "memory shard(s)", sources=_MEMORY_SOURCES),
        _count_step(4, "Propose work items",
                    "Scan the sources and create_item to PROPOSE work (deduped against existing items, "
                    "capped) — never bulk-create a flood.",
                    lambda: _n(db, Item, project_id), "item(s)", sources=_ITEM_SOURCES),
    ]
    return {"project_id": project_id, "offline": bool(d.get("offline")), "steps": steps}


def _n(db: Session, model, project_id: str) -> int:
    return db.scalar(select(func.count()).select_from(model).where(model.project_id == project_id)) or 0


def agent_view(db: Session, project_id: str) -> dict:
    """`setup_project`'s answer: a projection of `checklist` — steps ①–④ with the same id and
    status, plus the `empty`/`complete` flags agents already read."""
    full = checklist(db, project_id)
    steps = [s for s in full["steps"] if s["step"] >= 1]
    counts_ = [s for s in steps if s["step"] >= 2]
    empty = all(s["status"] == "pending" for s in counts_)
    return {
        "project_id": project_id,
        "empty": empty,
        "complete": all(s["status"] == "done" for s in counts_),
        "steps": steps,
        "note": "Resumable + idempotent — re-run any time; done steps reflect current state.",
    }


# The dialog's own progress marks (GRPH-P67 D8). UI facts only: nothing here is evidence that a
# setup step happened, and no checklist status reads it. Each mark stamps its `<mark>_at` once
# per call; later items (the tour, Request an org) add their marks here, not a second writer.
ONBOARDING_MARKS = ("quickstart_finished", "quickstart_dismissed", "tour_completed")

# The tour records which stops this user's nav had no anchor for (GRPH-1363). Ids are the
# `data-tour` values; the bounds only keep a client from storing an arbitrary blob.
TOUR_SKIPPED_MAX = 20
TOUR_STOP_ID_MAX = 40


def mark_onboarding(db: Session, user, mark: str, skipped_stops: list[str] | None = None) -> dict:
    """Stamp `<mark>_at` on the user's onboarding document and return the whole document.

    Only the listed marks exist; anything else is refused here so every surface shares the
    boundary. The document is merged, never replaced, so one mark cannot erase another.
    `tour_completed` also stores `tour_skipped_stops` (empty when every stop played); no
    other mark takes it."""
    if mark not in ONBOARDING_MARKS:
        raise ValueError(f"mark must be one of: {', '.join(ONBOARDING_MARKS)}")
    if skipped_stops is not None and mark != "tour_completed":
        raise ValueError("skipped_stops goes with the tour_completed mark only")
    if skipped_stops is not None and (
        len(skipped_stops) > TOUR_SKIPPED_MAX
        or any(not isinstance(s, str) or not s or len(s) > TOUR_STOP_ID_MAX for s in skipped_stops)
    ):
        raise ValueError(f"skipped_stops is at most {TOUR_SKIPPED_MAX} stop ids of 1-{TOUR_STOP_ID_MAX} characters")
    doc = dict(user.onboarding or {})
    doc[f"{mark}_at"] = datetime.now(timezone.utc).isoformat()
    if mark == "tour_completed":
        doc["tour_skipped_stops"] = list(skipped_stops or [])
    user.onboarding = doc  # reassign: a mutated JSON dict is not change-tracked
    db.commit()
    return doc
