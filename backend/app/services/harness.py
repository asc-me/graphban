"""What a finished delegation says about the harness that ran it (PRD-38).

PRD-37 gave the supervisor a way to choose a harness and explain the choice, and gave the
ledger one bit per attempt to judge it by: signed off, or not. This module is the record that
makes that bit comparable — what kind of work the attempt was, who ran it, how it ended, and
**how it came to be sampled**, which is the field the rest of the design leans on. PRD-35
named the bias and PRD-37 sharpened it: the harness a profile prefers gets the samples, so a
rate over those samples measures the preference unless the record says where they came from.

Two writers, no ordering between them (D3):

- **The server derives** at the outcome event, from what the ledger already holds.
- **The supervisor posts** what only it can see — the resolution before the child starts, the
  binary version and turn count and tokens after it exits.

Either half may arrive first and neither waits for the other. What has arrived is legible from
`derived_at` and `reported_at`, so a number that nobody reported reads as "not reported"
instead of as a zero — the distinction this repository keeps having to relearn.

Only FINISHED delegations keep a row (D1). A launch post whose child never claimed anything is
swept: an attempt that never ran teaches nothing about the harness, and counting it would put
the supervisor's failures in the harness's column.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Agent, AttemptTelemetry, Delegation, Enrolment, Item

logger = logging.getLogger(__name__)

#: D2. `other` is not a failure of the mapper, it is the mapper's honest output for a reason it
#: does not recognise, and the page shows its count so the coverage is a number rather than a
#: silent default.
BOUNCE_CATEGORIES = ("tests", "scope", "quality", "process", "other")

#: Matched against the LOWERCASED bounce reason, first hit wins. English only and deliberately
#: so: there is no language detection here, and a reason this misses is `other` rather than a
#: guess. Ordered by how SPECIFIC the word is to a category rather than by how common it is:
#: the artifact words go first, so "wrong branch" is process while a bare "wrong" is the
#: judgement word that quality catches. The order is a defensible reading of English, not a
#: measurement, which is the whole reason nothing downstream reads this field.
_BOUNCE_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("tests", ("test", "spec", "suite", "coverage", "ci ", "ci fail", "red build", "lint")),
    ("process", ("branch", "commit", "pr ", "pull request", "worktree", "merge", "conflict",
                 "evidence", "checklist", "docs", "migration")),
    ("scope", ("scope", "out of scope", "unrelated", "extra change", "not asked", "missing",
               "incomplete", "did not implement", "half")),
    ("quality", ("quality", "bug", "wrong", "incorrect", "broken", "regression", "unsafe",
                 "race", "leak", "naming", "readab")),
)

#: D2's size band. Touchpoints and description length are the only difficulty proxies the
#: server has; both are what the delegator wrote, not a judgement the server invented.
SIZE_S_TOUCHPOINTS, SIZE_S_CHARS = 2, 600
SIZE_L_TOUCHPOINTS, SIZE_L_CHARS = 6, 2400

#: D9. The resolver reads a trailing window so a stale cell ages out instead of anchoring a
#: choice forever. A constant, not a setting: every explanation would otherwise carry a
#: parameter, and the explanation is the feature.
WINDOW_DAYS = 90

SAMPLED = ("first_choice", "fallback", "explicit", "unknown")


class AttemptRefused(Exception):
    """A post the server will not take. `status` is the HTTP code the router raises."""

    def __init__(self, message: str, *, status: int = 404) -> None:
        super().__init__(message)
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def bounce_category(reason: str | None) -> str:
    """Total into `BOUNCE_CATEGORIES`. Never null for a bounce, never a guess.

    Nothing downstream reads this — `sampled` comes from the launch post, every rate is
    signed-off over finished, and no recommendation rule takes it as input. It is a breakdown
    for a person reading a cell, and it is stated here so that no threshold is later wired to
    a field whose input is free text a reviewer typed in a hurry.
    """
    text = (reason or "").strip().lower()
    if not text:
        return "other"
    for category, words in _BOUNCE_WORDS:
        if any(word in text for word in words):
            return category
    return "other"


def size_band(item: Item | None) -> str:
    """`S`, `M` or `L` from what the delegator wrote down. A proxy, and named as one."""
    if item is None:
        return "M"
    touchpoints = len(item.touchpoints or [])
    chars = len(item.description or "")
    if touchpoints >= SIZE_L_TOUCHPOINTS or chars >= SIZE_L_CHARS:
        return "L"
    if touchpoints <= SIZE_S_TOUCHPOINTS and chars <= SIZE_S_CHARS:
        return "S"
    return "M"


def _declared(db: Session, row: Delegation) -> tuple[str, str]:
    """The child's declared vendor and model, in the same terms `delegation.measured` uses."""
    from app.services.delegation import UNDECLARED

    agent = db.get(Agent, row.agent_id) if row.agent_id else None
    caps = (agent.capabilities or {}) if agent is not None else {}
    vendor = caps.get("vendor") if isinstance(caps.get("vendor"), str) and caps.get("vendor") else UNDECLARED
    model = row.declared_model or ("" if vendor != UNDECLARED else UNDECLARED)
    return vendor, model


