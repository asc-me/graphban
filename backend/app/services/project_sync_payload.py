"""Sync payload for one project (GRPH-P89 S2, D2).

``entity`` builds the S0 record ``{type, id, version, hash, body}``. ``body`` is
only the keys in ``BODY_ALLOW``. A new column on the model stays off the wire
until someone adds it here. ``hash`` is ``sha256:`` of that body's canonical
JSON (sorted keys, no extra whitespace), so the same content hashes the same.

Actors are a display label. The link stores no org member list, so an email
is never a label. API keys, enrolment codes, seat ids, embeddings, transcripts
and box settings are not fields of any allow-list, so they are not read.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Event, Item, Prd, Project, Request, RequestComment, SyncManifest, SyncOutbox, User,
)
from app.services.project_sync_outbox import version_in_range, wire_version
from app.services import items as items_svc
from app.services import prds as prds_svc
from app.services import waits as waits_svc
from app.services.project_sync_client import ENTITY_TYPES

# Top-level body keys, per entity type. The test pins this tuple. ``title`` is
# the item read's name; the rest are the fields the item names that
# ``item_dict`` does not itself render (description, acceptance, blocker,
# links, status history) plus the item_dict fields that are the work rather
# than who holds it. ``assignee``, ``claimed_by`` and ``built_by`` stay off:
# dumping ``item_dict`` as-is would put the box user and the seat on the wire.
ITEM_KEYS = (
    "title", "description", "acceptance", "evidence", "blocker", "links",
    "tags", "touchpoints", "status", "kind", "prd_id", "line_id", "status_history",
)
PRD_KEYS = (
    "body", "sections", "grill_state", "baseline_hash", "version", "coverage",
)
#: GBC-P23 holder fields (GRPH-1978). Not in ``ITEM_KEYS``: they go out only while the
#: project's effective level is ``full`` and the console has stored it (``holders_on``), and
#: only when there is a holder to name. ``assignee`` is ``{id, name}`` from the assignable
#: people list; ``held_by`` is ``{kind, label, owner?, agent_kind?}``. Never an email, an
#: agent or seat id, a claim token or an enrolment code.
HOLDER_KEYS = ("assignee", "held_by")
HELD_BY_KEYS = ("kind", "label", "owner", "agent_kind")

# Comments have no answer and waits do. Both types carry the same four keys
# so a new column on either model is still an edit to this tuple.
NOTE_KEYS = ("text", "answer", "timestamps", "actor")

BODY_ALLOW: dict[str, tuple[str, ...]] = {
    "item": ITEM_KEYS,
    "prd": PRD_KEYS,
    "comment": NOTE_KEYS,
    "wait": NOTE_KEYS,
}

# The rollup only. ``coverage()`` also returns per-item drift and titles;
# those are not the summary, and copying the whole dict would publish them.
_COVERAGE_KEYS = (
    "section_count", "implementable_sections", "sections_with_tasks", "gaps",
    "shaped", "empty_sections", "total_items", "done_items", "resolved_items",
    "percent_done", "open_high_fidelity",
)


def canonical(value) -> str:
    """Sorted-key JSON with no extra whitespace. Nested dicts sort too."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(body: dict) -> str:
    """``sha256:`` of the canonical body. The envelope is not part of the hash."""
    digest = hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()
    return "sha256:" + digest


def _project_id(db: Session, type: str, row) -> str:
    if type == "comment":
        request = db.get(Request, row.request_id)
        return request.project_id if request is not None else ""
    return row.project_id or ""


def _assigned_version(db: Session, project_id: str, type: str, entity_id: str,
                      body_hash: str) -> int:
    """A version in ``0..2^53-1`` that climbs when the body changes.

    The hash of the body stays in ``hash`` and is the no-op skip. The version
    is not that hash: a 63-bit hash is above the console's safe integer, and
    an unordered hash would let a newer edit sort below the stored one.
    """
    if not project_id:
        return wire_version(0, 1)
    manifest = db.get(SyncManifest, (project_id, type, entity_id))
    if (manifest is not None and manifest.hash == body_hash
            and version_in_range(manifest.version)):
        return int(manifest.version)
    confirmed = int(manifest.version) if manifest is not None else 0
    pending = db.get(SyncOutbox, (project_id, type, entity_id))
    captures = int(pending.version) if pending is not None and pending.version else 1
    return wire_version(confirmed, captures)


