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
import re
import uuid
from datetime import datetime, timedelta, timezone

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (Agent, AttemptTelemetry, Delegation, Enrolment,
                        HarnessRollup, Item)

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
                  adapter: str | None = None,
                  resolution: dict | None = None) -> AttemptTelemetry:
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
                 "resolution": resolution,
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


# ---- rollups and the page's read (D11, D6, D5) ------------------------------------------------

#: A rate needs this many finished attempts before it counts (PRD-37 `MIN_SAMPLE`). The floor is
#: a rule about READING a number, never about whether a week happened — which is why the rollup
#: below writes thin weeks and this constant is applied at the read.
FLOOR = 5

#: Above this share of one sampling reason, a rate carries the "sampled by preference" badge.
#: Visual only: it never alters a denominator, and the rules read the same undivided rate.
SKEW_SHARE = 0.8

#: Below this share of attempts reporting tokens, the cost proxy is not shown at all. A partial
#: numerator over a full denominator makes a vendor that prints nothing look cheap.
COST_COVERAGE = 0.8

CELL_KEYS = ("vendor", "model", "binary_version", "lane", "tier", "task_class", "size_band")


def week_of(when: datetime) -> str:
    """ISO year-week, e.g. `2026-W37`. The week a rollup row is a fact about."""
    year, week, _ = _aware(when).isocalendar()
    return f"{year}-W{week:02d}"


def _cell_of(row: AttemptTelemetry) -> tuple:
    return (row.vendor or "", row.model or "", row.binary_version or "", row.lane or "",
            row.tier_requested or "", row.task_class or "", row.size_band or "")


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def roll(db: Session, project_id: str, *, weeks: set[str] | None = None) -> int:
    """Recompute `harness_rollups` from the raw rows. Returns the number of rows written.

    Recomputed, never folded into: a rollup that cannot be reproduced from what it summarises
    is a number nobody can check, and that is the whole reason `attempt_telemetry` is kept far
    longer than any chart needs it. Deleting the weeks first is what makes a re-roll idempotent
    rather than cumulative.

    Weeks with attempts are written even below the floor. A week with NO attempts is left
    absent, because no attempts and a zero rate are different claims and the chart draws the
    absence as a gap.
    """
    rows = db.scalars(select(AttemptTelemetry).where(
        AttemptTelemetry.project_id == project_id,
        AttemptTelemetry.derived_at.is_not(None))).all()
    buckets: dict[tuple, dict] = {}
    for row in rows:
        week = week_of(row.derived_at)
        if weeks is not None and week not in weeks:
            continue
        cell = buckets.setdefault((week, *_cell_of(row)), {
            "finished": 0, "signed_off": 0, "bounced": 0, "seconds": [],
            "tokens_in": 0, "tokens_out": 0, "tokens_reported": 0, "signed_off_reported": 0,
            "first_choice": 0, "fallback": 0, "explicit": 0, "unknown": 0})
        cell["finished"] += 1
        cell["signed_off"] += 1 if row.outcome == "signed_off" else 0
        cell["bounced"] += 1 if row.outcome == "bounced" else 0
        if row.claim_to_finish_s is not None:
            cell["seconds"].append(float(row.claim_to_finish_s))
        # Tokens are SUMMED over the attempts that reported, with the count of those attempts
        # beside them. No average is stored, which is what keeps "not reported" from becoming
        # a zero the moment a week is aggregated.
        if row.tokens_in is not None or row.tokens_out is not None:
            cell["tokens_in"] += row.tokens_in or 0
            cell["tokens_out"] += row.tokens_out or 0
            cell["tokens_reported"] += 1
            cell["signed_off_reported"] += 1 if row.outcome == "signed_off" else 0
        cell[row.sampled if row.sampled in SAMPLED else "unknown"] += 1

    touched = weeks if weeks is not None else {k[0] for k in buckets}
    existing = db.scalars(select(HarnessRollup).where(
        HarnessRollup.project_id == project_id)).all()
    for old in existing:
        if weeks is None or old.week in touched:
            db.delete(old)
    db.flush()
    now = _now()
    for (week, vendor, model, version, lane, tier, task_class, band), cell in buckets.items():
        db.add(HarnessRollup(
            project_id=project_id, week=week, vendor=vendor, model=model,
            binary_version=version, lane=lane, tier=tier, task_class=task_class,
            size_band=band, finished=cell["finished"], signed_off=cell["signed_off"],
            bounced=cell["bounced"],
            median_seconds=(int(_median(cell["seconds"])) if cell["seconds"] else None),
            tokens_in=cell["tokens_in"] or None, tokens_out=cell["tokens_out"] or None,
            tokens_reported=cell["tokens_reported"],
            signed_off_reported=cell["signed_off_reported"],
            first_choice=cell["first_choice"], fallback=cell["fallback"],
            explicit=cell["explicit"], unknown=cell["unknown"], rolled_at=now))
    db.flush()
    return len(buckets)


