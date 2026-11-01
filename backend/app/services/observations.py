"""The observations ledger (PRD-63 S1 / D2): typed facts about how work went.

Every observation is derived from a STRUCTURED record the server already holds — a sabotage
receipt's `tests_failed`, an attestation predicate's `passed`, a status move, a typed refusal's
`rule_id`. Free text never scores: a bounce reason is not read, and a bounce with no structured
fact behind it records nothing. A row stores ids, predicate names and counts, never copied text,
so it is safe to aggregate (D8).

Two writers share one set of derivers: the live hooks at the points those records are created,
and `backfill`, which walks history. They produce the same `dedupe_key` for the same fact, so
running the backfill over live data adds nothing and running it twice adds nothing.

Recording never raises into the caller (the same contract as `events.record`): an observation
failing to write must not fail the sign-off or refusal it describes.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Event, Item, Observation, Project
from app.services import items as items_svc
from app.services.rules import STAGE_IDS

logger = logging.getLogger("graphban.observations")

#: kind -> the stage whose check it bears on (PRD-61 §4 vocabulary).
KINDS: dict[str, str] = {
    "bounce": "review",
    "reviewer_sabotage_caught": "review",
    "reviewer_sabotage_missed": "review",
    "ci_red_after_sign_off": "merge",
    "reopened_after_done": "merge",
    "predicate_exempted": "review",
    "gap_noted_at_sign_off": "review",
    "refusal": "unknown",  # taken from the refusing rule's id, see `_refusal_stage`
    "evidence_missing": "build",
}

#: A checked engine a migration-class item is expected to have been run on.
_MIGRATION_PATH = "alembic/versions/"
_CHECK_POSTGRES = "postgres"
#: "Postgres NOT run" is the commonest way a declared check goes missing and the gate's denial
#: scan (`fleet.denial_in`) does not cover it. Used ONLY inside `_postgres_evidence_present`.
_NOT_RUN = re.compile(r"\bnot (?:run|ran|executed)\b|\bskipped\b", re.I)


class BadFilter(ValueError):
    pass


def _short(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]


def _refusal_stage(rule_id: str | None) -> str:
    head = (rule_id or "").split(".", 1)[0]
    return head if head in STAGE_IDS else "unknown"


def _add(db: Session, item: Item, *, kind: str, key: str, origin: str, stage: str | None = None,
         rule_id: str | None = None, commit: str | None = None, ref: dict | None = None,
         occurred_at: datetime | None = None, project_id: str | None = None) -> bool:
    """Insert one observation unless its fact is already recorded. True when it was new.

    A savepoint around the insert, so losing a race to the unique key (a live write and a
    backfill describing the same fact) rolls back only this row.
    """
    pid = project_id or (item.project_id if item is not None else None)
    if not pid:
        return False
    if db.scalar(select(Observation.id).where(
            Observation.project_id == pid, Observation.dedupe_key == key)) is not None:
        return False
    try:
        with db.begin_nested():
            db.add(Observation(
                project_id=pid, item_id=item.id if item is not None else None, kind=kind,
                stage=stage or KINDS[kind], rule_id=rule_id, commit=commit, dedupe_key=key,
                origin=origin, ref=ref or {}, occurred_at=occurred_at or datetime.now(timezone.utc)))
        return True
    except IntegrityError:
        return False


# ---- derivers: one item's state -> the facts it implies ---------------------------------------
# Each yields (kind, key, commit, ref). None reads the free text of a note, a detail or a
# bounce reason; the one place a receipt's prose is consulted is `_postgres_evidence_present`,
# which asks whether a declared check has a receipt at all and says so there.

def _signoff_commits(item: Item) -> list[str]:
    """The commits a reviewer's sign-off attested (the `fleet.sign_off` adapter)."""
    return [a.get("commit") for a in items_svc.attestation_receipts(item.evidence)
            if a.get("adapter") == "fleet.sign_off" and a.get("commit")]


def _exempted(item: Item):
    """`adversarial_evidence` passed on a sign-off attestation, for an item below the effort
    threshold: the predicate passed by exemption rather than by evidence."""
    from app.services import fleet as fleet_svc

    if fleet_svc.needs_adversarial_evidence(item):
        return
    for a in items_svc.attestation_receipts(item.evidence):
        if a.get("adapter") != "fleet.sign_off":
            continue
        for q in a.get("predicates") or []:
            if q.get("name") == "adversarial_evidence" and q.get("passed") is True:
                yield ("predicate_exempted", f"exempt:adversarial_evidence:{item.id}:{a['commit']}",
                       a["commit"], {"predicate": "adversarial_evidence",
                                     "effort": item.effort or 0,
                                     "threshold": fleet_svc.ADVERSARIAL_EFFORT_THRESHOLD})


