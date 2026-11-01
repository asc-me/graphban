"""Measuring library rules (GRPH-1265 / PRD-P63 S5, D8, D9): caught, escapes, cost, retirement.

Every measured value leaves here as `{status, value, n, n_band, min_n, reason}` (the GBC-P6
contract):

- `measured`     — a real value, zero included. `value: 0` is a real zero.
- `insufficient` — fewer than `min_n` observations. `value` is None and the UI renders
                   "not enough data (n of min_n)". A null value never means anything else.
- `unavailable`  — nothing in the ledger can answer it; `reason` says what is missing.

`n` is an exact int at project and org scope, with `n_band` None. At platform scope `n` is ALWAYS
None and `n_band` carries the band ("20–49", or "<20" below the floor), because an exact platform
total would let a contributor subtract its own counts and recover the rest (PRD-38 D13).

Escape-rate changes are OBSERVATIONAL (D9, §13): the window after adoption is compared with an
equal-length window before it, with n on both sides, and nothing here claims a cause. The rate is
PROJECT-WIDE (`escape_scope: "project"`): every escape kind surfaces at merge, after every stage's
check has run, so no ledger record says which stage should have caught it. Every rule in a project
therefore shows the same before/after counts for the same adoption time.

Scopes: a project, an org (team/enterprise — D11), and the platform (opt-in, hosted only, on the
PRD-38 D13 terms: `Organization.telemetry_share`, three orgs, `PLATFORM_MIN_N`, a 60% top-org cap,
n as a band, rates to two decimals, no ids or text).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Event, Item, Observation, Organization, PlatformRuleRollup, Project
from app.services import harness as harness_svc
from app.services import observations as observations_svc
from app.services import quotas
from app.services import rule_toggles

#: Sign-offs needed on EACH side of an adoption before an escape rate is shown.
MIN_SIGNOFFS = 10
#: Escape observation kinds measured in v1 (D9). A bounce counts only after a prior sign-off.
ESCAPE_KINDS = ("ci_red_after_sign_off", "reopened_after_done", "bug_escaped")
#: Escape kinds with no record to count from.
UNMEASURED_ESCAPES: tuple[str, ...] = ()
#: What the escape counts are scoped to; see the module docstring.
ESCAPE_SCOPE = "project"
ESCAPE_SCOPE_NOTE = ("escapes are counted across the whole project, not per stage: no record "
                     "says which stage's check an escape got past")
#: A rule on this long with no catches is suggested for retirement.
RETIRE_AFTER_DAYS = 60
CAUGHT_LINKS = 50
#: Observation kinds recorded here (GRPH-1283, D9 cost). Both name the rule; neither carries text.
OVERRIDE_KIND = "rule_override"
HELD_KIND = "rule_held"
#: Refusals (for overrides) or recorded holds (for held time) needed before a cost is shown.
MIN_COST_N = 5
NOTE = "observed change, not a proven cause"


def value(v, *, n: int, min_n: int, reason: str | None = None, available: bool = True) -> dict:
    if not available:
        return {"status": "unavailable", "value": None, "n": n, "n_band": None, "min_n": min_n,
                "reason": reason}
    if n < min_n:
        return {"status": "insufficient", "value": None, "n": n, "n_band": None, "min_n": min_n,
                "reason": reason or f"not enough data ({n} of {min_n})"}
    return {"status": "measured", "value": v, "n": n, "n_band": None, "min_n": min_n,
            "reason": reason}


def banded(v, *, n: int, min_n: int, reason: str | None = None, available: bool = True) -> dict:
    """`value` for the platform scope: the exact count never leaves, only its band."""
    out = value(v, n=n, min_n=min_n, reason=reason, available=available)
    return {**out, "n": None, "n_band": _band(n) if available else None}


def _aware(ts: datetime) -> datetime:
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---- adoption ---------------------------------------------------------------------------------

def adoption(db: Session, rule_id: str, project_id: str) -> tuple[bool, datetime | None]:
    """(on, since) for the rule in the project. `since` is the latest off→on transition in the
    `set_rule_toggle` audit events (S2 records them), project's own or its org's. A rule that is
    on by default and never went off→on is on with `since` None: the pack load that made it so
    is not recorded, so its adoption time is unknown — it is still on, and must not drop out."""
    org_id = db.scalar(select(Project.org_id).where(Project.id == project_id))
    rule = rule_toggles.LIBRARY.get(rule_id)
    state, since = bool(rule and rule.default_on), None
    q = select(Event).where(Event.action == "set_rule_toggle", Event.target_id == rule_id) \
        .order_by(Event.ts, Event.id)
    for e in db.scalars(q):
        m = e.meta or {}
        if e.project_id == project_id:
            new = (m.get("effective_after") or {}).get("on")
        elif m.get("scope") == "org" and m.get("org_id") == org_id \
                and project_id in (m.get("projects_changed") or []):
            new = (m.get("new") or {}).get("on")
        else:
            continue
        if new is None or new == state:
            continue
        state, since = new, (_aware(e.ts) if new else None)
    return state, (since if state else None)


def adopted_at(db: Session, rule_id: str, project_id: str) -> datetime | None:
    """When the rule last went on for the project and stayed on; None when off, or on by
    default with no recorded adoption time (`adoption` tells the two apart)."""
    return adoption(db, rule_id, project_id)[1]


# ---- recording cost (D9) ----------------------------------------------------------------------

def record_override(db: Session, item: Item, rule_id: str, actor: str,
                    occurred_at: datetime | None = None) -> bool:
    """A person overrode the rule's refusal on the item. One fact per override: it names the rule,
    the item and the actor (an id, never text). True when it was newly recorded."""
    rule_toggles._rule(rule_id)
    n = db.scalar(select(func.count(Observation.id)).where(
        Observation.item_id == item.id, Observation.kind == OVERRIDE_KIND,
        Observation.rule_id == rule_id)) or 0
    return observations_svc._add(
        db, item, kind=OVERRIDE_KIND, key=f"override:{item.id}:{rule_id}:{n + 1}", origin="live",
        stage=rule_toggles.LIBRARY[rule_id].stage, rule_id=rule_id, ref={"actor": actor},
        occurred_at=occurred_at)


def record_held(db: Session, item: Item, rule_id: str, started_at: datetime,
                ended_at: datetime) -> bool:
    """The item was held by the rule from `started_at` to `ended_at`. Seconds are stored per
    rule; a hold that ends before it starts is refused rather than recorded as zero."""
    rule_toggles._rule(rule_id)
    started, ended = _aware(started_at), _aware(ended_at)
    if ended < started:
        raise ValueError("a hold cannot end before it starts")
    return observations_svc._add(
        db, item, kind=HELD_KIND, key=f"held:{item.id}:{rule_id}:{started.isoformat()}",
        origin="live", stage=rule_toggles.LIBRARY[rule_id].stage, rule_id=rule_id,
        ref={"seconds": int((ended - started).total_seconds())}, occurred_at=ended)


def open_hold(db: Session, item_id: str, rule_id: str) -> datetime | None:
    """When the rule's current hold on the item began, or None when it holds nothing: the
    earliest refusal by the rule on the item since its last hold was closed (every close —
    the rule passing, or a person overriding it — records a `rule_held`)."""
    last = db.scalar(select(func.max(Observation.occurred_at)).where(
        Observation.item_id == item_id, Observation.kind == HELD_KIND,
        Observation.rule_id == rule_id))
    q = select(func.min(Observation.occurred_at)).where(
        Observation.item_id == item_id, Observation.kind == "refusal",
        Observation.rule_id == rule_id)
    if last is not None:
        q = q.where(Observation.occurred_at > last)
    start = db.scalar(q)
    return _aware(start) if start is not None else None


def close_holds(db: Session, item: Item, rule_ids, ended_at: datetime | None = None) -> int:
    """The item got past these rules: each open hold is recorded as held time. A rule not in
    the loaded library holds nothing. Returns how many holds were closed."""
    ended, n = _aware(ended_at or _now()), 0
    for rule_id in rule_ids:
        if rule_id not in rule_toggles.LIBRARY:
            continue
        start = open_hold(db, item.id, rule_id)
        if start is not None:
            n += record_held(db, item, rule_id, start, max(start, ended))
    return n


def override_holds(db: Session, rule_id: str, project_ids: list[str], actor: str,
                   at: datetime | None = None) -> int:
    """A person switched the rule off for these projects: every open item it was holding
    there proceeds past its refusal. Each is one override naming the rule, the item and the
    person, and the hold closes at that moment. Returns how many items were overridden."""
    if rule_id not in rule_toggles.LIBRARY or not project_ids:
        return 0
    at, n = _aware(at or _now()), 0
    item_ids = db.scalars(select(Observation.item_id).where(
        Observation.project_id.in_(project_ids), Observation.kind == "refusal",
        Observation.rule_id == rule_id, Observation.item_id.is_not(None)).distinct())
    for item_id in list(item_ids):
        item = db.get(Item, item_id)
        if item is None or item.status == "done":
            continue
        start = open_hold(db, item.id, rule_id)
        if start is None:
            continue
        record_override(db, item, rule_id, actor, occurred_at=at)
        record_held(db, item, rule_id, start, max(start, at))
        n += 1
    return n


def _cost_rows(db: Session, rule_id: str, project_id: str, kind: str) -> list[Observation]:
    return list(db.scalars(select(Observation).where(
        Observation.project_id == project_id, Observation.kind == kind,
        Observation.rule_id == rule_id)))


# ---- per-project counts -----------------------------------------------------------------------

def _caught(db: Session, rule_id: str, project_id: str) -> list[Observation]:
    return list(db.scalars(select(Observation).where(
        Observation.project_id == project_id, Observation.kind == "refusal",
        Observation.rule_id == rule_id).order_by(Observation.occurred_at.desc())))


def _signoffs(db: Session, project_id: str) -> dict[str, datetime]:
    """item id -> first sign-off time, from the audit ledger."""
    out: dict[str, datetime] = {}
    for e in db.scalars(select(Event).where(
            Event.project_id == project_id, Event.action == "sign_off",
            Event.target_type == "item").order_by(Event.ts, Event.id)):
        out.setdefault(e.target_id, _aware(e.ts))
    return out


def _window_counts(db: Session, project_id: str, start: datetime, end: datetime,
                   signed: dict[str, datetime]) -> tuple[int, int]:
    """(sign-offs, escapes) with the time in [start, end)."""
    n_signed = sum(1 for t in signed.values() if start <= t < end)
    escapes = 0
    for o in db.scalars(select(Observation).where(
            Observation.project_id == project_id,
            Observation.kind.in_((*ESCAPE_KINDS, "bounce")))):
        t = _aware(o.occurred_at)
        if not start <= t < end:
            continue
        if o.kind == "bounce":
            first = signed.get(o.item_id or "")
            if first is None or first >= t:
                continue
        escapes += 1
    return n_signed, escapes


def project_counts(db: Session, rule_id: str, project_id: str) -> dict:
    """Raw counts for one rule in one project; the unit every scope sums."""
    on, since = adoption(db, rule_id, project_id)
    out = {"on": on, "adopted_at": since, "caught": _caught(db, rule_id, project_id),
           "overrides": _cost_rows(db, rule_id, project_id, OVERRIDE_KIND),
           "holds": _cost_rows(db, rule_id, project_id, HELD_KIND),
           "before": (0, 0), "after": (0, 0)}
    if since is not None:
        span = _now() - since
        signed = _signoffs(db, project_id)
        out["before"] = _window_counts(db, project_id, since - span, since, signed)
        out["after"] = _window_counts(db, project_id, since, _now() + timedelta(seconds=1), signed)
    return out


#: Why a rule that is on has no adoption time to measure from.
NO_ADOPTION_TIME = ("on by default, and no record says when the rule pack made it so, so there "
                    "is no adoption time to measure from")


def _escape(counts: list[dict], which: str, label: str, untimed: int = 0) -> dict:
    n = sum(c[which][0] for c in counts)
    esc = sum(c[which][1] for c in counts)
    reason = None if n >= MIN_SIGNOFFS else \
        f"not enough data ({n} of {MIN_SIGNOFFS} sign-offs {label} adoption)"
    if untimed:
        left_out = (f"leaves out {untimed} project(s) where the rule is on by default with no "
                    "recorded adoption time")
        reason = f"{reason}; {left_out}" if reason else left_out
    return value(round(esc / n, 4) if n else None, n=n, min_n=MIN_SIGNOFFS, reason=reason)


def _summary(rule_id: str, counts: list[dict], project_ids: list[str]) -> dict:
    caught = [o for c in counts for o in c["caught"]]
    on = [c for c in counts if c["on"]]
    adopted = [c for c in on if c["adopted_at"] is not None]
    untimed = len(on) - len(adopted)
    rule = rule_toggles.LIBRARY[rule_id]
    out = {
        "rule_id": rule_id, "stage": rule.stage, "title": rule.title,
        "projects": len(project_ids), "projects_on": len(on),
        "projects_on_without_adoption_time": untimed,
        "adopted_at": (min(c["adopted_at"] for c in adopted).isoformat() if adopted else None),
        "caught": {**value(len(caught), n=len(caught), min_n=0),
                   "items": sorted({o.item_id for o in caught if o.item_id})[:CAUGHT_LINKS]},
        "cost": _cost(counts, caught),
        "escape_note": NOTE,
        "escape_scope": ESCAPE_SCOPE, "escape_scope_note": ESCAPE_SCOPE_NOTE,
        "escapes_unmeasured": list(UNMEASURED_ESCAPES),
    }
    if adopted:
        out["escape_rate"] = {"before": _escape(adopted, "before", "before", untimed),
                              "after": _escape(adopted, "after", "after", untimed)}
    else:
        why = NO_ADOPTION_TIME if untimed else \
            "the rule has not been adopted in this scope, so there is no adoption time"
        out["escape_rate"] = {k: value(None, n=0, min_n=MIN_SIGNOFFS, available=False, reason=why)
                              for k in ("before", "after")}
    overrides = [o for c in counts for o in c["overrides"]]
    out["retirement"] = _retirement(adopted, caught, untimed, overrides)
    return out


def _cost(counts: list[dict], caught: list) -> dict:
    """Overrides are counted against the refusals they could have overridden, so a real zero
    needs enough refusals behind it; held time against the number of recorded holds."""
    overrides = [o for c in counts for o in c["overrides"]]
    holds = [h for c in counts for h in c["holds"]]
    return {
        "overridden": value(len(overrides), n=len(caught), min_n=MIN_COST_N,
                            reason=None if len(caught) >= MIN_COST_N else
                            f"not enough data ({len(caught)} of {MIN_COST_N} refusals to override)"),
        "held_seconds": value(sum((h.ref or {}).get("seconds", 0) for h in holds),
                              n=len(holds), min_n=MIN_COST_N),
    }


def _retirement(adopted: list[dict], caught: list, untimed: int = 0,
                overrides: list | None = None) -> dict:
    """Suggest retiring a rule on either D9 criterion: on a long while and caught nothing, or
    overridden more often than it caught over the adoption window. `overrides` is None when they
    are not recorded; the evidence then says so rather than reporting 0."""
    if not adopted:
        if untimed:
            return {"suggested": False, "status": "unavailable",
                    "reason": f"{NO_ADOPTION_TIME}, so how long it has been on is unknown"}
        return {"suggested": False, "reason": "not adopted here"}
    oldest = min(c["adopted_at"] for c in adopted)
    days = (_now() - oldest).days
    # Both criteria read the adoption window: a catch from before the rule last went on says
    # nothing about the rule as it runs now, and pairing it with in-window overrides would
    # compare counts from two different windows.
    floor = _aware(oldest)
    caught = [o for o in caught if _aware(o.occurred_at) >= floor]
    evidence = {"days_on": days, "caught": len(caught)}
    if overrides is None:
        evidence["overrides"] = "unavailable: not recorded"
    else:
        in_window = [o for o in overrides if _aware(o.occurred_at) >= floor]
        evidence["overrides"] = len(in_window)
        if len(in_window) > len(caught):
            return {"suggested": True, "evidence": evidence,
                    "reason": f"overridden {len(in_window)} times against {len(caught)} caught "
                              f"in {days} days"}
    if days >= RETIRE_AFTER_DAYS and not caught:
        return {"suggested": True, "reason": f"on for {days} days and caught nothing",
                "evidence": evidence}
    return {"suggested": False, "reason": f"on for {days} days; caught {len(caught)}",
            "evidence": evidence}


# ---- scopes -----------------------------------------------------------------------------------

def _rules(rule_id: str | None) -> list[str]:
    if rule_id is not None:
        rule_toggles._rule(rule_id)
        return [rule_id]
    return list(rule_toggles.LIBRARY)


def project_effect(db: Session, project_id: str, rule_id: str | None = None) -> dict:
    return {"scope": "project", "project_id": project_id,
            "library_loaded": rule_toggles.library_loaded(),
            "rules": [_summary(r, [project_counts(db, r, project_id)], [project_id])
                      for r in _rules(rule_id)]}


def org_effect(db: Session, org_id: str, rule_id: str | None = None) -> dict:
    """Across the org's projects. Premium (D11): 402 on free/pro."""
    quotas.require_org_rules(db, org_id)
    pids = observations_svc.org_project_ids(db, org_id)
    return {"scope": "org", "org_id": org_id, "library_loaded": rule_toggles.library_loaded(),
            "rules": [_summary(r, [project_counts(db, r, p) for p in pids], pids)
                      for r in _rules(rule_id)]}