def roll_if_stale(db: Session, project_id: str) -> int:
    """Re-roll only the weeks whose raw rows have moved since they were last rolled.

    The nightly job is the ordinary path; this is what keeps a page honest between runs
    without recomputing a year of weeks to answer one request.
    """
    rolled: dict[str, datetime] = {
        r.week: _aware(r.rolled_at) for r in db.scalars(select(HarnessRollup).where(
            HarnessRollup.project_id == project_id)).all()}
    stale: set[str] = set()
    for row in db.scalars(select(AttemptTelemetry).where(
            AttemptTelemetry.project_id == project_id,
            AttemptTelemetry.derived_at.is_not(None))).all():
        week = week_of(row.derived_at)
        seen = max(t for t in (_aware(row.derived_at), _aware(row.reported_at)) if t)
        if week not in rolled or rolled[week] is None or rolled[week] < seen:
            stale.add(week)
    # A week whose raw rows are all gone (retention) leaves a rollup nothing refreshes; that is
    # correct — the rollup is the surviving fact — so `stale` only ever names weeks with rows.
    return roll(db, project_id, weeks=stale) if stale else 0


def _version_key(version: str) -> tuple:
    """Order versions numerically where they look numeric, alphabetically where they do not.

    `0.23.0` before `0.100.0` is the whole point; a string sort puts them the other way round
    and the page would then default to a version that is not the current one.
    """
    parts = re.split(r"[.\-+]", version or "")
    out: list = []
    for part in parts:
        out.append((0, int(part), "") if part.isdigit() else (1, 0, part))
    return (len(out) > 0, tuple(out))


def _skew(sampling: dict) -> dict | None:
    """The lopsidedness of a cell's sampling, or None when nothing dominates.

    A badge, not a correction: the rate beside it counts every attempt in the cell, and the
    counts are shown so a reader can do their own arithmetic. What it says is "this number is
    honest about what happened and is not a fair comparison against a cell chosen differently".
    """
    total = sum(sampling.values())
    if not total:
        return None
    reason, count = max(sampling.items(), key=lambda kv: kv[1])
    share = count / total
    return {"reason": reason, "share": round(share, 3)} if share > SKEW_SHARE else None


def _cost(tokens_in: int, tokens_out: int, reported: int, signed_off_reported: int,
          finished: int) -> dict:
    """Tokens per signed-off item, or a stated refusal to compare.

    Suppressed below `COST_COVERAGE`, and the refusal carries the two counts rather than a
    shrug, because "3 of 11 attempts reported tokens" is a fact a reader can act on and
    "unavailable" is not.
    """
    coverage = (reported / finished) if finished else 0.0
    if not reported or coverage < COST_COVERAGE:
        return {"comparable": False, "reported": reported, "finished": finished,
                "reason": f"not comparable: {reported} of {finished} attempts reported tokens"}
    if not signed_off_reported:
        return {"comparable": False, "reported": reported, "finished": finished,
                "reason": "no signed-off attempt reported tokens"}
    return {"comparable": True, "reported": reported, "finished": finished,
            "tokens_per_signed_off": round((tokens_in + tokens_out) / signed_off_reported, 1),
            "tokens_in": tokens_in, "tokens_out": tokens_out}