def entity(db: Session, type: str, id: str) -> dict | None:
    """One ``{type, id, version, hash, body}`` record, or None when ``id`` is not there.

    ``type`` outside the contract is a caller mistake and raises. None is only
    "this id is not an entity of that type", which is a different answer from
    an empty body.
    """
    if type not in BODY_ALLOW:
        raise ValueError(
            f"unknown sync entity type {type!r}; the contract types are "
            f"{', '.join(ENTITY_TYPES)}"
        )
    row = _load(db, type, id)
    if row is None:
        return None
    return _record(db, type, row)


def backfill(db: Session, project_id: str) -> list[dict] | None:
    """Every syncable entity in the project, in a stable order.

    None means the project does not exist. An empty list means it exists and
    has nothing to send — those two are not the same, and a caller that
    treated both as "synced" would report a missing project as caught up.
    """
    if db.get(Project, project_id) is None:
        return None
    records: list[dict] = []
    items = _items(db, project_id)
    for item in items:
        records.append(_record(db, "item", item))
    for prd in _prds(db, project_id):
        records.append(_record(db, "prd", prd))
    for comment in _comments(db, project_id):
        records.append(_record(db, "comment", comment))
    for item in items:
        if _is_wait(item):
            records.append(_record(db, "wait", item))
    return records


def backfill_bytes(db: Session, project_id: str) -> bytes | None:
    """The canonical JSON of ``backfill``, or None when the project is absent."""
    rows = backfill(db, project_id)
    if rows is None:
        return None
    return canonical(rows).encode("utf-8")


def allow(source: dict, type: str) -> dict:
    """The allow-listed keys only, each one required.

    A missing key raises. Dropping a key on the floor would make the body
    look like a shorter allow-list, and the exact-set test would be the only
    thing that noticed — if it noticed.
    """
    keys = BODY_ALLOW[type]
    missing = [key for key in keys if key not in source]
    if missing:
        raise KeyError(f"{type} payload is missing {', '.join(missing)}")
    return {key: source[key] for key in keys}


def _record(db: Session, type: str, row) -> dict:
    body = _body(db, type, row)
    digest = content_hash(body)
    entity_id = _public_id(type, row)
    return {
        "type": type,
        "id": entity_id,
        "version": _assigned_version(db, _project_id(db, type, row), type, entity_id, digest),
        "hash": digest,
        "body": body,
    }


def _body(db: Session, type: str, row) -> dict:
    if type == "item":
        return _item_body(db, row)
    if type == "prd":
        return _prd_body(db, row)
    if type == "comment":
        return _comment_body(db, row)
    return _wait_body(db, row)


def _item_body(db: Session, item: Item) -> dict:
    # Built from the item read, then cut to the allow-list. Returning
    # ``item_dict`` itself puts assignee and claimed_by on the wire; the leak
    # test is the check that this cut happened.
    rendered = items_svc.item_dict(item)
    source = {
        "title": rendered["title"],
        "description": item.description or "",
        "acceptance": _acceptance(item),
        "evidence": list(rendered.get("evidence") or []),
        "blocker": item.blocker or "",
        "links": _links(db, item),
        "tags": list(rendered.get("tags") or []),
        "touchpoints": list(rendered.get("touchpoints") or []),
        "status": rendered["status"],
        "kind": rendered.get("kind") or "",
        "prd_id": rendered.get("prd_id"),
        "line_id": rendered.get("line_id"),
        "status_history": _status_history(db, item),
    }
    body = allow(source, "item")
    if holders_allowed(db, item.project_id):
        body.update(holders(db, item))
    return body


def holders_allowed(db: Session, project_id: str | None) -> bool:
    """Does this project's effective level, as the console stored it, let holders out?"""
    from app.models import SyncProject
    from app.services import project_sync_lifecycle

    if not project_id:
        return False
    return project_sync_lifecycle.holders_on(db.get(SyncProject, project_id))


def _person(user: User) -> dict:
    """``{id, name}`` exactly as ``push_assignable_people`` names the person. A name that is
    itself an address is not a name: the handle (or the id) stands in."""
    name = (user.name or "").strip()
    if not name or _looks_email(name):
        name = (user.handle or "").strip() or user.id
    return {"id": user.id, "name": name}


def _assignable_by_handle(db: Session, project_id: str) -> dict[str, User]:
    from app.services import handles, triage

    out: dict[str, User] = {}
    for user in triage.assignable_people(db, project_id):
        for handle in {handles.project_handle(db, user, project_id), user.handle, user.id}:
            if handle:
                out.setdefault(handle, user)
    return out


