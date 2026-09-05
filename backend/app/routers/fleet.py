"""The Fleet view's server half (GRPH-336 / PRD-17 D5).

One read that answers the view's whole question — who is out there, what is waiting for a
second pair of eyes, and which clusters are held back and why — plus the two writes that make
a wave: mint a role-narrowed credential, and end the wave.

Session-authenticated, unlike the MCP surface beside it. The caller here is a human deciding
how to spend a fleet, not an agent working inside one.
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ApiKey, Enrolment, User
from app.security import authz
from app.security.deps import get_agent_key, get_current_user
from app.models import Project
from app.services import events as events_svc
from app.services import fleet as fleet_svc
from app.services import fleet_profiles
from app.services import harness as harness_svc

router = APIRouter(prefix="/fleet", tags=["fleet"])


@router.get("")
def fleet_overview(project_id: str | None = None, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """Roster, review queue, and the cluster board in one call.

    One request rather than three because the view renders them together and a partial fleet
    picture is worse than a slow one — a roster that arrives before the review queue shows an
    idle reviewer next to work it could already be taking.
    """
    authz.require_readable(db, user.id, project_id)
    status = fleet_svc.fleet_status(db, project_id, caller_user_id=user.id)
    return {
        **status,
        "review_queue": fleet_svc.review_queue(db, project_id),
        "clusters": fleet_svc.cluster_board(db, project_id),
        # Seats ride with the roster because they are read together: "three agents online,
        # one seat still unused" is one question, and two calls would let the page render a
        # half-answer.
        "seats": fleet_svc.list_enrolments(db, project_id),
        # The credentials this project's agents authenticate with. Shown beside the seats
        # because the walk kept asking "which key is that agent on" and the answer lived in a
        # different screen — and because a wave-tagged key is a wave artifact somebody may
        # want to clear without ending anything.
        "credentials": fleet_svc.list_credentials(db, project_id),
        # Only waves that still own something. History is not a thing you can end.
        "waves": fleet_svc.live_waves(db, project_id),
    }


class FleetKeyIn(BaseModel):
    project_id: str
    role: str
    wave: str = "wave-1"
    label: str = ""


@router.get("/presence")
def fleet_presence(project_id: str | None = None, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """Which code nodes are under an agent's hands right now (PRD-20 D4).

    **JWT only, and that restriction IS the privacy design rather than a check bolted beside
    it.** This payload says which HUMAN is editing which file, so it is reachable by a project
    member's session and by nothing else. An agent has no use for it — `graph_query` already
    answers what depends on the code it is about to touch, which is the question an agent
    actually has — and shipping this on the MCP surface would put a live map of everyone's
    activity behind every credential in the fleet. `test_cross_tenant.py` exists because this
    codebase has shipped isolation bugs; narrowing the surface removes the class rather than
    guarding it.
    """
    authz.require_readable(db, user.id, project_id)
    return fleet_svc.held_areas(db, project_id)


@router.post("/keys", status_code=201)
def mint_fleet_key(body: FleetKeyIn, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """A credential narrowed to one role and tagged to this wave.

    Plaintext is returned ONCE, as everywhere else — keys are stored hashed and cannot be
    recovered, which is a property rather than an oversight.
    """
    authz.require_writable(db, user.id, body.project_id)
    try:
        row, plaintext = fleet_svc.mint_fleet_key(
            db, user_id=user.id, project_id=body.project_id, role=body.role,
            wave=body.wave, label=body.label)
    except ValueError as e:
        raise HTTPException(422, str(e))
    events_svc.record_user(db, user, action="mint_fleet_key", target_type="apikey",
                           target_id=row.id, project_id=body.project_id,
                           meta={"role": body.role, "wave": body.wave})
    return {"id": row.id, "plaintext": plaintext, "role": body.role, "wave": body.wave,
            "expires_at": row.expires_at, "prefix": row.prefix,
            "tool_tiers": row.tool_tiers or []}


class SeatsIn(BaseModel):
    project_id: str
    # ONE ENTRY PER AGENT, repeats included: ["planner", "worker", "worker", "reviewer"].
    # Two agents on one seat share a session and cannot review each other.
    roles: list[str]
    # Blank means "the next one" — computed server-side, because the client hardcoded wave-1
    # and every wave for weeks landed in the same bucket.
    wave: str = ""


@router.post("/seats", status_code=201)
def issue_seats(body: SeatsIn, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """Issue a wave of seats. Each code is returned ONCE."""
    authz.require_writable(db, user.id, body.project_id)
    if not body.roles:
        raise HTTPException(422, "name at least one role")
    try:
        wave = body.wave or fleet_svc.next_wave(db, body.project_id)
        issued = fleet_svc.issue_wave(db, project_id=body.project_id, roles=body.roles,
                                      wave=wave, issued_by=user.id)
    except ValueError as e:
        raise HTTPException(422, str(e))
    events_svc.record_user(db, user, action="issue_seats", target_type="project",
                           target_id=body.project_id, project_id=body.project_id,
                           meta={"roles": body.roles, "wave": body.wave})
    return {"wave": wave,
            "seats": [{"id": row.id, "role": row.role, "code": code,
                       "expires_at": row.expires_at} for row, code in issued]}


@router.post("/seats/{seat_id}/reissue", status_code=201)
def reissue_seat(seat_id: str, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    """Replace a spent seat. The dead one stays as the record that something died."""
    row = db.get(Enrolment, seat_id)
    if row is None:
        raise HTTPException(404, "no such seat")
    authz.require_writable(db, user.id, row.project_id)
    fresh, code = fleet_svc.reissue_enrolment(db, enrolment_id=seat_id)
    events_svc.record_user(db, user, action="reissue_seat", target_type="project",
                           target_id=row.project_id, project_id=row.project_id,
                           meta={"replaces": seat_id, "role": fresh.role})
    return {"id": fresh.id, "role": fresh.role, "code": code, "expires_at": fresh.expires_at,
            "reissued_from": seat_id}


class RevokeSeatsIn(BaseModel):
    project_id: str
    wave: str | None = None


@router.post("/seats/revoke-unused")
def revoke_unused_seats(body: RevokeSeatsIn, db: Session = Depends(get_db),
                        user: User = Depends(get_current_user)):
    """Throw away seats nobody redeemed. Consumed seats are untouched — they record which
    agent took what, and ending the wave is what stops live sessions."""
    authz.require_writable(db, user.id, body.project_id)
    n = fleet_svc.revoke_unused_seats(db, project_id=body.project_id, wave=body.wave)
    events_svc.record_user(db, user, action="revoke_unused_seats", target_type="project",
                           target_id=body.project_id, project_id=body.project_id,
                           meta={"revoked": n, "wave": body.wave})
    return {"revoked": n}


@router.post("/keys/revoke-expired")
def revoke_expired_keys(body: RevokeSeatsIn, db: Session = Depends(get_db),
                        user: User = Depends(get_current_user)):
    """Revoke credentials that have already expired. Expired only — see the service."""
    authz.require_writable(db, user.id, body.project_id)
    n = fleet_svc.revoke_expired_keys(db, project_id=body.project_id)
    events_svc.record_user(db, user, action="revoke_expired_keys", target_type="project",
                           target_id=body.project_id, project_id=body.project_id,
                           meta={"revoked": n})
    return {"revoked": n}


class DismissIn(BaseModel):
    undo: bool = False


@router.post("/agents/{agent_id}/dismiss")
def dismiss_agent(agent_id: str, body: DismissIn, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """Hide an agent from the roster. Never deletes — see the model."""
    from app.models import Agent

    row = db.get(Agent, agent_id)
    if row is None:
        raise HTTPException(404, "no such agent")
    authz.require_writable(db, user.id, row.project_id)
    try:
        out = fleet_svc.dismiss_agent(db, agent_id=agent_id, undo=body.undo)
    except ValueError as e:
        raise HTTPException(409, str(e))
    events_svc.record_user(db, user, action="dismiss_agent" if not body.undo else "restore_agent",
                           target_type="agent", target_id=agent_id, project_id=row.project_id)
    return {"id": out.id, "dismissed": out.dismissed_at is not None}


@router.get("/end-wave")
def preview_end_wave(project_id: str | None = None, wave: str | None = None,
                     db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """What ending the wave would destroy, so the confirm can name it.

    A confirm reading "are you sure?" teaches people to click through it. One reading "revoke
    4 keys, release 3 leases?" is a decision.
    """
    authz.require_readable(db, user.id, project_id)
    return fleet_svc.end_wave_preview(db, project_id=project_id, wave=wave)


class EndWaveIn(BaseModel):
    project_id: str | None = None
    wave: str | None = None


@router.post("/end-wave")
def end_wave(body: EndWaveIn, db: Session = Depends(get_db),
             user: User = Depends(get_current_user)):
    """Revoke this wave's keys and release everything they hold. A hard stop.

    Only keys carrying a `fleet_wave` tag are touched — a hand-minted credential is somebody's
    long-lived key and revoking it would be a surprise this button never promised.
    """
    authz.require_writable(db, user.id, body.project_id)
    out = fleet_svc.end_wave(db, project_id=body.project_id, wave=body.wave)
    events_svc.record_user(db, user, action="end_fleet_wave", target_type="project",
                           target_id=body.project_id or "", project_id=body.project_id,
                           meta=out)
    return out


# ---- PRD-37: harness preferences and fleet policy -----------------------------------------
#
# The server stores and serves; the supervisor resolves. These routes exist for the Fleet
# view. A profile is the CALLER's own — there is no route that edits somebody else's taste.
# Policy is a project setting and takes the project's write gate, like every other one.

class ProfileIn(BaseModel):
    project_id: str | None = None
    defaults: list[str] = []
    weights: dict[str, float] = {}
    excludes: list[str] = []


class PolicyIn(BaseModel):
    project_id: str
    local_only: bool = False
    reviewer_cross_vendor: bool = False
    allowed_harnesses: list[str] = []


@router.get("/profile")
def read_profile(project_id: str | None = None, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    """The caller's default and project override side by side, plus which one is in force."""
    if project_id:
        authz.require_readable(db, user.id, project_id)
    return fleet_profiles.both(db, user.id, project_id)


