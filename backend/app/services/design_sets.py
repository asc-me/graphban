"""Design sets (GRPH-1279 / GRPH-P64).

An optional record on a PRD: the hashes a set was compiled from, its set constraints, and per
screen a tier, a prompt hash, a canvas hash and findings. Compile, findings and accept belong
to later items; this module owns the set, its staleness and its freeze.

A set that moved under a preamble edit or a PRD baseline change is `stale`, never hidden: it
says which hash moved, and its prompt rows stay retrievable with `stale_reason`.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import errors
from app.hashing import content_hash
from app.models import CodeNode, DesignScreen, DesignSet, Document, PlatformConfig
from app.services import prds as prd_svc

STATUSES = ("open", "accepted", "stale", "frozen")
TIERS = ("BACKED", "HALF", "UNBACKED")
STALE_REASONS = ("preamble", "prd_baseline")
_DESIGN_DOCS = ("design_prompt", "design_review")


def new_id() -> str:
    return f"DS-{uuid.uuid4().hex[:10]}"


def _preamble(db: Session, project_id: str) -> Document | None:
    return db.scalars(select(Document).where(
        Document.project_id == project_id, Document.kind == "preamble",
        Document.set_id == "", Document.governing.is_(True))).first()


def _prd_hash(db: Session, prd) -> tuple[str, bool]:
    """(hash, baselined): the baseline body hash, or the live body hash when none exists."""
    base = prd_svc.baseline_of(db, prd.id)
    if base is not None:
        return content_hash(base.body), True
    return content_hash(prd.body), False


def _get(db: Session, project_id: str, set_id: str) -> DesignSet:
    ds = db.get(DesignSet, set_id)
    if ds is None or ds.project_id != project_id:
        raise errors.NotFound(f"design set not found: {set_id}")
    return ds


def _writable(ds: DesignSet) -> None:
    if ds.status == "frozen":
        raise errors.Validation(
            f"{ds.id} is frozen: its PRD is closed",
            hint="a frozen design set stays readable and takes no further writes")


def _pin_preamble(db: Session, ds: DesignSet) -> None:
    pre = _preamble(db, ds.project_id)
    ds.preamble_version = pre.version if pre else 0
    ds.preamble_hash = pre.prd_hash if pre else ""


def open_design(db: Session, project_id: str, prd_id: str, created_by: str = "") -> dict:
    prd = prd_svc.get_prd(db, prd_id)
    if prd is None or prd.project_id != project_id:
        raise errors.NotFound(f"prd not found in this project: {prd_id}")
    if prd.status == "closed":
        raise errors.Validation(f"{prd.id} is closed", hint="a closed PRD takes no design set")
    existing = db.scalars(select(DesignSet).where(
        DesignSet.prd_id == prd.id, DesignSet.status != "frozen")).first()
    if existing is not None:
        # A stale set is the existing set, not a second one.
        raise errors.Validation(
            f"{prd.id} already has a {existing.status} design set: {existing.id}",
            hint="one set per PRD; a stale set is revised, not replaced")
    digest, baselined = _prd_hash(db, prd)
    ds = DesignSet(id=new_id(), project_id=project_id, prd_id=prd.id, status="open",
                   prd_hash=digest, prd_baselined=baselined, sections=[], created_by=created_by)
    _pin_preamble(db, ds)
    db.add(ds)
    db.commit()
    return to_dict(db, ds)


def put_preamble(db: Session, project_id: str, body: str, cites_prd_id: str,
                 created_by: str = "") -> dict:
    """Write the project's standing preamble; a new version stales the sets built on the old."""
    from app.services import documents as documents_svc

    out = documents_svc.put_document(
        db, project_id=project_id, kind="preamble", body=body, prd_id=cites_prd_id,
        created_by=created_by)
    return {"preamble_version": out["version"], "cites_prd_id": out["prd_id"],
            "prd_hash": out["prd_hash"], "content_hash": out["content_hash"], "id": out["id"]}


def put_set_constraints(db: Session, project_id: str, set_id: str, body: str,
                        sections: list[str] | None = None,
                        screens: list[dict] | None = None) -> dict:
    """Store the candidate set constraints. Does not accept them. Compiling re-pins the set to the
    current preamble and PRD hash, so revising a stale set is what clears the staleness."""
    ds = _get(db, project_id, set_id)
    _writable(ds)
    if ds.status == "accepted":
        raise errors.Validation(f"{ds.id} is accepted", hint="its constraints are accepted as a unit")
    if not (body or "").strip():
        raise errors.Validation("body is empty", hint="constraints need text")
    prd = prd_svc.get_prd(db, ds.prd_id)
    ds.prd_hash, ds.prd_baselined = _prd_hash(db, prd)
    _pin_preamble(db, ds)
    ds.constraints_body, ds.constraints_hash = body, content_hash(body)
    ds.constraints_accepted = False
    if sections is not None:
        ds.sections = [str(s) for s in sections]
    for s in screens or []:
        _screen(db, ds, str(s["screen"]), s.get("routes"))
    ds.status, ds.stale_reason, ds.stale_detail = "open", "", None
    db.commit()
    return to_dict(db, ds)


def propose_tier(db: Session, project_id: str, routes: list[str]) -> tuple[str, str]:
    """(tier, basis) from the code graph. Anything that is not a present route node is UNBACKED,
    including a graph never pushed and a project with `sync_graph` off."""
    cfg = db.get(PlatformConfig, project_id)
    if cfg is not None and not cfg.sync_graph:
        return "UNBACKED", "sync_graph is off for this project"
    if not routes:
        return "UNBACKED", "the screen lists no routes"
    have = {p for p in db.scalars(select(CodeNode.path).where(
        CodeNode.project_id == project_id, CodeNode.path.in_(routes)))}
    if not have:
        return "UNBACKED", "no listed route is in the code graph (or it was never pushed)"
    if len(have) < len(set(routes)):
        return "HALF", f"{len(have)} of {len(set(routes))} routes are in the code graph"
    return "BACKED", "every listed route is in the code graph"


