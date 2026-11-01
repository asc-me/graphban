"""Plan tier table, usage math, and hosted per-org rate limiting (AL-75).

The product no longer refuses work for plan limits — project/seat/shard/monthly-call
caps and Stripe billing live on the control plane (GBC-35). ``enforce_project_quota``,
``enforce_seat_quota``, ``enforce_shard_quota``, and ``meter_call`` are permanent no-ops
here so every install serves the tracker without HTTP 402 plan gates.

``PLANS`` and ``usage()`` remain for operator/galaxy metadata surfaces that still
report limits vs counters. ``enforce_org_rate`` is unchanged: a short-window burst cap
on MCP traffic when ``hosted_mode`` is on.

Public / unauthenticated surfaces (feedback intake, public roadmap) are deliberately
NOT metered or gated.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.errors import QuotaExceeded, RateLimited
from app.services import ratelimit
from app.models import (
    MemoryShard,
    OrgInvite,
    OrgMembership,
    OrgUsage,
    Organization,
    Project,
    User,
    utcnow,
)


@dataclass(frozen=True)
class Plan:
    max_projects: int
    max_seats: int
    max_shards: int
    max_calls_per_month: int
    # Standing entitlement to found ADDITIONAL organizations (AL-92). Off for the
    # self-serve tiers — one org each, more by request — and carried by `enterprise`,
    # which is the licensed multi-org offering. Billing lights this up later (AL-82).
    may_found_additional_orgs: bool = False
    # Capacity routing beyond in-runner failover: the entitlement registry, cross-runner
    # routing, forecasting and utilization (PRD-62 D8, S2–S5). S1 is open source and never
    # reads this.
    capacity_routing: bool = False
    # Org-level rule adoption and locks (PRD-P63 D11): an org admin setting a library rule
    # for every project in the org. Per-project toggles ship on every plan and never read this.
    org_rules: bool = False


# Starting private-beta limits — tunable in one place. Free stays generous on the
# cloud collaboration features (feedback toolkit, cloud sync, public roadmap — none
# of which are metered here); the caps bite on projects/seats/shards and agent calls.
PLANS: dict[str, Plan] = {
    "free": Plan(max_projects=2, max_seats=3, max_shards=250, max_calls_per_month=10_000),
    "pro": Plan(max_projects=10, max_seats=15, max_shards=10_000, max_calls_per_month=100_000),
    "team": Plan(max_projects=50, max_seats=100, max_shards=100_000, max_calls_per_month=1_000_000,
                 capacity_routing=True, org_rules=True),
    "enterprise": Plan(
        max_projects=500, max_seats=1_000, max_shards=1_000_000,
        max_calls_per_month=10_000_000, may_found_additional_orgs=True, capacity_routing=True,
        org_rules=True,
    ),
}
DEFAULT_PLAN = "free"


def plan_of(org: Organization) -> Plan:
    return PLANS.get(org.plan, PLANS[DEFAULT_PLAN])


def is_platform_admin(user: User) -> bool:
    """Whether this account may manage plans (operator allowlist, AL-75)."""
    return (user.email or "").lower() in settings.platform_admin_email_set


def _period() -> str:
    now = utcnow()
    return f"{now.year:04d}-{now.month:02d}"


# ---- usage counts -------------------------------------------------------------
def project_count(db: Session, org_id: str) -> int:
    return db.scalar(select(func.count()).select_from(Project).where(Project.org_id == org_id)) or 0


def seat_count(db: Session, org_id: str) -> int:
    """Occupied + reserved seats: members plus still-pending invites, so you can't
    out-invite the seat cap and only discover it when everyone accepts."""
    members = db.scalar(
        select(func.count()).select_from(OrgMembership).where(OrgMembership.org_id == org_id)
    ) or 0
    pending = db.scalar(
        select(func.count())
        .select_from(OrgInvite)
        .where(OrgInvite.org_id == org_id, OrgInvite.status == "pending")
    ) or 0
    return members + pending


def shard_count(db: Session, org_id: str) -> int:
    return db.scalar(
        select(func.count())
        .select_from(MemoryShard)
        .join(Project, Project.id == MemoryShard.project_id)
        .where(Project.org_id == org_id, MemoryShard.status != "rejected")
    ) or 0


def calls_this_month(db: Session, org_id: str) -> int:
    row = db.scalar(
        select(OrgUsage).where(OrgUsage.org_id == org_id, OrgUsage.period == _period())
    )
    return row.mcp_calls if row else 0


def usage(db: Session, org_id: str) -> dict[str, int]:
    return {
        "projects": project_count(db, org_id),
        "seats": seat_count(db, org_id),
        "shards": shard_count(db, org_id),
        "calls_this_month": calls_this_month(db, org_id),
    }


def org_id_for_project(db: Session, project_id: str | None) -> str | None:
    if not project_id:
        return None
    p = db.get(Project, project_id)
    return p.org_id if p else None


# ---- plan quota enforcement (removed from product — control plane owns caps) -----
def enforce_project_quota(db: Session, org_id: str | None) -> None:
    return


def enforce_seat_quota(db: Session, org_id: str | None) -> None:
    return


def enforce_shard_quota(db: Session, org_id: str | None) -> None:
    return


def require_capacity_routing(db: Session, org_id: str | None) -> Organization:
    """The PRD-62 D8 premium gate: S2–S5 need the `team` or `enterprise` plan.

    Unlike the caps above this one is NOT a no-op — it is a product boundary the PRD draws,
    not a usage limit the control plane meters. Self-host keeps it behind the same check: a
    project with no org has no plan but `DEFAULT_PLAN`, so it is refused rather than waved
    through. Raises `QuotaExceeded` (HTTP 402 / `quota_exceeded`) and returns the org.
    """
    org = db.get(Organization, org_id) if org_id else None
    if org is not None and plan_of(org).capacity_routing:
        return org
    paid = ", ".join(n for n, p in PLANS.items() if p.capacity_routing)
    where = f"this org is on {org.plan or DEFAULT_PLAN!r}" if org else "this project has no org"
    raise QuotaExceeded(
        f"capacity routing needs one of: {paid}; {where}",
        hint="upgrade the org's plan, or keep to in-runner failover, which needs no plan",
    )


def require_org_rules(db: Session, org_id: str | None) -> Organization:
    """The PRD-P63 D11 premium gate: org-level rule adoption and locks need `team` or
    `enterprise`. Not a no-op, for the reason `require_capacity_routing` gives, and self-host
    keeps it behind the same check. Raises `QuotaExceeded` (402 / `quota_exceeded`)."""
    org = db.get(Organization, org_id) if org_id else None
    if org is not None and plan_of(org).org_rules:
        return org
    paid = ", ".join(n for n, p in PLANS.items() if p.org_rules)
    where = f"this org is on {org.plan or DEFAULT_PLAN!r}" if org else "there is no such org"
    raise QuotaExceeded(
        f"org-level rules need one of: {paid}; {where}",
        hint="upgrade the org's plan, or set the rule per project, which every plan can",
    )


def enforce_org_rate(org_id: str | None) -> None:
    """Hosted per-org burst cap on agent (MCP) calls — a short-window limit distinct
    from the monthly plan quota. Shared across replicas when REDIS_URL is set. No-op
    off hosted mode, without an org, or when the cap is disabled (0)."""
    if not settings.hosted_mode or not org_id or settings.org_rate_per_min <= 0:
        return
    if not ratelimit.allow(f"org:{org_id}", settings.org_rate_per_min, 60):
        raise RateLimited(
            f"rate limit exceeded (> {settings.org_rate_per_min} calls/min for this organization)",
            hint="back off and retry in a few seconds",
        )


def meter_call(db: Session, org_id: str | None) -> None:
    """Monthly MCP call caps left the product with plan billing (GBC-35)."""
    return
