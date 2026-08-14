"""The Fleet view's server half (GRPH-336 / PRD-17 D5).

One read that answers the view's whole question — who is out there, what is waiting for a
second pair of eyes, and which clusters are held back and why — plus the two writes that make
a wave: mint a role-narrowed credential, and end the wave.

Session-authenticated, unlike the MCP surface beside it. The caller here is a human deciding
how to spend a fleet, not an agent working inside one.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Enrolment, User
from app.security import authz
from app.security.deps import get_current_user
from app.services import events as events_svc
from app.services import fleet as fleet_svc

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
    status = fleet_svc.fleet_status(db, project_id)
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
            "expires_at": row.expires_at, "prefix": row.prefix}


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