def _screen(db: Session, ds: DesignSet, name: str, routes: list[str] | None) -> DesignScreen:
    row = next((s for s in ds.screens if s.screen == name), None)
    if row is None:
        row = DesignScreen(screen=name, findings=[])
        ds.screens.append(row)
    if routes is not None:
        row.routes = [str(r) for r in routes]
    row.tier_proposal, row.tier_proposal_basis = propose_tier(db, ds.project_id, row.routes or [])
    return row


def confirm_tier(db: Session, project_id: str, set_id: str, screen: str, tier: str, note: str,
                 routes: list[str] | None = None, confirmed_by: str = "") -> dict:
    ds = _get(db, project_id, set_id)
    _writable(ds)
    if tier not in TIERS:
        raise errors.Validation(f"unknown tier {tier!r}", hint=f"one of {', '.join(TIERS)}")
    if not (note or "").strip():
        raise errors.Validation("note is empty", hint="say what backs the screen or what is missing")
    row = _screen(db, ds, screen, routes)
    row.tier, row.tier_confirmed, row.tier_note = tier, True, note.strip()
    row.confirmed_by = confirmed_by
    db.commit()
    return to_dict(db, ds)


def acceptance_blockers(ds: DesignSet) -> list[str]:
    """Why this set cannot be accepted now (empty means nothing here blocks it)."""
    out = []
    if ds.status == "frozen":
        out.append("frozen: the PRD is closed")
    if ds.status == "stale":
        out.append(f"stale: {ds.stale_reason} moved")
    if not ds.prd_baselined:
        out.append("the PRD has no baseline: a design cannot govern a spec that was not grilled")
    return out


def screen_dict(s: DesignScreen) -> dict:
    return {"screen": s.screen, "routes": s.routes or [], "tier": s.tier,
            "tier_confirmed": s.tier_confirmed, "tier_note": s.tier_note,
            "tier_proposal": s.tier_proposal, "tier_proposal_basis": s.tier_proposal_basis,
            "prompt_hash": s.prompt_hash, "canvas_hash": s.canvas_hash,
            "findings": s.findings or []}


def to_dict(db: Session, ds: DesignSet) -> dict:
    out = {
        "id": ds.id, "project_id": ds.project_id, "prd_id": ds.prd_id, "status": ds.status,
        "preamble_version": ds.preamble_version, "preamble_hash": ds.preamble_hash,
        "constraints_hash": ds.constraints_hash, "constraints_accepted": ds.constraints_accepted,
        "sections": ds.sections or [], "prd_hash": ds.prd_hash, "prd_baselined": ds.prd_baselined,
        "screens": [screen_dict(s) for s in ds.screens],
        "acceptance_blockers": acceptance_blockers(ds),
    }
    if ds.status == "stale":
        out["stale_reason"], out["stale_detail"] = ds.stale_reason, ds.stale_detail or {}
    return out


def get_design_set(db: Session, project_id: str, set_id: str) -> dict:
    return to_dict(db, _get(db, project_id, set_id))


def _mark_stale(db: Session, ds: DesignSet, reason: str, was: str, now: str) -> None:
    ds.status, ds.stale_reason = "stale", reason
    ds.stale_detail = {reason: {"was": was, "now": now}}
    # The set's prompt and review rows stay retrievable, labelled: stale is not governing.
    for d in db.scalars(select(Document).where(
            Document.set_id == ds.id, Document.kind.in_(_DESIGN_DOCS))):
        d.status, d.stale_reason = "stale", reason


def refresh(db: Session, project_id: str, prd_id: str | None = None) -> int:
    """Re-check every live set of the project against the current preamble and PRD hashes, marking
    the ones that moved `stale`. Called after a preamble write or a baseline change. Returns the
    number marked."""
    pre = _preamble(db, project_id)
    n = 0
    for ds in db.scalars(select(DesignSet).where(
            DesignSet.project_id == project_id, DesignSet.status.in_(("open", "accepted")))):
        if pre is not None and ds.preamble_version and (
                pre.version != ds.preamble_version or pre.prd_hash != ds.preamble_hash):
            _mark_stale(db, ds, "preamble", ds.preamble_hash, pre.prd_hash)
            n += 1
            continue
        prd = prd_svc.get_prd(db, ds.prd_id)
        if prd is None or (prd_id and ds.prd_id != prd_id):
            continue
        digest, baselined = _prd_hash(db, prd)
        if ds.prd_baselined and (not baselined or digest != ds.prd_hash):
            _mark_stale(db, ds, "prd_baseline", ds.prd_hash, digest if baselined else "")
            n += 1
        elif not ds.prd_baselined and baselined:
            if digest != ds.prd_hash:
                _mark_stale(db, ds, "prd_baseline", ds.prd_hash, digest)
                n += 1
            else:
                ds.prd_baselined = True
    db.commit()
    return n


def freeze_for_prd(db: Session, prd_id: str) -> int:
    """A closed PRD freezes its design set: readable, no further writes."""
    n = 0
    for ds in db.scalars(select(DesignSet).where(
            DesignSet.prd_id == prd_id, DesignSet.status != "frozen")):
        ds.status = "frozen"
        n += 1
    db.commit()
    return n