@router.put("/profile")
def write_profile(body: ProfileIn, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    if body.project_id:
        authz.require_readable(db, user.id, body.project_id)
    try:
        row = fleet_profiles.set_profile(db, user_id=user.id, project_id=body.project_id,
                                         defaults=body.defaults, weights=body.weights,
                                         excludes=body.excludes)
    except fleet_profiles.ProfileInvalid as e:
        raise HTTPException(422, str(e))
    events_svc.record_user(db, user, action="set_fleet_profile", target_type="fleet_profile",
                           target_id=row.id, project_id=body.project_id,
                           meta={"scope": "project" if body.project_id else "default"})
    return fleet_profiles.summary(row)


@router.delete("/profile")
def delete_profile(project_id: str | None = None, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    if project_id:
        authz.require_readable(db, user.id, project_id)
    cleared = fleet_profiles.clear_profile(db, user_id=user.id, project_id=project_id)
    if cleared:
        events_svc.record_user(db, user, action="clear_fleet_profile", target_type="fleet_profile",
                               target_id=user.id, project_id=project_id,
                               meta={"scope": "project" if project_id else "default"})
    return {"cleared": cleared}


@router.get("/policy")
def read_policy(project_id: str, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    authz.require_readable(db, user.id, project_id)
    return {"project_id": project_id, "policy": fleet_profiles.policy_of(db, project_id)}


@router.put("/policy")
def write_policy(body: PolicyIn, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    authz.require_writable(db, user.id, body.project_id)
    project = db.get(Project, body.project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    try:
        policy = fleet_profiles.set_policy(db, project, body.model_dump(exclude={"project_id"}))
    except fleet_profiles.ProfileInvalid as e:
        raise HTTPException(422, str(e))
    events_svc.record_user(db, user, action="set_fleet_policy", target_type="project",
                           target_id=body.project_id, project_id=body.project_id,
                           meta={"policy": policy})
    return {"project_id": body.project_id, "policy": policy}


class AttemptIn(BaseModel):
    """The supervisor's post, in either of its two shapes (PRD-38 D3).

    Every field optional because the two shapes share one route: at launch the supervisor
    names the seat and what it resolved; at exit it names the delegation and what the run
    cost. Nothing here is required together with anything else except an address — a post
    that names neither has nowhere to land.
    """

    # --- the address ---
    # A launch post knows the code it is about to hand a child; an exit post knows the seat's
    # ROW id, read off the roster when the child registered, and typically not the delegation.
    enrolment_code: str | None = None
    enrolment_id: str | None = None
    delegation_id: str | None = None
    # --- the launch shape ---
    winner: str | None = None
    runner_up: str | None = None
    source: str | None = None
    adapter: str | None = None
    # --- the exit shape ---
    binary_version: str | None = None
    turns_used: int | None = None
    turn_budget: int | None = None
    wall_seconds: int | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    exit_meaning: str | None = None


@router.post("/attempts")
def post_attempt(body: AttemptIn, db: Session = Depends(get_db),
                 key: ApiKey = Depends(get_agent_key)):
    """What only the supervisor knows, posted twice per child (PRD-38 D3).

    **Authenticated by the key, not by a session**, because the caller is a supervisor process
    and not a human — the same shape `learning_loop` and the sync routes already use. It wants
    WRITE on the project, since a read-only credential has no business adding measurements to
    somebody's cells.

    There is no ordering requirement against the server's own derivation: whichever arrives
    first creates the row, and `harness.derive` fills its half whenever the outcome lands. A
    post for a delegation this key cannot read answers 404 rather than 403 — a key must not be
    able to learn that an id exists by the shape of its refusal.
    """
    if not (body.enrolment_code or body.enrolment_id or body.delegation_id):
        raise HTTPException(
            422, "an attempt post names an enrolment_code, an enrolment_id or a delegation_id")
    target = harness_svc.target_for(db, enrolment_code=body.enrolment_code,
                                    enrolment_id=body.enrolment_id,
                                    delegation_id=body.delegation_id)
    if target is None or not authz.can_write(db, key.user_id, target.project_id):
        raise HTTPException(404, "no such attempt")
    if body.enrolment_code:
        row = harness_svc.record_launch(db, target=target, winner=body.winner,
                                        runner_up=body.runner_up, source=body.source,
                                        adapter=body.adapter)
    else:
        row = harness_svc.record_exit(db, target=target, values={
            "binary_version": body.binary_version, "turns_used": body.turns_used,
            "turn_budget": body.turn_budget, "wall_seconds": body.wall_seconds,
            "tokens_in": body.tokens_in, "tokens_out": body.tokens_out,
            "exit_meaning": body.exit_meaning, "adapter_launched": body.adapter,
        })
    db.commit()
    # 202 means stored but not yet counted, and it is the honest answer to every post that
    # arrives before the outcome does — which a launch post always is. The runtime facts are
    # true whether or not an outcome ever follows; the supervisor must not read "stored" as
    # "in the numbers".
    if row.derived_at is None:
        return JSONResponse(status_code=202, content=harness_svc.row_dict(row))
    return harness_svc.row_dict(row)
