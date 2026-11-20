"""Observing rules: would-have outcomes, the decision nudge, promotion (GRPH-1777 / P91 D5-D6).

A rule an adopter starts is `observing`: on, measured, not enforced. Where a SERVER-enforced
primitive (`exclusive_areas` in `fleet.claim_cluster`, `evidence_required`, `done_predicates`
and `sabotage_receipt` in `items.py` / `fleet.py`) would hold or refuse, an observing rule does
neither; it records a `RuleObservation` row (`would_hold` | `would_refuse`) and lets the call
through. Supervisor-read primitives are S6.

- **Nothing is re-evaluated in bulk.** Adopt, promote and lock change a stored value; the next
  check reads it.
- **History is the rule's.** Rows name the rule, never the proposer or adopter, and promotion
  or removal never purges them (D6: evaluation never reads who proposed or adopted).
- **Promote is a person's act** (`rules.person_adopts`): REST only, with a reason.
- **The nudge never promotes.** `decision_due` is reported once `DUE_DAYS` days have passed or
  `DUE_OUTCOMES` would-have outcomes exist, whichever first; the rule stays observing until a
  person acts.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import Validation
from app.models import CustomRule, RuleObservation, RuleToggle, utcnow
from app.services import events as events_svc
from app.services import rule_toggles as T

OUTCOMES = ("would_hold", "would_refuse", "would_allow")
#: GRPH-1778 (S6): rules whose primitive is READ by a brief or a doc, not enforced by anything
#: that can measure a refusal. Observing, they are stated as "observing, not applied" and their
#: would-have count is UNMEASURED (None), never 0.
UNMEASURED_RULES = ("build.test_scope", "plan.path_classes", "review.staff_every_row")
#: Supervisor-enforced rules whose would-have outcome `gbfleet` posts to the observations route,
#: with the outcomes each may post: a loosening would have let a file through, a tightening
#: would have held. `review.staff_every_row` widens staffing to rows this wave did not cut, which the
#: route cannot record, so it is UNMEASURED too.
SUPERVISOR_OUTCOMES = {
    "build.generated_files": ("would_allow",),
    "build.undeclared_files": ("would_hold",),
}
DUE_DAYS = 14
DUE_OUTCOMES = 10


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_observing(db: Session, rule_id: str, project_id: str | None) -> bool:
    return T.observing_since(db, rule_id, project_id) is not None


def record(db: Session, project_id: str | None, rule_id: str, outcome: str, *,
           item_id: str | None = None, commit: str | None = None,
           detail: dict | None = None) -> RuleObservation | None:
    """Record one would-have outcome (flushed, not committed: the caller's call carries on and
    commits). The same outcome for the same item and detail is one row, so a retried claim does
    not count twice."""
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown would-have outcome {outcome!r}")
    if not project_id:
        return None
    detail = detail or {}
    for prior in db.scalars(select(RuleObservation).where(
            RuleObservation.project_id == project_id, RuleObservation.rule_id == rule_id,
            RuleObservation.outcome == outcome, RuleObservation.item_id == item_id)):
        if (prior.commit or None) == (commit or None) and (prior.detail or {}) == detail:
            return prior
    row = RuleObservation(project_id=project_id, rule_id=rule_id, outcome=outcome,
                          item_id=item_id, commit=commit, detail=detail)
    db.add(row)
    db.flush()
    return row


def summary(db: Session, project_id: str | None, rule_id: str,
            since: datetime | None) -> dict:
    """What an observing rule reports: its would-have counts since it began observing, and
    whether a decision is due."""
    if rule_id in UNMEASURED_RULES:
        out = {"since": since, "would_hold": None, "would_refuse": None, "would_allow": None,
               "would_have": None, "unmeasured": True,
               "decision_due": False, "decision_due_reason": None}
        if since is not None and utcnow() - _aware(since) >= timedelta(days=DUE_DAYS):
            out["decision_due"], out["decision_due_reason"] = True, f"observing for {DUE_DAYS} days"
        return out
    counts = {o: 0 for o in OUTCOMES}
    if project_id and since is not None:
        q = (select(RuleObservation.outcome, func.count()).where(
            RuleObservation.project_id == project_id, RuleObservation.rule_id == rule_id,
            RuleObservation.at >= since).group_by(RuleObservation.outcome))
        for outcome, n in db.execute(q):
            counts[outcome] = n
    total = sum(counts.values())
    due, why = False, None
    if since is not None:
        if total >= DUE_OUTCOMES:
            due, why = True, f"{total} would-have outcomes"
        elif utcnow() - _aware(since) >= timedelta(days=DUE_DAYS):
            due, why = True, f"observing for {DUE_DAYS} days"
    return {"since": since, "would_hold": counts["would_hold"],
            "would_refuse": counts["would_refuse"], "would_allow": counts["would_allow"],
            "would_have": total,
            "decision_due": due, "decision_due_reason": why}


def history(db: Session, project_id: str, rule_id: str) -> list[RuleObservation]:
    return list(db.scalars(select(RuleObservation).where(
        RuleObservation.project_id == project_id, RuleObservation.rule_id == rule_id)
        .order_by(RuleObservation.id)))


def promote(db: Session, actor: object, project_id: str, rule_id: str, reason: str) -> dict:
    """A person promotes an observing rule to `active`, at its primitive's real level. Refused
    for an agent key naming `rules.person_adopts`. Would-have rows are kept."""
    user = T._check_project_writer(db, actor, project_id)
    why = T._reason(reason)
    from app.services import custom_rules
    if custom_rules.is_custom(rule_id):
        crow = custom_rules.get(db, rule_id, project_id)
        if crow.org_id:
            raise Validation(f"{rule_id} is owned by the org; an org admin promotes it",
                             hint="a project promotes only a rule it holds itself")
        if crow.state != "observing":
            raise Validation(f"{rule_id} is not observing")
        before = summary(db, project_id, rule_id, crow.created_at)
        crow.state = "active"
    else:
        T._rule(rule_id)
        since = T.observing_since(db, rule_id, project_id)
        if since is None:
            raise Validation(f"{rule_id} is not observing in this project")
        before = summary(db, project_id, rule_id, since)
        T._row(db, rule_id, project_id=project_id).observing_since = None
    events_svc.record_user(
        db, user, action="promote_rule", target_type="rule", target_id=rule_id,
        project_id=project_id,
        meta={"reason": why, "would_hold": before["would_hold"],
              "would_refuse": before["would_refuse"]})
    db.commit()
    return T.resolve(db, rule_id, project_id)