# ---- opt-in contribution and the platform prior (D8, on PRD-38 D13) ---------------------------

def contribution(db: Session, org_id: str) -> dict:
    """What an org would contribute, per rule: counts only. Listed in the UI so the
    redaction is checkable. Nothing here carries an id, a name or text."""
    pids = observations_svc.org_project_ids(db, org_id)
    rows = {}
    for r in rule_toggles.LIBRARY:
        counts = [project_counts(db, r, p) for p in pids]
        adopted = [c for c in counts if c["adopted_at"] is not None]
        rows[r] = {
            "projects_on": sum(1 for c in counts if c["on"]),
            "caught": sum(len(c["caught"]) for c in counts),
            "signed_before": sum(c["before"][0] for c in adopted),
            "escapes_before": sum(c["before"][1] for c in adopted),
            "signed_after": sum(c["after"][0] for c in adopted),
            "escapes_after": sum(c["after"][1] for c in adopted)}
    return rows


def platform_roll(db: Session) -> int:
    """Recompute `platform_rule_rollups` from orgs that opted in. Opting out removes an org on
    the next call, which is the whole purge: no raw row ever leaves, and none is stored here."""
    week = _now().strftime("%G-W%V")
    orgs = [o.id for o in db.scalars(select(Organization).where(
        Organization.telemetry_share.is_(True)))]
    per_rule: dict[str, dict[str, dict]] = {}
    for org_id in orgs:
        for rule_id, c in contribution(db, org_id).items():
            per_rule.setdefault(rule_id, {})[org_id] = c
    for old in db.scalars(select(PlatformRuleRollup)):
        db.delete(old)
    db.flush()
    for rule_id, by_org in per_rule.items():
        tot = {k: sum(c[k] for c in by_org.values())
               for k in ("projects_on", "caught", "signed_before", "escapes_before",
                         "signed_after", "escapes_after")}
        n_each = {o: c["signed_before"] + c["signed_after"] for o, c in by_org.items()}
        n = sum(n_each.values())
        db.add(PlatformRuleRollup(
            week=week, rule_id=rule_id,
            orgs_contributing=sum(1 for v in n_each.values()
                                  if v >= harness_svc.PLATFORM_MIN_ORG_N),
            top_org_share=(max(n_each.values()) / n) if n else None, rolled_at=_now(), **tot))
    db.flush()
    return len(per_rule)


