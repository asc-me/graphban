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

from app.config import DEFAULT_JWT_SECRET, settings
from app.models import ApiKey, CodeNode, Item, Membership, MemoryShard, Project, User, utcnow

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
        if d.get("workspace_confirmed_at"):
            return d  # already confirmed: re-confirming is a no-op
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


def checklist(db: Session, project_id: str, user_id: str | None = None) -> dict:
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
    steps.append(_connect_step(db, project_id, user_id))
    return {"project_id": project_id, "offline": bool(d.get("offline")), "steps": steps}


CONNECT_STEP = 5  # quick-start ③; numbered last so ①–④ keep the ids agents already read


def _connect_step(db: Session, project_id: str, user_id: str | None = None) -> dict:
    """Quick-start ③: ✓ only when a key the dialog minted for this project has made an
    authenticated call (`api_keys.last_used`, GRPH-1362). A revoked or expired key proves
    nothing now, so it is not evidence. Opening a later screen never reaches here. A machine
    report is the second proof (GRPH-1442): any of this user's machines that reported with no
    FAIL line for this project. A failed or undelivered report is never proof."""
    base = {"step": CONNECT_STEP, "name": "Connect your agent",
            "how": "Install the harness config (gban setup, or paste a key) and restart the agent; "
                   "the first call it makes is the proof."}
    try:
        keys = db.scalars(select(ApiKey).where(
            ApiKey.project_id == project_id, ApiKey.origin == "quickstart",
            ApiKey.revoked.is_(False), ApiKey.last_used.is_not(None)
        ).execution_options(populate_existing=True)).all()  # a row this session loaded earlier may be stale
        live = []
        for k in keys:
            exp = k.expires_at
            if exp is not None and (exp if exp.tzinfo else exp.replace(tzinfo=timezone.utc)) <= utcnow():
                continue
            live.append(k)
    except Exception as e:  # noqa: BLE001
        return {**base, "status": "unknown", "detail": f"could not check: {type(e).__name__}",
                "reason": str(e)[:200]}
    from app.services import machines as machines_svc

    try:
        proof = machines_svc.project_proof(db, project_id, user_id)
    except Exception as e:  # noqa: BLE001
        return {**base, "status": "unknown", "detail": f"could not check: {type(e).__name__}",
                "reason": str(e)[:200]}
    base["machines"] = proof["machines"]
    if proof["clean"] and not live:
        name, entry = proof["clean"]
        return {**base, "status": "done", "proof": "machine_report", "machine": name,
                "detail": f"{name} reported clean at {entry['at']}."}
    if not live:
        if proof["failed"]:
            name, entry = proof["failed"]
            why = (f"{entry.get('fails', 0)} FAIL" if entry.get("fails")
                   else f"{entry.get('unknowns', 0)} UNKNOWN")
            return {**base, "status": "pending", "detail": f"{name} reported {why}.",
                    "reported": {"machine": name, "fails": entry.get("fails", 0),
                                 "unknowns": entry.get("unknowns", 0),
                                 "failing": entry.get("failing", []), "at": entry["at"]}}
        return {**base, "status": "pending", "detail": "No first call seen yet."}
    k = max(live, key=lambda r: r.last_used)
    return {**base, "status": "done", "proof": "first_call", "key_name": k.name,
            "detail": f"First call from key '{k.name}' at {k.last_used.isoformat()}."}


def _n(db: Session, model, project_id: str) -> int:
    return db.scalar(select(func.count()).select_from(model).where(model.project_id == project_id)) or 0


def agent_view(db: Session, project_id: str) -> dict:
    """`setup_project`'s answer: a projection of `checklist` — steps ①–④ with the same id and
    status, plus the `empty`/`complete` flags agents already read."""
    full = checklist(db, project_id)
    steps = [s for s in full["steps"] if 1 <= s["step"] <= 4]
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
ONBOARDING_MARKS = ("quickstart_finished", "quickstart_dismissed", "tour_completed",
                    "tour_offered")

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


# ---- instance posture (GRPH-1365) ------------------------------------------------------------

def is_instance_admin(db: Session, user) -> bool:
    """Who may read the posture: the platform operator when hosted, otherwise anyone who owns a
    project on this box (self-host has no separate admin role; the first account owns its first
    project). A member of someone else's project learns nothing about this deployment."""
    from app.services import quotas

    if settings.hosted_mode:
        return quotas.is_platform_admin(user)
    if db.scalar(select(func.count()).select_from(Membership).where(
            Membership.user_id == user.id, Membership.role == "owner")) > 0:
        return True
    # GRPH-1447: the first account on a fresh self-host box owns no project yet on step ①, but it
    # is the box's only user, so it is its operator. The read returns booleans only. Hosted mode
    # returned above and is not loosened.
    return db.scalar(select(func.count()).select_from(User)) == 1


def instance_posture(db: Session, *, loopback: bool) -> dict:
    """Booleans only — never a value. `ok` is True when the setting is safe; every field answers
    one question the dialog's \"Is this box safe to expose?\" check asks.

    The caller is signed in, so an account already exists: open signup is past its first-account
    use (D3)."""
    return {
        "jwt_secret_set": settings.jwt_secret != DEFAULT_JWT_SECRET,
        "jwt_secret_strong": not settings.jwt_secret_is_weak,
        "signup_open": settings.signup_mode == "open",
        "secret_encryption_key_set": bool(settings.secret_encryption_key),
        "app_base_url_set": settings.app_base_url != _DEFAULT_APP_BASE_URL,
        "request_on_loopback": loopback,
    }


_DEFAULT_APP_BASE_URL = "http://localhost:5173"


# ── Request an org (GRPH-1364) ─────────────────────────────────────────────────────────
ORG_REQUEST_NAME_MAX = 120
ORG_REQUEST_EMAIL_MAX = 254
ORG_REQUEST_NOTE_MAX = 2000


class OrgRequestUnavailable(Exception):
    """No operator address is configured, or the relay could not deliver."""


def request_org(db: Session, user, name: str, email: str, note: str = "") -> dict:
    """Relay a self-host user's org request to the operator, then stamp `org_requested_at`.

    The date is stored only after the relay succeeded, so the card never claims a request
    that nobody received. A repeat overwrites the date (there is no Declined/Expired state)."""
    from app.services import email as email_svc

    name, email, note = (name or "").strip(), (email or "").strip(), (note or "").strip()
    if not name or len(name) > ORG_REQUEST_NAME_MAX:
        raise ValueError(f"name is required, at most {ORG_REQUEST_NAME_MAX} characters")
    if not email or len(email) > ORG_REQUEST_EMAIL_MAX or "@" not in email or any(c in email for c in " \r\n"):
        raise ValueError("a valid contact email is required")
    if len(note) > ORG_REQUEST_NOTE_MAX:
        raise ValueError(f"note is at most {ORG_REQUEST_NOTE_MAX} characters")
    to = settings.org_request_recipients
    if not to:
        raise OrgRequestUnavailable("this deployment has no operator address to send an org request to")
    subject = f"Org request from {name}"
    text = f"Name: {name}\nContact: {email}\nAccount: {user.email}\n\n{note or '(no note)'}\n"
    if not all([email_svc.send_email(t, subject, text) for t in to]):
        raise OrgRequestUnavailable("the request could not be delivered; try again later")
    doc = dict(user.onboarding or {})
    doc["org_requested_at"] = datetime.now(timezone.utc).isoformat()
    user.onboarding = doc
    db.commit()
    return doc