def report(db: Session, project_id: str, *, window_days: int | None = None,
           versions: str = "current") -> dict:
    """The Harness page's whole read: one entry per cell, each with its weekly series.

    `versions="current"` keeps only the newest `binary_version` seen for each vendor+model and
    names the others in `versions_seen`, which is what "defaults to the current version and
    shows both on request" means (criterion 7). Every cell carries `n`, `below_floor`, its
    sampling counts and skew badge, and a cost proxy that says when it will not compare.
    """
    roll_if_stale(db, project_id)
    window = WINDOW_DAYS if window_days is None else window_days
    cutoff = week_of(_now() - timedelta(days=window))
    rows = [r for r in db.scalars(select(HarnessRollup).where(
        HarnessRollup.project_id == project_id)).all() if r.week >= cutoff]

    cells: dict[tuple, dict] = {}
    for row in rows:
        key = (row.vendor, row.model, row.binary_version, row.lane, row.tier,
               row.task_class, row.size_band)
        cell = cells.setdefault(key, {
            "finished": 0, "signed_off": 0, "bounced": 0, "tokens_in": 0, "tokens_out": 0,
            "tokens_reported": 0, "signed_off_reported": 0,
            "sampling": {r: 0 for r in SAMPLED}, "series": [], "medians": []})
        cell["finished"] += row.finished
        cell["signed_off"] += row.signed_off
        cell["bounced"] += row.bounced
        cell["tokens_in"] += row.tokens_in or 0
        cell["tokens_out"] += row.tokens_out or 0
        cell["tokens_reported"] += row.tokens_reported
        cell["signed_off_reported"] += row.signed_off_reported or 0
        for reason in SAMPLED:
            cell["sampling"][reason] += getattr(row, reason)
        if row.median_seconds is not None:
            cell["medians"].append(float(row.median_seconds))
        cell["series"].append({
            "week": row.week, "finished": row.finished, "signed_off": row.signed_off,
            "rate": round(row.signed_off / row.finished, 3) if row.finished else None,
            # Every point carries its own floor verdict. A thin week is drawn grey and left
            # unconnected rather than dropped, because dropping it would let a reader join two
            # solid points across a gap that was never measured.
            "below_floor": row.finished < FLOOR,
            "median_seconds": row.median_seconds,
        })

    # "Current" is per vendor+model, not per cell: one binary runs every lane, and picking the
    # newest version separately in each cell would show two versions side by side and call
    # both current.
    newest: dict[tuple, str] = {}
    for (vendor, model, version, *_rest) in cells:
        seen = newest.get((vendor, model))
        if seen is None or _version_key(version) > _version_key(seen):
            newest[(vendor, model)] = version
    versions_seen: dict[tuple, list[str]] = {}
    for (vendor, model, version, *_rest) in cells:
        versions_seen.setdefault((vendor, model), [])
        if version not in versions_seen[(vendor, model)]:
            versions_seen[(vendor, model)].append(version)

    out = []
    for key, cell in sorted(cells.items()):
        vendor, model, version, lane, tier, task_class, band = key
        if versions == "current" and version != newest[(vendor, model)]:
            continue
        out.append({
            "key": dict(zip(CELL_KEYS, key)),
            "finished": cell["finished"],
            "signed_off": cell["signed_off"],
            "bounced": cell["bounced"],
            "rate": round(cell["signed_off"] / cell["finished"], 3) if cell["finished"] else None,
            "below_floor": cell["finished"] < FLOOR,
            "sampling": cell["sampling"],
            "skew": _skew(cell["sampling"]),
            "median_seconds": (int(_median(cell["medians"])) if cell["medians"] else None),
            "cost": _cost(cell["tokens_in"], cell["tokens_out"], cell["tokens_reported"],
                          cell["signed_off_reported"], cell["finished"]),
            "versions_seen": sorted(versions_seen[(vendor, model)], key=_version_key),
            "is_current_version": version == newest[(vendor, model)],
            "series": sorted(cell["series"], key=lambda p: p["week"]),
        })
    return {
        "project_id": project_id,
        "window_days": window,
        "versions": versions,
        "floor": FLOOR,
        "skew_share": SKEW_SHARE,
        "generated_at": _now().isoformat(),
        "cells": out,
        # Named rather than left to be counted off a list the page may have filtered. A fleet
        # whose every cell is thin is the ordinary state of a small instance, and the page has
        # to be able to say so instead of looking empty.
        "below_floor_count": sum(1 for c in out if c["below_floor"]),
    }


# ---- recommendations: what a person has seen, and what a cell teaches (D7, D8) ---------------

#: PRD-16 calls this out on the shard; the Lessons page scores it like any other lesson.
LESSON_SOURCE = "harness-telemetry"


def _lesson_key(cell: dict) -> str:
    """The dedup key: the cell WITHOUT its binary version.

    A point release is not a new thing to learn, so 0.23.0 and 0.100.0 of one harness share a
    mark. Crossing the floor is an event in a cell's life, not a level it can re-enter.
    """
    k = cell["key"]
    return ":".join([k["vendor"], k["model"], k["lane"], k["tier"], k["task_class"],
                     k["size_band"]])


def marks_for(db: Session, *, user_id: str, scope: str, scope_id: str) -> dict[str, "RecommendationMark"]:
    from app.models import RecommendationMark

    rows = db.scalars(select(RecommendationMark).where(
        RecommendationMark.user_id == user_id,
        RecommendationMark.scope == scope,
        RecommendationMark.scope_id == scope_id)).all()
    return {r.card_key: r for r in rows}