def _band(n: int) -> str:
    return harness_svc._band_n(n)


def platform_effect(db: Session, org_id: str) -> dict:
    """Platform effect rates for an org. Premium (D11), hosted only, and only for an org that
    has itself opted in: an org that has not neither contributes nor sees them."""
    quotas.require_org_rules(db, org_id)
    org = db.get(Organization, org_id)
    out = {"scope": "platform", "rules": []}
    if not settings.hosted_mode:
        return {**out, "available": False, "reason": "no platform rates on a self-hosted "
                "instance: they are built from other orgs' contributions"}
    if not (org and org.telemetry_share):
        return {**out, "available": False, "reason": "this org has not opted in to contribute, "
                "so it does not see platform effect rates"}
    for row in db.scalars(select(PlatformRuleRollup).order_by(PlatformRuleRollup.rule_id)):
        out["rules"].append({"rule_id": row.rule_id, **_platform_cell(row)})
    return {**out, "available": True,
            "contributed_fields": ["projects_on", "caught", "signed_before", "escapes_before",
                                   "signed_after", "escapes_after"]}


def _platform_cell(row: PlatformRuleRollup) -> dict:
    """One rule's platform rates. `n` is never exact here, on any branch: see the module doc."""
    n_min = harness_svc.PLATFORM_MIN_N

    def cell(reason: str) -> dict:
        return {"before": banded(None, n=0, min_n=n_min, available=False, reason=reason),
                "after": banded(None, n=0, min_n=n_min, available=False, reason=reason)}

    if row.orgs_contributing < harness_svc.PLATFORM_MIN_ORGS:
        return cell(f"no platform rate: fewer than {harness_svc.PLATFORM_MIN_ORGS} orgs "
                    "contribute here")
    if row.top_org_share is not None and row.top_org_share > harness_svc.PLATFORM_MAX_ORG_SHARE:
        return cell("no platform rate: one org holds too large a share of this sample")

    def side(esc: int, signed: int, label: str) -> dict:
        # Each side stands on its own floor and shows its band, never its count.
        if signed < n_min:
            return {"status": "insufficient", "value": None, "n": None,
                    "n_band": _band(signed), "min_n": n_min,
                    "reason": f"not enough data {label} adoption (needs at least {n_min} "
                              "sign-offs)"}
        return banded(round(esc / signed, 2), n=signed, min_n=n_min)
    return {"orgs": row.orgs_contributing, "note": NOTE, "escape_scope": ESCAPE_SCOPE,
            "before": side(row.escapes_before, row.signed_before, "before"),
            "after": side(row.escapes_after, row.signed_after, "after")}