def _vendor_of(chosen: str | None) -> str:
    return (chosen or "").split(":", 1)[0]


def sampled_from(*, declared_vendor: str, declared_model: str, winner: str | None,
                 runner_up: str | None, source: str | None) -> str:
    """How this attempt came to be sampled (D2).

    `unknown` when no launch post arrived, and that is a value the page shows rather than a
    gap it hides: a supervisor that could not reach the server has not turned every attempt
    into somebody's first choice.
    """
    if source == "explicit":
        return "explicit"
    if not winner:
        return "unknown"
    declared = f"{declared_vendor}:{declared_model}"
    if declared == winner:
        return "first_choice"
    if runner_up and declared == runner_up:
        return "fallback"
    return "unknown"


def _attempt_no(db: Session, row: Delegation) -> int:
    finished = _aware(row.finished_at) or _now()
    prior = db.scalars(select(Delegation).where(Delegation.item_id == row.item_id)).all()
    # Tie-broken on the id, because `on_outcome` finishes every linked row on the item in one
    # pass and gives them the same timestamp — two rows each counting the other would make
    # both of them attempt 2, and no attempt 1 would exist.
    here = (finished, row.id)
    earlier = [
        r for r in prior
        if r.id != row.id and r.outcome is not None
        and ((_aware(r.finished_at) or finished), r.id) < here
    ]
    return len(earlier) + 1


def _row_for(db: Session, *, delegation_id: str | None = None,
             enrolment_id: str | None = None) -> AttemptTelemetry | None:
    if delegation_id:
        found = db.scalar(select(AttemptTelemetry).where(
            AttemptTelemetry.delegation_id == delegation_id))
        if found is not None:
            return found
    if enrolment_id:
        return db.scalar(select(AttemptTelemetry).where(
            AttemptTelemetry.enrolment_id == enrolment_id))
    return None


def _merge(row: AttemptTelemetry, values: dict) -> bool:
    """D3's merge rule, in one place so no caller can forget half of it.

    A post merges non-null values in and NEVER writes a null over a value. A differing
    non-null value wins, because a supervisor that read the child's result record twice is
    likelier right the second time — and a repost that changes something must be visible in
    `report_count` rather than silently applied or silently dropped.
    """
    changed = False
    for field, value in values.items():
        if value is None:
            continue
        if getattr(row, field) != value:
            setattr(row, field, value)
            changed = True
    return changed


def derive(db: Session, row: Delegation) -> AttemptTelemetry | None:
    """Write this finished attempt's half of the record. Called at the outcome event.

    Flushes rather than commits: the caller's transaction owns the outcome this describes,
    and a telemetry row that survived a rolled-back sign-off would be a measurement of
    something that did not happen.
    """
    if row.outcome is None:
        return None
    item = db.get(Item, row.item_id) if row.item_id else None
    telemetry = _row_for(db, delegation_id=row.id,
                         enrolment_id=_enrolment_of(db, row))
    created = telemetry is None
    if created:
        telemetry = AttemptTelemetry(id=f"at_{uuid.uuid4().hex[:12]}")
        db.add(telemetry)
    vendor, model = _declared(db, row)
    claimed, finished = _aware(row.claimed_at), _aware(row.finished_at)
    telemetry.delegation_id = row.id
    telemetry.project_id = row.project_id
    telemetry.item_id = row.item_id
    telemetry.vendor = vendor
    telemetry.model = model
    telemetry.lane = row.lane
    telemetry.tier_requested = row.requested_tier
    telemetry.tier_declared = row.declared_tier
    telemetry.task_class = task_class(item)
    telemetry.size_band = size_band(item)
    telemetry.attempt_no = _attempt_no(db, row)
    telemetry.outcome = row.outcome
    telemetry.bounce_category = (bounce_category(getattr(item, "bounce_reason", None))
                                 if row.outcome == "bounced" else None)
    telemetry.claim_to_finish_s = (int((finished - claimed).total_seconds())
                                   if claimed and finished and finished >= claimed else None)
    telemetry.sampled = sampled_from(
        declared_vendor=vendor, declared_model=model, winner=telemetry.chosen_winner,
        runner_up=telemetry.chosen_runner_up, source=telemetry.chosen_source)
    # The child said one vendor and the supervisor launched another. Flagged on the row and
    # counted, never resolved: neither side is trusted over the other here, and quietly
    # preferring one would be a guess wearing a fact's clothes. An UNDECLARED child is not a
    # mismatch — it is GRPH-732's other failure, and it already has its own cell.
    from app.services.delegation import UNDECLARED

    launched = _vendor_of(telemetry.chosen_winner)
    telemetry.declaration_mismatch = bool(
        launched and vendor != UNDECLARED and vendor != launched)
    telemetry.derived_at = _now()
    db.flush()
    return telemetry