def mark_card(db: Session, *, user_id: str, scope: str, scope_id: str, card_key: str,
              evidence_hash: str, action: str) -> "RecommendationMark":
    """Record that this person has seen this card at this evidence (D7).

    Accept and dismiss share the row deliberately. Both mean "stay quiet until the numbers
    move", and two tables would let one of them forget the hash rule — which is the half that
    makes an accepted recommendation come back when its evidence reverses.
    """
    from app.models import RecommendationMark

    if action not in ("accept", "dismiss"):
        raise AttemptRefused(f"unknown action {action!r}", status=422)
    row = db.scalar(select(RecommendationMark).where(
        RecommendationMark.user_id == user_id,
        RecommendationMark.scope == scope,
        RecommendationMark.scope_id == scope_id,
        RecommendationMark.card_key == card_key))
    if row is None:
        row = RecommendationMark(id=f"rm_{uuid.uuid4().hex[:12]}", user_id=user_id, scope=scope,
                                 scope_id=scope_id, card_key=card_key,
                                 evidence_hash=evidence_hash)
        db.add(row)
    row.evidence_hash = evidence_hash
    if action == "accept":
        row.accepted_at = _now()
        row.dismissed_at = None
    else:
        row.dismissed_at = _now()
        row.accepted_at = None
    db.flush()
    return row


def draft_lesson(db: Session, project_id: str, *, text: str, provenance: dict,
                 origin: str) -> str | None:
    """Write a lesson CANDIDATE into the PRD-16 review inbox. Never publishes.

    `auto_triage=False` on purpose: the scorer that publishes candidates on similarity has no
    business acting on a number this PRD produced, and "nothing is published by this PRD" is
    a claim that has to be enforced at the call rather than asserted in a docstring.
    """
    from app.services import memory as memory_svc

    try:
        shard = memory_svc.add_memory(
            db, text_body=text, scope="global", source=LESSON_SOURCE, project_id=project_id,
            status="candidate", origin=origin, auto_triage=False, fresh=False)
    except Exception:  # noqa: BLE001 — a lesson draft must never fail the read that produced it
        logger.exception("harness: lesson draft failed for %s", project_id)
        return None
    logger.info("harness: drafted lesson %s for %s (%s)", shard.id, project_id, provenance)
    return shard.id


def lessons_for_crossings(db: Session, project_id: str, report: dict) -> list[str]:
    """A cell crossing the floor for the first time drafts one candidate (D8).

    The mark is permanent, which is what makes "first" mean first: a cell that dips back under
    the floor and returns drafts nothing, a new binary version inherits the mark, and a
    rejected candidate does not come back the next night. A lesson that re-drafts itself after
    a human said no is nagging dressed as learning.
    """
    from app.models import HarnessLessonMark

    existing = {m.cell_key for m in db.scalars(select(HarnessLessonMark).where(
        HarnessLessonMark.project_id == project_id)).all()}
    drafted: list[str] = []
    for cell in report["cells"]:
        if cell["below_floor"] or cell["rate"] is None:
            continue
        key = _lesson_key(cell)
        if key in existing:
            continue
        k = cell["key"]
        runner = _runner_up_cell(report, cell)
        text = (f"For {k['lane']}/{k['task_class']} items of size {k['size_band']} at tier "
                f"{k['tier']} in the last {report['window_days']} days, "
                f"{k['vendor']}:{k['model']} signed off {cell['signed_off']}/{cell['finished']}"
                + (f"; {_cell_label(runner)} signed off "
                   f"{runner['signed_off']}/{runner['finished']}." if runner else "."))
        shard_id = draft_lesson(db, project_id, text=text,
                                provenance={"cell": k, "why": "crossed the sample floor"},
                                origin="agent:harness-telemetry")
        db.add(HarnessLessonMark(id=f"hlm_{uuid.uuid4().hex[:12]}", project_id=project_id,
                                 cell_key=key, first_crossed_at=_now(), shard_id=shard_id))
        existing.add(key)
        if shard_id:
            drafted.append(shard_id)
    db.flush()
    return drafted


def _cell_label(cell: dict) -> str:
    return f"{cell['key']['vendor']}:{cell['key']['model']}"


def _runner_up_cell(report: dict, cell: dict) -> dict | None:
    """The best OTHER vendor:model measured in the same lane, tier, class and band.

    A lesson that named only the winner would be a recommendation without an alternative, and
    the reader could not tell whether the number was good or merely the only one there is.
    """
    k = cell["key"]
    rivals = [c for c in report["cells"]
              if c is not cell and not c["below_floor"] and c["rate"] is not None
              and (c["key"]["lane"], c["key"]["tier"], c["key"]["task_class"],
                   c["key"]["size_band"]) == (k["lane"], k["tier"], k["task_class"],
                                              k["size_band"])
              and _cell_label(c) != _cell_label(cell)]
    return max(rivals, key=lambda c: c["rate"]) if rivals else None
