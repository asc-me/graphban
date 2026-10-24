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
from app.errors import RateLimited
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


# Starting private-beta limits — tunable in one place. Free stays generous on the
# cloud collaboration features (feedback toolkit, cloud sync, public roadmap — none
# of which are metered here); the caps bite on projects/seats/shards and agent calls.
PLANS: dict[str, Plan] = {
    "free": Plan(max_projects=2, max_seats=3, max_shards=250, max_calls_per_month=10_000),
    "pro": Plan(max_projects=10, max_seats=15, max_shards=10_000, max_calls_per_month=100_000),
    "team": Plan(max_projects=50, max_seats=100, max_shards=100_000, max_calls_per_month=1_000_000),
    "enterprise": Plan(
        max_projects=500, max_seats=1_000, max_shards=1_000_000,
        max_calls_per_month=10_000_000, may_found_additional_orgs=True,
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