def _ci_red(item: Item):
    """A failed `suite_green` at a commit a reviewer had signed off."""
    signed = _signoff_commits(item)
    if not signed:
        return
    for a in items_svc.attestation_receipts(item.evidence):
        if a.get("adapter") == "fleet.sign_off":
            continue
        commit = a.get("commit") or ""
        if not any(items_svc.same_commit(commit, s) for s in signed):
            continue
        for q in a.get("predicates") or []:
            if q.get("name") == items_svc.CI_PREDICATE and q.get("passed") is False:
                yield ("ci_red_after_sign_off", f"ci_red:{item.id}:{commit[:12]}", commit,
                       {"predicate": items_svc.CI_PREDICATE, "adapter": a.get("adapter")})


def _gaps(item: Item):
    """Builder notes carrying the structured `done: false` flag, still on the item at sign-off.
    Counted, not quoted: the note's prose never leaves the item."""
    n = sum(1 for e in item.evidence or []
            if isinstance(e, dict) and e.get("kind") == "note" and e.get("done") is False)
    if n and item.reviewed_by:
        yield ("gap_noted_at_sign_off", f"gap:{item.id}", None, {"notes": n})


def _postgres_evidence_present(item: Item) -> bool:
    """Whether a `test` receipt names a Postgres run that does not deny itself.

    A presence check on a DECLARED check (a migration-class item must be run on the real
    engine), not scoring of prose: it asks "is there a receipt for this check", and reuses the
    denial scan the sign-off gate already applies to acceptance clauses for "...and it was
    not run". The observation records the check's name only.
    """
    from app.services import fleet as fleet_svc

    for e in item.evidence or []:
        if not isinstance(e, dict) or e.get("kind") != "test":
            continue
        line = str(e.get("detail") or "")
        if _CHECK_POSTGRES in line.lower() and not _NOT_RUN.search(line) \
                and not fleet_svc.denial_in(_CHECK_POSTGRES, line):
            return True
    return False


def _evidence_missing(item: Item):
    if any(_MIGRATION_PATH in (t or "") for t in item.touchpoints or []) \
            and not _postgres_evidence_present(item):
        yield ("evidence_missing", f"evidence_missing:{_CHECK_POSTGRES}:{item.id}", None,
               {"check": _CHECK_POSTGRES})


def _sabotages(item, receipts):
    for e in receipts:
        if not isinstance(e, dict) or e.get("kind") != "sabotage":
            continue
        failed = e.get("tests_failed") or 0
        kind = "reviewer_sabotage_caught" if failed else "reviewer_sabotage_missed"
        yield (kind, f"sabotage:{item.id}:{_short(str(e.get('claim')), str(e.get('mutation')))}", None,
               {"tests_failed": failed})


def _write(db: Session, item: Item, facts, origin: str) -> int:
    n = 0
    for kind, key, commit, ref in facts:
        n += _add(db, item, kind=kind, key=key, origin=origin, commit=commit, ref=ref)
    return n


# ---- live hooks -------------------------------------------------------------------------------

def _safe(db: Session, what: str, fn) -> None:
    """Run one hook, committing what it added; a failure is logged and rolled back."""
    try:
        fn()
        db.commit()
    except Exception:  # noqa: BLE001 — recording must never break the operation it describes
        logger.exception("failed to record %s observations", what)
        db.rollback()


def on_sign_off(db: Session, item: Item, reviewer_receipts) -> None:
    """After a sign-off: the reviewer's own sabotage receipts (only THIS call's evidence is
    attributable to the reviewer), the exemption, a gap still on the item, and a missing check."""
    def go():
        _write(db, item, _sabotages(item, reviewer_receipts or []), "live")
        _write(db, item, _exempted(item), "live")
        _write(db, item, _gaps(item), "live")
        _write(db, item, _evidence_missing(item), "live")
    _safe(db, "sign-off", go)


def on_evidence(db: Session, item: Item) -> None:
    """After evidence lands on an item: a red suite on a head that was already signed off."""
    _safe(db, "evidence", lambda: _write(db, item, _ci_red(item), "live"))


def on_reopen(db: Session, item: Item) -> None:
    """An item left `done`. The ordinal makes a second reopening a second fact."""
    def go():
        n = db.scalar(select(func.count(Observation.id)).where(
            Observation.item_id == item.id, Observation.kind == "reopened_after_done")) or 0
        _add(db, item, kind="reopened_after_done", key=f"reopen:{item.id}:{n + 1}", origin="live",
             ref={"to_status": item.status})
    _safe(db, "reopen", go)


def on_bounce(db: Session, item: Item) -> None:
    """A bounce scores only through a structured fact attached to it: a predicate that ran and
    failed, or a sabotage that broke nothing. Its reason text is never read, and a bounce with
    neither records nothing."""
    def go():
        facts = {}
        failing = sorted(items_svc.failing_predicates(item.evidence))
        if failing:
            facts["predicate_failed"] = failing
        missed = len(items_svc.vacuous_sabotages(item.evidence))
        if missed:
            facts["sabotage_missed"] = missed
        if not facts:
            return
        n = db.scalar(select(func.count(Observation.id)).where(
            Observation.item_id == item.id, Observation.kind == "bounce")) or 0
        _add(db, item, kind="bounce", key=f"bounce:{item.id}:{n + 1}", origin="live", ref=facts)
    _safe(db, "bounce", go)