def task_class(item: Item | None) -> str:
    """The brief's checklist, which is the only task class the ledger already computes."""
    from app.services import delegation as delegation_svc

    if item is None:
        return "general"
    return delegation_svc.checklist_for(item.touchpoints) or "general"


def _enrolment_of(db: Session, row: Delegation) -> str | None:
    """The seat this delegation's child registered on, which is how a launch post addressed
    the row before any delegation was linked to it."""
    agent = db.get(Agent, row.agent_id) if row.agent_id else None
    if agent is not None and getattr(agent, "enrolment_id", None):
        return agent.enrolment_id
    seat = db.scalar(select(Enrolment).where(Enrolment.delegation_id == row.id))
    return seat.id if seat is not None else None


def purge_unfinished(db: Session, project_id: str | None) -> int:
    """Drop runtime-only rows whose delegation never finished (D1).

    A launch post creates a row before anything has happened; if the child never claims, or
    claims and is superseded, no outcome ever arrives and the row would sit forever as an
    attempt with no ending. `expired` is derived from the delegation and the clock rather than
    stored, so there is no sweep to hang this on — it runs at the outcome event instead, which
    is both bounded and the moment new rows appear.
    """
    from app.services import delegation as delegation_svc

    stmt = select(AttemptTelemetry).where(AttemptTelemetry.derived_at.is_(None))
    if project_id:
        stmt = stmt.where(AttemptTelemetry.project_id == project_id)
    dropped = 0
    for row in db.scalars(stmt).all():
        delegation = db.get(Delegation, row.delegation_id) if row.delegation_id else None
        if delegation is None:
            seat = db.get(Enrolment, row.enrolment_id) if row.enrolment_id else None
            expires = _aware(getattr(seat, "expires_at", None)) if seat is not None else None
            # A seat that expired a day ago is not about to produce a child. Nothing shorter:
            # a seat expires in half an hour and the child it minted may still be working.
            if seat is None or (expires and expires < _now() - timedelta(days=1)):
                db.delete(row)
                dropped += 1
            continue
        if delegation.outcome is None and delegation_svc.state(delegation) in ("expired", "closed"):
            db.delete(row)
            dropped += 1
    if dropped:
        db.flush()
    return dropped


# ---- the supervisor's two posts (D3) ---------------------------------------------------------

@dataclass(frozen=True)
class Target:
    """What a post is about, resolved before anything is written.

    Exists so the ROUTER never touches `Delegation` or `Enrolment` itself — the layering
    `test_routers_do_not_touch_the_delegation_model` pins. It carries `project_id` because
    that is the only thing the router needs in order to decide whether this credential may
    write here, and `kind` because the two shapes of the post are not interchangeable.
    """

    kind: str  # seat | delegation
    project_id: str | None
    seat: Enrolment | None = None
    delegation: Delegation | None = None


def target_for(db: Session, *, enrolment_code: str | None = None,
               enrolment_id: str | None = None,
               delegation_id: str | None = None) -> Target | None:
    """Resolve a post's address, or None when it names nothing that exists.

    None is deliberately the same answer for "no such id" and "an id in a project you cannot
    see" once the caller applies its write check: a refusal that distinguished them would let
    a credential enumerate ids by the shape of the error.
    """
    from app.services.fleet import _hash_code

    if enrolment_code:
        seat = db.scalar(select(Enrolment).where(
            Enrolment.code_hash == _hash_code(enrolment_code)))
        return Target("seat", seat.project_id, seat=seat) if seat is not None else None
    if enrolment_id:
        seat = db.get(Enrolment, enrolment_id)
        return Target("seat", seat.project_id, seat=seat) if seat is not None else None
    if delegation_id:
        row = db.get(Delegation, delegation_id)
        return Target("delegation", row.project_id, delegation=row) if row is not None else None
    return None


