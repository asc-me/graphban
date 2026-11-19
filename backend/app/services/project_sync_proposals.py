"""Pull cloud proposals and apply them on the box (GRPH-1737, P89 S5).

The box is the authority. ``cloud_permission`` on the mapping decides what a
proposal may do, whatever the console sent. ``read_only`` refuses every
proposal, ``comments`` allows only comment and answer, ``proposals`` allows
all four kinds. Status ``done``, sign-off, an attestation waiver, closing a
PRD, and rules changes are refused in every permission, with a reason that
names the rule.

Exactly-once is the lookup at the top of ``apply_proposal``, not the primary
key. The receipt is upserted in the same transaction as the apply, so a
re-delivered id reports the stored outcome and does not apply again. Removing
that lookup applies the proposal a second time; an integrity error on the
second insert would roll the second apply back and hide the hole.

A failed outcome post stays ``reported=false`` and is retried. The apply is
not. The cursor advances only after every proposal on the page has a receipt,
so a crash before that commit re-delivers stored ids as no-ops.

``after_push`` is the hook a later flush calls. This module does not push.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Event, Item, SyncProject, SyncProposal
from app.services import items as items_svc
from app.services import project_sync_client as sync_client
from app.services import project_sync_outbox as outbox
from app.services import project_sync_payload as payload
from app.services import waits

# D11's max delay is the normal poll. D5 drops it while a decision is open.
NORMAL_POLL_SECONDS = 60
WAIT_POLL_SECONDS = 10

_KINDS = frozenset(sync_client.PROPOSAL_KINDS)
_COMMENT_ANSWER = frozenset({"comment", "answer"})
_EDIT_FIELDS = (
    "title", "description", "status", "tags", "effort", "blocker",
    "prd_section", "fidelity", "kind", "touchpoints",
)
_WAIVER_KEYS = ("attestation_waiver", "waive_attestation", "waiver")
_RULE_KEYS = ("rules", "rule_toggles", "toggles")
_RULE_TYPES = frozenset({"rule", "rules", "rule_toggle"})

REASON_READ_ONLY = "read_only refuses every proposal"
REASON_COMMENTS = "comments allows only comment and answer"
REASON_DONE = "status done is never accepted from the cloud (v1)"
REASON_SIGN_OFF = "sign_off is never accepted from the cloud (v1)"
REASON_WAIVER = "an attestation waiver is never accepted from the cloud (v1)"
REASON_PRD_CLOSE = "closing a PRD is never accepted from the cloud (v1)"
REASON_RULES = "rules changes are never accepted from the cloud (v1)"


class _HoldCommit:
    """Turn ``db.commit`` into ``flush`` for one service call.

    ``update_item``, ``create_item`` and ``answer_wait`` commit on their own.
    The receipt has to land in that same transaction, so those commits flush
    and the caller commits once, after the receipt is in the session.
    """

    def __init__(self, db: Session):
        self.db = db
        self._real = None

    def __enter__(self) -> _HoldCommit:
        self._real = self.db.commit

        def _flush():
            self.db.flush()

        self.db.commit = _flush  # type: ignore[method-assign]
        return self

    def __exit__(self, *exc: object) -> bool:
        self.db.commit = self._real  # type: ignore[method-assign]
        return False


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def poll_interval(db: Session, project_id: str) -> int:
    """Seconds until the next pull. An open decision shortens it (D5).

    The caller is ``due``. A project with no open decision waits 60s. One
    with an unanswered ``wait:decision`` waits 10s. Both are measured from
    the last poll, not from when the wait opened or closed.
    """
    if waits.open_decision_waits(db, project_id):
        return WAIT_POLL_SECONDS
    return NORMAL_POLL_SECONDS


def due(db: Session, now: datetime) -> list[SyncProject]:
    """Sync-level projects with a cloud id whose proposal pull is due at ``now``.

    Due when ``proposal_pull_now`` is set (a push just happened), when this
    project has never been polled, or when ``polled_at + poll_interval`` has
    arrived. ``now`` is the caller's clock, so a test can move it.
    """
    moment = _aware(now)
    rows = db.scalars(
        select(SyncProject).where(SyncProject.sync_level == outbox.SYNC_LEVEL_SYNC)
    ).all()
    ready: list[SyncProject] = []
    for row in rows:
        if not row.cloud_project_id:
            continue
        if row.proposal_pull_now or row.proposal_polled_at is None:
            ready.append(row)
            continue
        polled = _aware(row.proposal_polled_at)
        interval = poll_interval(db, row.local_project_id)
        if polled + timedelta(seconds=interval) <= moment:
            ready.append(row)
    return ready


def after_push(db: Session, local_project_id: str) -> bool:
    """Mark one project due for a proposal pull on the next pass.

    The flush that pushes entities does not live here. It calls this after a
    push so the pull runs immediately instead of waiting out the interval.
    A project that is not at ``sync``, or that has no cloud id, is not marked:
    there is nothing to pull. Returns whether the flag was set.
    """
    row = db.get(SyncProject, local_project_id)
    if row is None or row.sync_level != outbox.SYNC_LEVEL_SYNC or not row.cloud_project_id:
        return False
    row.proposal_pull_now = True
    db.commit()
    return True


def run_due(db: Session, now: datetime, *,
            transport: httpx.BaseTransport | None = None) -> list[dict]:
    """Pull and apply for every project ``due`` at ``now``."""
    outcomes: list[dict] = []
    for project in list(due(db, now)):
        outcomes.extend(pull_project(db, project, now=now, transport=transport))
    return outcomes


def pull_project(db: Session, project: SyncProject, *, now: datetime,
                 transport: httpx.BaseTransport | None = None) -> list[dict]:
    """One pull for one mapping. The cursor moves only after every receipt is stored.

    ``polled_at`` is committed before the apply, including when the console
    answers ``refused`` or ``unknown``, so a miss does not tight-spin.
    A 304 keeps the cursor. Queued outcomes are posted either way.
    """
    local_id = project.local_project_id
    cloud_id = project.cloud_project_id or ""
    if not cloud_id:
        return []
    row = db.get(SyncProject, local_id)
    if row is None:
        return []
    cursor = row.proposal_cursor or ""
    etag = row.proposal_etag
    row.proposal_polled_at = _aware(now)
    row.proposal_pull_now = False
    db.commit()

    result = sync_client.pull_proposals(
        db, project=cloud_id, after=cursor, etag=etag, transport=transport,
    )
    if result.status != "ok":
        _report_pending(db, local_id, transport)
        return []
    if result.not_modified:
        if result.etag:
            row = db.get(SyncProject, local_id)
            if row is not None:
                row.proposal_etag = result.etag
                db.commit()
        _report_pending(db, local_id, transport)
        return []

    outcomes: list[dict] = []
    for proposal in result.proposals:
        row = db.get(SyncProject, local_id)
        if row is None:
            break
        outcomes.append(apply_proposal(db, row, proposal, transport=transport))
    row = db.get(SyncProject, local_id)
    if row is not None:
        if isinstance(result.cursor, str):
            row.proposal_cursor = result.cursor
        if result.etag:
            row.proposal_etag = result.etag
        db.commit()
    _report_pending(db, local_id, transport)
    return outcomes


def apply_proposal(db: Session, project: SyncProject, proposal: dict, *,
                   transport: httpx.BaseTransport | None = None) -> dict:
    """Apply one proposal, or report the outcome already stored for its id.

    The lookup below is the exactly-once guard. The receipt write is an
    upsert on purpose: a second insert that raised on the primary key would
    roll the second apply back, and deleting this lookup would not fail the
    duplicate-comment test.
    """
    if not isinstance(proposal, dict):
        return {"id": "", "status": "refused", "reason": "proposal is not an object"}
    proposal_id = proposal.get("id")
    if not isinstance(proposal_id, str) or not proposal_id:
        return {"id": "", "status": "refused", "reason": "proposal has no id"}

    existing = db.get(SyncProposal, proposal_id)
    if existing is not None:
        outcome = _outcome_dict(existing)
        _send(db, [outcome], transport)
        return outcome

    try:
        outcome = _decide(db, project, proposal_id, proposal)
    except ValueError as exc:
        db.rollback()
        outcome = _persist(
            db, project.local_project_id, proposal_id, "refused", str(exc), None,
        )
        db.commit()
    _send(db, [outcome], transport)
    return outcome


def _decide(db: Session, project: SyncProject, proposal_id: str, proposal: dict) -> dict:
    kind = proposal.get("kind") if isinstance(proposal.get("kind"), str) else ""
    payload_body = proposal.get("payload") if isinstance(proposal.get("payload"), dict) else {}
    target = proposal.get("target") if isinstance(proposal.get("target"), dict) else None
    reason = _permission_reason(project.cloud_permission, kind) or _never_reason(target, payload_body)
    if reason:
        outcome = _persist(db, project.local_project_id, proposal_id, "refused", reason, None)
        db.commit()
        return outcome
    if kind == "edit":
        return _apply_edit(db, project, proposal_id, proposal, target, payload_body)
    if kind == "comment":
        return _apply_comment(db, project, proposal_id, proposal, target, payload_body)
    if kind == "answer":
        return _apply_answer(db, project, proposal_id, proposal, target, payload_body)
    if kind == "create":
        return _apply_create(db, project, proposal_id, proposal, payload_body)
    outcome = _persist(
        db, project.local_project_id, proposal_id, "refused",
        f"unknown proposal kind {kind!r}", None,
    )
    db.commit()
    return outcome


def _permission_reason(permission: str | None, kind: str) -> str | None:
    """What this mapping refuses before any field is read as an apply.

    An unknown permission refuses everything. A missing value is not
    ``comments`` and not ``proposals``.
    """
    if permission == outbox.CLOUD_PERMISSION_READ_ONLY:
        return REASON_READ_ONLY
    if permission == outbox.CLOUD_PERMISSION_COMMENTS:
        if kind not in _COMMENT_ANSWER:
            return REASON_COMMENTS
        return None
    if permission == outbox.CLOUD_PERMISSION_PROPOSALS:
        if kind not in _KINDS:
            return f"unknown proposal kind {kind!r}"
        return None
    return f"cloud permission {permission!r} refuses every proposal"


def _never_reason(target: dict | None, body: dict) -> str | None:
    """Rules the cloud cannot cross in v1, in every permission that allows the kind."""
    action = body.get("action") if isinstance(body.get("action"), str) else ""
    if body.get("sign_off") or action == "sign_off":
        return REASON_SIGN_OFF
    if any(key in body for key in _WAIVER_KEYS):
        return REASON_WAIVER
    evidence = body.get("evidence")
    if isinstance(evidence, list):
        for entry in evidence:
            if isinstance(entry, dict) and entry.get("kind") == "attestation":
                return REASON_WAIVER
    target_type = target.get("type") if isinstance(target, dict) else None
    if target_type in _RULE_TYPES or any(key in body for key in _RULE_KEYS):
        return REASON_RULES
    if target_type == "prd" and (
        body.get("close") or body.get("close_prd") or action in ("close", "close_prd")
        or body.get("status") in ("closed", "done")
    ):
        return REASON_PRD_CLOSE
    if body.get("status") == "done":
        return REASON_DONE
    return None


def _apply_edit(db: Session, project: SyncProject, proposal_id: str, proposal: dict,
                target: dict | None, body: dict) -> dict:
    if isinstance(target, dict) and target.get("type") not in (None, "item", "wait"):
        raise ValueError("only item edits are applied from the cloud (v1)")
    item = _require_item(db, project, target)
    record = payload.entity(db, "item", item.key)
    if record is None:
        raise ValueError("target not found")
    current = record["version"]
    base = proposal.get("base_version")
    if not _is_int(base) or base != current:
        outcome = _persist(
            db, project.local_project_id, proposal_id, "conflict", None, current,
        )
        db.commit()
        return outcome
    fields = _edit_fields(body)
    if not fields:
        raise ValueError("edit names no field the box applies")
    actor_label = _actor_label(proposal)
    with _HoldCommit(db):
        updated = items_svc.update_item(db, item.id, **fields)
        if updated is None:
            raise ValueError("target not found")
        _history(db, item=updated, action="update_item", proposal_id=proposal_id,
                 actor_label=actor_label, meta={"fields": sorted(fields)})
        outcome = _persist(
            db, project.local_project_id, proposal_id, "applied", None, None,
        )
    db.commit()
    return outcome


def _edit_fields(body: dict) -> dict:
    fields = {}
    for key in _EDIT_FIELDS:
        if key not in body or body[key] is None:
            continue
        value = body[key]
        if key == "effort":
            value = int(value)
        elif key == "tags":
            if not isinstance(value, list) or not all(isinstance(tag, str) for tag in value):
                raise ValueError("tags must be a list of strings")
        elif key == "touchpoints":
            if not isinstance(value, list) or not all(isinstance(path, str) for path in value):
                raise ValueError("touchpoints must be a list of strings")
        elif key in ("title", "description", "status", "blocker", "prd_section", "fidelity", "kind"):
            if not isinstance(value, str):
                raise ValueError(f"{key} must be a string")
        fields[key] = value
    return fields


def _apply_comment(db: Session, project: SyncProject, proposal_id: str, proposal: dict,
                   target: dict | None, body: dict) -> dict:
    item = _require_item(db, project, target)
    text = _text(body, "text", "body")
    if not text:
        raise ValueError("a comment needs text")
    actor_label = _actor_label(proposal)
    with _HoldCommit(db):
        _history(db, item=item, action="comment", proposal_id=proposal_id,
                 actor_label=actor_label, meta={"text": text})
        outcome = _persist(
            db, project.local_project_id, proposal_id, "applied", None, None,
        )
    db.commit()
    return outcome


def _apply_answer(db: Session, project: SyncProject, proposal_id: str, proposal: dict,
                  target: dict | None, body: dict) -> dict:
    item = _require_item(db, project, target)
    choice = body.get("choice") if isinstance(body.get("choice"), str) else ""
    if not choice and isinstance(body.get("option"), str):
        choice = body["option"]
    text = _text(body, "text", "body")
    actor = SimpleNamespace(handle=_actor_label(proposal), id="", name=_actor_label(proposal))
    with _HoldCommit(db):
        waits.answer_wait(
            db, item.id, choice=choice, text=text, actor=actor, surface="sync",
            proposal_id=proposal_id,
        )
        outcome = _persist(
            db, project.local_project_id, proposal_id, "applied", None, None,
        )
    db.commit()
    return outcome


def _apply_create(db: Session, project: SyncProject, proposal_id: str, proposal: dict,
                  body: dict) -> dict:
    title = body.get("title") if isinstance(body.get("title"), str) else ""
    if not title.strip():
        raise ValueError("a create needs a title")
    description = body.get("description") if isinstance(body.get("description"), str) else ""
    tags = body.get("tags") if isinstance(body.get("tags"), list) else None
    if tags is not None and not all(isinstance(tag, str) for tag in tags):
        raise ValueError("tags must be a list of strings")
    status = body.get("status") if isinstance(body.get("status"), str) and body.get("status") else "backlog"
    actor_label = _actor_label(proposal)
    with _HoldCommit(db):
        item = items_svc.create_item(
            db, title=title.strip(), description=description, tags=tags, status=status,
            project_id=project.local_project_id, commit=False,
        )
        _history(db, item=item, action="create_item", proposal_id=proposal_id,
                 actor_label=actor_label, meta={"title": item.title})
        outcome = _persist(
            db, project.local_project_id, proposal_id, "applied", None, None,
        )
    db.commit()
    return outcome


def _require_item(db: Session, project: SyncProject, target: dict | None) -> Item:
    from app.services import keys

    if not isinstance(target, dict):
        raise ValueError("target not found")
    ident = target.get("id")
    if not isinstance(ident, str) or not ident:
        raise ValueError("target not found")
    item = db.get(Item, keys.resolve_item(db, ident) or ident)
    if item is None or item.project_id != project.local_project_id:
        raise ValueError("target not found")
    return item


def _text(body: dict, *keys: str) -> str:
    for key in keys:
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _actor_label(proposal: dict) -> str:
    proposer = proposal.get("proposer") if isinstance(proposal.get("proposer"), dict) else {}
    label = proposer.get("label") if isinstance(proposer.get("label"), str) else ""
    return f"cloud:{label}"


def _history(db: Session, *, item: Item, action: str, proposal_id: str,
             actor_label: str, meta: dict) -> Event:
    """One item-history row. Written here, not via ``events.record``, which commits.

    ``note_event`` captures the same change for the outbox. It does not commit.
    """
    stored = {**meta, "proposal_id": proposal_id}
    event = Event(
        actor_type="cloud", actor_id="", actor_label=actor_label, surface="sync",
        action=action, target_type="item", target_id=item.key, project_id=item.project_id,
        meta=stored,
    )
    db.add(event)
    db.flush()
    outbox.note_event(
        db, project_id=item.project_id, target_type="item", target_id=item.key,
        action=action, meta=stored,
    )
    return event


def _persist(db: Session, project_id: str, proposal_id: str, status: str,
             reason: str | None, current_version: int | None) -> dict:
    """Upsert the receipt. A second call updates the row; it does not insert another."""
    row = db.get(SyncProposal, proposal_id)
    if row is None:
        row = SyncProposal(
            id=proposal_id, project_id=project_id, status=status, reason=reason,
            current_version=current_version, reported=False,
        )
        db.add(row)
    else:
        row.project_id = project_id
        row.status = status
        row.reason = reason
        row.current_version = current_version
        row.reported = False
    db.flush()
    return _outcome_dict(row)


def _outcome_dict(row: SyncProposal) -> dict:
    outcome: dict = {"id": row.id, "status": row.status}
    if row.reason:
        outcome["reason"] = row.reason
    if row.current_version is not None:
        outcome["current_version"] = row.current_version
    return outcome


def _send(db: Session, outcomes: list[dict], transport: httpx.BaseTransport | None) -> None:
    """Post outcomes. ``ok`` marks those rows reported. Anything else leaves them queued."""
    if not outcomes:
        return
    result = sync_client.report_outcomes(db, outcomes=outcomes, transport=transport)
    if result.status != "ok":
        return
    for outcome in outcomes:
        row = db.get(SyncProposal, outcome["id"])
        if row is not None and row.status == outcome["status"]:
            row.reported = True
    db.commit()


def _report_pending(db: Session, project_id: str,
                    transport: httpx.BaseTransport | None) -> None:
    rows = db.scalars(select(SyncProposal).where(
        SyncProposal.project_id == project_id,
        SyncProposal.reported.is_(False),
    )).all()
    _send(db, [_outcome_dict(row) for row in rows], transport)