def on_refusal(db: Session, event: Event) -> None:
    """A typed refusal (PRD-61 §6) that names its rule. Unmapped refusals have no rule to imply."""
    meta = event.meta or {}
    rule_id = meta.get("rule_id")
    if event.action != "refusal" or not rule_id or not event.project_id:
        return
    item = db.get(Item, event.target_id) if event.target_type == "item" and event.target_id else None
    _safe(db, "refusal", lambda: _add(
        db, item, kind="refusal", key=f"refusal:event:{event.id}", origin="live",
        stage=_refusal_stage(rule_id), rule_id=rule_id, project_id=event.project_id,
        ref={"where": meta.get("where"), "status": meta.get("status"), "event_id": event.id},
        occurred_at=event.ts))


# ---- backfill ---------------------------------------------------------------------------------

def backfill(db: Session, project_id: str) -> dict:
    """Derive observations from a project's history. Idempotent; returns what it added by kind.

    What it does NOT backfill, and why — each is a thing a stored record cannot support:
    sabotage verdicts (a stored receipt carries no author, so a builder's would be recorded as
    a reviewer's), `reopened_after_done` (the audit event does not hold the prior status) and
    `bounce` (the facts a bounce rests on are already observations in their own right).
    """
    added: dict[str, int] = {}

    def count(kind_facts, item):
        for kind, key, commit, ref in kind_facts:
            if _add(db, item, kind=kind, key=key, origin="backfill", commit=commit, ref=ref):
                added[kind] = added.get(kind, 0) + 1

    items = db.scalars(select(Item).where(
        Item.project_id == project_id, Item.status.in_(["review", "done"]))).all()
    for item in items:
        if item.reviewed_by:
            count(_exempted(item), item)
            count(_ci_red(item), item)
            count(_gaps(item), item)
        count(_evidence_missing(item), item)
    for e in db.scalars(select(Event).where(
            Event.project_id == project_id, Event.action == "refusal")):
        rule_id = (e.meta or {}).get("rule_id")
        if not rule_id:
            continue
        item = db.get(Item, e.target_id) if e.target_type == "item" and e.target_id else None
        if _add(db, item, kind="refusal", key=f"refusal:event:{e.id}", origin="backfill",
                stage=_refusal_stage(rule_id), rule_id=rule_id, project_id=project_id,
                ref={"where": (e.meta or {}).get("where"), "status": (e.meta or {}).get("status"),
                     "event_id": e.id}, occurred_at=e.ts):
            added["refusal"] = added.get("refusal", 0) + 1
    db.commit()
    return {"project_id": project_id, "added": added, "total_added": sum(added.values())}


# ---- read API ---------------------------------------------------------------------------------

def _out(o: Observation) -> dict:
    return {"id": o.id, "project_id": o.project_id, "item_id": o.item_id, "kind": o.kind,
            "stage": o.stage, "rule_id": o.rule_id, "commit": o.commit, "origin": o.origin,
            "ref": o.ref or {},
            "occurred_at": (o.occurred_at.replace(tzinfo=timezone.utc)
                            if o.occurred_at.tzinfo is None else o.occurred_at).isoformat()}


def org_project_ids(db: Session, org_id: str) -> list[str]:
    return list(db.scalars(select(Project.id).where(Project.org_id == org_id)))


def list_observations(db: Session, *, project_ids: list[str], stage: str | None = None,
                      kind: str | None = None, limit: int = 100, offset: int = 0) -> dict:
    """Newest first, with the per-kind counts of the whole filtered set (not just the page)."""
    if kind is not None and kind not in KINDS:
        raise BadFilter(f"unknown kind {kind!r}; one of {', '.join(KINDS)}")
    if stage is not None and stage not in (*STAGE_IDS, "unknown"):
        raise BadFilter(f"unknown stage {stage!r}; one of {', '.join(STAGE_IDS)}")
    conds = [Observation.project_id.in_(project_ids)] if project_ids else [Observation.id < 0]
    if stage:
        conds.append(Observation.stage == stage)
    if kind:
        conds.append(Observation.kind == kind)
    counts = dict(db.execute(select(Observation.kind, func.count(Observation.id))
                             .where(*conds).group_by(Observation.kind)).all())
    rows = db.scalars(select(Observation).where(*conds)
                      .order_by(Observation.occurred_at.desc(), Observation.id.desc())
                      .limit(limit).offset(offset)).all()
    return {"total": sum(counts.values()), "counts": counts, "observations": [_out(o) for o in rows]}


def count_for_project(db: Session, project_id: str) -> int:
    return db.scalar(select(func.count(Observation.id)).where(
        Observation.project_id == project_id)) or 0
