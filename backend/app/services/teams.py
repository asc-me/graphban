"""Teams, and what a grant writes (PRD-21 D5).

A team is a named group inside an org with **grants** — a project plus an access level —
and a grant is the unit of access administration.

**A grant materializes.** Creating or changing one writes `Membership` rows; it does not
add a resolution step to `authz.can_read` / `can_write`. Those two are the hottest
authorization path in the application and every route depends on them, so resolving team
closure at read time would change the risk profile of the whole app for a feature that is
fundamentally administrative. Materialized, the blast radius is {@link recompute} and its
tests, and every existing authz test keeps its meaning.

The cost of materializing is drift between a derived row and the team that wrote it, which
D8 handles by refusing direct edits on a derived membership.

**Authorization never reads `origin` — it reads `access`.** `origin` exists for that
refusal and for recompute, and touches no permission decision. That bound is the whole
reason this stays administrative.
"""
from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Membership, Project, Team, TeamGrant, TeamMember, User

# write beats read beats nothing. Access resolves to the HIGHEST across all sources, so a
# person in two teams granting the same project gets the more permissive of the two.
_RANK = {"none": 0, "read": 1, "write": 2}
GRANT_LEVELS = ("read", "write")


def _org_scoped(db: Session, team_id: str) -> Team:
    team = db.get(Team, team_id)
    if team is None:
        raise HTTPException(404, "team not found")
    return team


def recompute(db: Session, user_id: str, project_id: str) -> Membership | None:
    """Bring one (user, project) pair in line with the grants that currently exist.

    **The single sync function**, and the only place a derived membership is written or
    removed. Called after every change to a team's members or grants.

    Two rules do the work:

    - **A direct membership always survives**, and where direct and derived collide the
      direct one wins — the grant materializes nothing. A human's explicit decision is not
      something a team's bulk administration may quietly overwrite.
    - **Revocation recomputes rather than deletes.** The derived access is recalculated
      from the grants that remain, so a second team still granting the project keeps the
      row alive. Deleting rows "belonging to" the revoked team would strip access someone
      still legitimately has, and that is a wrong answer arrived at by bookkeeping.

    Does NOT commit — the caller owns the transaction, so a grant change and everything it
    materializes land together or not at all.
    """
    # Serialize concurrent recomputes for this pair before reading. A (user, project)
    # that has no row yet offers nothing to lock, so the lock is taken on the row that
    # always exists — the project. Two grant changes touching one project queue here
    # instead of both reading `None` and both inserting. `with_for_update` is a no-op on
    # SQLite, which serializes writes globally anyway, so this degrades rather than lies.
    # Raced for real in tests/test_lock_concurrency.py, on Postgres (GRPH-432).
    db.get(Project, project_id, with_for_update=True)

    existing = db.scalar(
        select(Membership).where(
            Membership.user_id == user_id, Membership.project_id == project_id
        )
    )
    if existing is not None and existing.origin == "direct":
        return existing

    # The highest access any of this user's teams grants on this project.
    levels = [
        access
        for (access,) in db.execute(
            select(TeamGrant.access)
            .join(TeamMember, TeamMember.team_id == TeamGrant.team_id)
            .where(
                TeamMember.user_id == user_id,
                TeamGrant.project_id == project_id,
            )
        )
    ]
    best = max(levels, key=lambda a: _RANK.get(a, 0), default=None)

    if best is None:
        # No grant reaches this pair any more. The derived row goes; a direct one would
        # have returned above and is never touched here.
        if existing is not None:
            db.delete(existing)
        return None

    if existing is None:
        existing = Membership(
            user_id=user_id, project_id=project_id, role="member", origin="team"
        )
        db.add(existing)
    existing.access = best
    existing.origin = "team"
    return existing


def _recompute_team(db: Session, team: Team) -> None:
    """Recompute every (member, granted project) pair this team touches."""
    user_ids = [u for (u,) in db.execute(
        select(TeamMember.user_id).where(TeamMember.team_id == team.id)
    )]
    project_ids = [p for (p,) in db.execute(
        select(TeamGrant.project_id).where(TeamGrant.team_id == team.id)
    )]
    for user_id in user_ids:
        for project_id in project_ids:
            recompute(db, user_id, project_id)


# ---- administration ------------------------------------------------------------
def create_team(db: Session, org_id: str, name: str, description: str = "") -> Team:
    name = (name or "").strip()
    if not name:
        raise HTTPException(422, "a team needs a name")
    if db.scalar(select(Team).where(Team.org_id == org_id, Team.name == name)):
        raise HTTPException(409, f"a team called {name!r} already exists in this org")
    team = Team(id="tm_" + uuid.uuid4().hex[:10], org_id=org_id, name=name,
                description=(description or "").strip())
    db.add(team)
    db.commit()
    db.refresh(team)
    return team