def holders(db: Session, item: Item) -> dict:
    """``assignee`` and ``held_by`` for one item, each only when there is one to name.

    ``assignee`` is a person this project lists as assignable; an assignee that is an agent,
    or a handle nobody here holds, is not sent (its id would be an agent or seat id).
    ``held_by`` is the lease: an agent is named by its label, with ``owner`` (its key's user)
    and ``agent_kind`` (its declared vendor). The agent id itself never leaves.
    """
    from app.models import Agent, ApiKey

    people = _assignable_by_handle(db, item.project_id)
    out: dict = {}
    assignee = people.get((item.assignee or "").strip())
    if assignee is not None:
        out["assignee"] = _person(assignee)
    holder = (item.claimed_by or "").strip()
    if not holder:
        return out
    agent = db.get(Agent, holder)
    if agent is not None:
        label = (agent.label or "").strip()
        if not label or _looks_email(label) or holder in label:
            label = "agent"
        held: dict = {"kind": "agent", "label": label}
        key = db.get(ApiKey, agent.api_key_id) if agent.api_key_id else None
        owner = db.get(User, key.user_id) if key is not None and key.user_id else None
        if owner is not None:
            held["owner"] = _person(owner)
        vendor = (agent.capabilities or {}).get("vendor") if isinstance(
            agent.capabilities, dict) else None
        if isinstance(vendor, str) and vendor.strip():
            held["agent_kind"] = vendor.strip()
        out["held_by"] = held
        return out
    person = people.get(holder)
    if person is not None:
        out["held_by"] = {"kind": "person", "label": _person(person)["name"]}
    else:
        # A lease under a name this box cannot place (a key name, a removed agent). The
        # string itself may be a seat or key id, so only the fact of the hold goes out.
        out["held_by"] = {"kind": "agent", "label": "agent"}
    return out


def _prd_body(db: Session, prd: Prd) -> dict:
    baseline = prds_svc.baseline_of(db, prd.id)
    # None is "never baselined". An empty string would read as a hash of an
    # empty body, which is a different fact (a baseline that is empty).
    baseline_hash = prds_svc.body_hash(baseline.body) if baseline is not None else None
    full = prds_svc.coverage(db, prd)
    source = {
        "body": prd.body or "",
        "sections": prds_svc.parse_sections(prd.body or ""),
        "grill_state": _grill_state(db, prd),
        "baseline_hash": baseline_hash,
        "version": prd.version or "",
        "coverage": {key: full[key] for key in _COVERAGE_KEYS},
    }
    return allow(source, "prd")


def _comment_body(db: Session, comment: RequestComment) -> dict:
    source = {
        "text": comment.body or "",
        "answer": "",
        "timestamps": {"created_at": _iso(comment.created_at)},
        "actor": _display_actor(
            db, actor_id=comment.author_user_id or "", actor_label="",
        ) or (comment.author_kind or ""),
    }
    return allow(source, "comment")


def _wait_body(db: Session, item: Item) -> dict:
    decision = item.decision if isinstance(item.decision, dict) else {}
    answer = decision.get("answer") if isinstance(decision.get("answer"), dict) else {}
    by = answer.get("by") if isinstance(answer.get("by"), dict) else {}
    # ``by.agent`` is a seat or agent id. The label is the display name only.
    source = {
        "text": str(decision.get("question") or item.title or ""),
        "answer": _answer_text(decision),
        "timestamps": {
            "created_at": _iso(item.created_at),
            "answered_at": _iso_unix(answer.get("at")),
        },
        "actor": _display_actor(db, actor_label=str(by.get("user") or "")),
    }
    return allow(source, "wait")


def _acceptance(item: Item) -> list[str]:
    from app.services.fleet import extract_acceptance_clauses

    return extract_acceptance_clauses(item.description or "")


def _links(db: Session, item: Item) -> list[dict]:
    from app.models import Link

    rows = db.scalars(select(Link).where(
        (Link.a == item.id) | (Link.b == item.id)).order_by(Link.id)).all()
    return [{
        "type": link.type,
        "a": items_svc.render_ref(db, link.a),
        "b": items_svc.render_ref(db, link.b),
        "reason": link.reason or "",
    } for link in rows]