def record_launch(db: Session, *, target: Target, winner: str | None,
                  runner_up: str | None = None, source: str | None = None,
                  adapter: str | None = None) -> AttemptTelemetry:
    """What the supervisor resolved, posted before the child starts.

    Keyed by the SEAT, because at launch there is no delegation to key on: the planner minted
    the seat, the supervisor resolved afterwards, and the child has not claimed anything yet.
    The row this creates is half a record until an outcome arrives, and `purge_unfinished`
    removes it if none ever does.
    """
    seat = target.seat
    if seat is None:
        raise AttemptRefused("a launch post names a seat", status=422)
    row = _row_for(db, enrolment_id=seat.id)
    if row is None and seat.delegation_id:
        row = _row_for(db, delegation_id=seat.delegation_id)
    if row is None:
        row = AttemptTelemetry(id=f"at_{uuid.uuid4().hex[:12]}", enrolment_id=seat.id,
                               project_id=seat.project_id, item_id=seat.item_id)
        db.add(row)
    row.enrolment_id = row.enrolment_id or seat.id
    _merge(row, {"chosen_winner": winner, "chosen_runner_up": runner_up,
                 "chosen_source": source, "adapter_launched": adapter,
                 "project_id": seat.project_id, "item_id": seat.item_id})
    row.report_count = (row.report_count or 0) + 1
    row.reported_at = _now()
    # A launch post that arrives after the outcome must not leave `sampled` at what it was
    # derived to be with no winner to compare against.
    if row.derived_at is not None and row.delegation_id:
        delegation = db.get(Delegation, row.delegation_id)
        if delegation is not None:
            vendor, model = _declared(db, delegation)
            row.sampled = sampled_from(declared_vendor=vendor, declared_model=model,
                                       winner=row.chosen_winner, runner_up=row.chosen_runner_up,
                                       source=row.chosen_source)
    db.flush()
    return row


def record_exit(db: Session, *, target: Target, values: dict) -> AttemptTelemetry:
    """The runtime facts only the supervisor saw, posted at child exit.

    Addressed by the delegation when the caller knows it and by the SEAT when it does not —
    which is the ordinary case, because the supervisor holds the seat's row id from the
    roster and never the delegation's. Both find the same row: the launch post created it
    under the seat, and the outcome derivation later binds the delegation to it.

    Idempotent by the merge rule rather than by refusing a second post: a supervisor that
    restarts and re-reports is doing the right thing, and a route that answered it with an
    error would train it to stop.
    """
    delegation, seat = target.delegation, target.seat
    if delegation is None and seat is None:
        raise AttemptRefused("an exit post names a delegation or a seat", status=422)
    if delegation is None and seat is not None and seat.delegation_id:
        delegation = db.get(Delegation, seat.delegation_id)
    row = _row_for(db,
                   delegation_id=delegation.id if delegation is not None else None,
                   enrolment_id=(seat.id if seat is not None
                                 else (_enrolment_of(db, delegation) if delegation else None)))
    if row is None:
        row = AttemptTelemetry(
            id=f"at_{uuid.uuid4().hex[:12]}",
            delegation_id=delegation.id if delegation is not None else None,
            enrolment_id=seat.id if seat is not None else None,
            project_id=(delegation.project_id if delegation is not None
                        else seat.project_id if seat is not None else None),
            item_id=(delegation.item_id if delegation is not None
                     else seat.item_id if seat is not None else None))
        db.add(row)
    if delegation is not None:
        row.delegation_id = delegation.id
    if seat is not None and row.enrolment_id is None:
        row.enrolment_id = seat.id
    _merge(row, values)
    row.report_count = (row.report_count or 0) + 1
    row.reported_at = _now()
    db.flush()
    return row


def row_dict(row: AttemptTelemetry) -> dict:
    """What the route echoes back. Nulls stay null: a token count nobody reported is not zero."""
    return {
        "id": row.id,
        "delegation_id": row.delegation_id,
        "enrolment_id": row.enrolment_id,
        "project_id": row.project_id,
        "item_id": row.item_id,
        "vendor": row.vendor,
        "model": row.model,
        "binary_version": row.binary_version,
        "lane": row.lane,
        "tier_requested": row.tier_requested,
        "task_class": row.task_class,
        "size_band": row.size_band,
        "attempt_no": row.attempt_no,
        "sampled": row.sampled,
        "declaration_mismatch": row.declaration_mismatch,
        "outcome": row.outcome,
        "bounce_category": row.bounce_category,
        "claim_to_finish_s": row.claim_to_finish_s,
        "turns_used": row.turns_used,
        "turn_budget": row.turn_budget,
        "wall_seconds": row.wall_seconds,
        "tokens_in": row.tokens_in,
        "tokens_out": row.tokens_out,
        "exit_meaning": row.exit_meaning,
        "derived": row.derived_at is not None,
        "reported": row.reported_at is not None,
        "report_count": row.report_count,
    }