def delete_team(db: Session, team_id: str) -> dict:
    """Disband a team and recompute everything it was granting. Commits.

    The recompute is what makes this safe: access someone also holds directly, or through
    another team, survives. Only what this team alone was providing goes away.
    """
    team = _org_scoped(db, team_id)
    user_ids = [u for (u,) in db.execute(
        select(TeamMember.user_id).where(TeamMember.team_id == team.id)
    )]
    project_ids = [p for (p,) in db.execute(
        select(TeamGrant.project_id).where(TeamGrant.team_id == team.id)
    )]

    for row in db.scalars(select(TeamGrant).where(TeamGrant.team_id == team.id)):
        db.delete(row)
    for row in db.scalars(select(TeamMember).where(TeamMember.team_id == team.id)):
        db.delete(row)
    # Flush the children BEFORE deleting the parent. No `relationship()` ties these tables
    # together, so the unit of work has no dependency to order by and is free to emit
    # `DELETE FROM teams` first — which Postgres refuses with a foreign-key violation.
    # SQLite does not enforce foreign keys by default, so it accepts the orphaning
    # silently: this is invisible on one engine and fatal on the other.
    db.flush()
    db.delete(team)
    db.flush()

    kept = 0
    for user_id in user_ids:
        for project_id in project_ids:
            if recompute(db, user_id, project_id) is not None:
                kept += 1
    db.commit()
    return {"users": len(user_ids), "projects": len(project_ids), "memberships_kept": kept}


def add_member(db: Session, team_id: str, user_id: str) -> TeamMember:
    """Put someone in a team, materializing every grant it already holds. Commits."""
    team = _org_scoped(db, team_id)
    if db.get(User, user_id) is None:
        raise HTTPException(404, "user not found")
    existing = db.scalar(
        select(TeamMember).where(TeamMember.team_id == team_id, TeamMember.user_id == user_id)
    )
    if existing is None:
        existing = TeamMember(team_id=team_id, user_id=user_id)
        db.add(existing)
        db.flush()
    for (project_id,) in db.execute(
        select(TeamGrant.project_id).where(TeamGrant.team_id == team.id)
    ):
        recompute(db, user_id, project_id)
    db.commit()
    db.refresh(existing)
    return existing


def remove_member(db: Session, team_id: str, user_id: str) -> None:
    """Take someone out of a team and recompute. Commits.

    They keep any access they hold directly, and any the same project gets from another
    team they are still in.
    """
    team = _org_scoped(db, team_id)
    row = db.scalar(
        select(TeamMember).where(TeamMember.team_id == team_id, TeamMember.user_id == user_id)
    )
    if row is None:
        raise HTTPException(404, "that person is not in this team")
    project_ids = [p for (p,) in db.execute(
        select(TeamGrant.project_id).where(TeamGrant.team_id == team.id)
    )]
    db.delete(row)
    db.flush()
    for project_id in project_ids:
        recompute(db, user_id, project_id)
    db.commit()


def set_grant(db: Session, team_id: str, project_id: str, access: str) -> TeamGrant:
    """Grant or change a team's access to a project. Commits.

    The write and everything it materializes are one transaction: a grant that landed
    while its memberships did not would be a promise the authorization layer never heard.
    """
    if access not in GRANT_LEVELS:
        raise HTTPException(
            422, f"unknown access {access!r}; a grant is one of {', '.join(GRANT_LEVELS)}"
        )
    team = _org_scoped(db, team_id)
    project = db.get(Project, project_id)
    if project is None or project.org_id != team.org_id:
        # Cross-org grants would be an access path the org roster cannot explain.
        raise HTTPException(404, "project not found in this organization")

    grant = db.scalar(
        select(TeamGrant).where(TeamGrant.team_id == team_id, TeamGrant.project_id == project_id)
    )
    if grant is None:
        grant = TeamGrant(team_id=team_id, project_id=project_id)
        db.add(grant)
    grant.access = access
    db.flush()
    _recompute_team(db, team)
    db.commit()
    db.refresh(grant)
    return grant


def revoke_grant(db: Session, team_id: str, project_id: str) -> dict:
    """Remove a team's grant on a project and recompute. Commits.

    Returns what survived, because "revoked" and "everyone lost access" are different
    facts and the screen says which.
    """
    team = _org_scoped(db, team_id)
    grant = db.scalar(
        select(TeamGrant).where(TeamGrant.team_id == team_id, TeamGrant.project_id == project_id)
    )
    if grant is None:
        raise HTTPException(404, "this team has no grant on that project")
    user_ids = [u for (u,) in db.execute(
        select(TeamMember.user_id).where(TeamMember.team_id == team.id)
    )]
    db.delete(grant)
    db.flush()

    kept = []
    for user_id in user_ids:
        if recompute(db, user_id, project_id) is not None:
            kept.append(user_id)
    db.commit()
    return {"affected": len(user_ids), "kept_access": kept}


def materialized_by(db: Session, team_id: str, project_id: str) -> dict:
    """Who a grant currently reaches, split by whether the grant is what provides it.

    The screen shows both: a member marked `direct` keeps their access when this grant is
    revoked, and saying so is the difference between a revoke that does what the admin
    expects and one that quietly does less.
    """
    out = {"derived": [], "direct": []}
    for (user_id,) in db.execute(
        select(TeamMember.user_id).where(TeamMember.team_id == team_id)
    ):
        m = db.scalar(
            select(Membership).where(
                Membership.user_id == user_id, Membership.project_id == project_id
            )
        )
        if m is None:
            continue
        out["direct" if m.origin == "direct" else "derived"].append(user_id)
    return out