def _status_history(db: Session, item: Item) -> list[dict]:
    """Status transitions from the audit log, as label, time and status.

    The event's ``meta`` is not copied. It carries evidence and the actor's
    id, and copying it would publish both. A row with no status string is not
    a status change.
    """
    ids = [item.id] if item.key == item.id else [item.id, item.key]
    rows = db.scalars(select(Event).where(
        Event.target_type == "item",
        Event.target_id.in_(ids),
    ).order_by(Event.id)).all()
    out = []
    for row in rows:
        meta = row.meta or {}
        status = meta.get("status")
        if not isinstance(status, str) or status not in items_svc.STATUSES:
            continue
        out.append({
            "at": _iso(row.ts),
            "status": status,
            "actor": _display_actor(
                db, actor_id=row.actor_id or "", actor_label=row.actor_label or "",
            ),
        })
    return out


def _grill_state(db: Session, prd: Prd) -> dict:
    """Grill outcomes without the transcript.

    ``grill_state()`` returns every turn's text. That text is the transcript
    this payload does not carry. Counts and outcomes are the state.
    """
    from app.models import GrillTurn
    from sqlalchemy import func

    questions = db.scalar(select(func.count()).select_from(GrillTurn).where(
        GrillTurn.prd_id == prd.id, GrillTurn.role == "agent")) or 0
    done = prds_svc.completion(db, prd.id)
    dimensions = {
        name: {"outcome": row["outcome"]}
        for name, row in done["dimensions"].items()
    }
    answers = int(done["answers"])
    return {
        "questions": int(questions),
        "answers": answers,
        "grilled": answers > 0,
        "complete": bool(done["complete"]),
        "graded": bool(done["graded"]),
        "outstanding": list(done["outstanding"]),
        "deferred": list(done["deferred"]),
        "dimensions": dimensions,
    }


def _looks_email(value: str) -> bool:
    if "@" not in value or value.count("@") != 1:
        return False
    local, domain = value.split("@", 1)
    return bool(local) and "." in domain and " " not in value


def _display_actor(db: Session, *, actor_id: str = "", actor_label: str = "") -> str:
    """A display label, never a box user id.

    ``SyncLink`` has the engineer's ``linked_email`` and no org member list.
    Until that list is stored, an email is not a label: a known user is named
    by handle, and a label that is itself an address is dropped.
    """
    user = db.get(User, actor_id) if actor_id else None
    if user is not None:
        return (user.handle or user.name or "").strip()
    label = (actor_label or "").strip()
    if _looks_email(label):
        return ""
    return label


def _answer_text(decision: dict) -> str:
    answer = decision.get("answer")
    if not answer:
        return ""
    if isinstance(answer, str):
        return answer
    if not isinstance(answer, dict):
        return ""
    choice = str(answer.get("choice") or "")
    text = str(answer.get("text") or "")
    if choice and text:
        return f"{choice}: {text}"
    return choice or text


def _load(db: Session, type: str, id: str):
    from app.services import keys

    if type in ("item", "wait"):
        item = db.get(Item, keys.resolve_item(db, id) or id)
        if item is None:
            return None
        if type == "wait" and not _is_wait(item):
            return None
        return item
    if type == "prd":
        return db.get(Prd, keys.resolve_prd(db, id) or id)
    if type == "comment":
        return db.get(RequestComment, id)
    return None


def _public_id(type: str, row) -> str:
    if type == "comment":
        return row.id
    return row.key


def _is_wait(item: Item) -> bool:
    if isinstance(item.decision, dict) and item.decision:
        return True
    return bool(waits_svc.wait_tags_on(item))


def _items(db: Session, project_id: str) -> list[Item]:
    return list(db.scalars(
        select(Item).where(Item.project_id == project_id).order_by(Item.number, Item.id)
    ).all())


def _prds(db: Session, project_id: str) -> list[Prd]:
    return list(db.scalars(
        select(Prd).where(Prd.project_id == project_id).order_by(Prd.number, Prd.id)
    ).all())


def _comments(db: Session, project_id: str) -> list[RequestComment]:
    return list(db.scalars(
        select(RequestComment)
        .join(Request, Request.id == RequestComment.request_id)
        .where(Request.project_id == project_id)
        .order_by(RequestComment.created_at, RequestComment.id)
    ).all())


def _iso(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _iso_unix(value) -> str:
    if not isinstance(value, int):
        return ""
    return datetime.fromtimestamp(value, timezone.utc).isoformat()
