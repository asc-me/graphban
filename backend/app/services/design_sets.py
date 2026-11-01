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
from app.models import (
    Agent, CodeNode, DesignAcceptance, DesignCanvas, DesignCorrection, DesignFinding, DesignScreen,
    DesignSet, Document, PlatformConfig,
)
from app.services import prds as prd_svc

STATUSES = ("open", "accepted", "stale", "frozen")
TIERS = ("BACKED", "HALF", "UNBACKED")
STALE_REASONS = ("preamble", "prd_baseline")
FINDING_KINDS = ("prompt_defect", "design_edit", "authority_conflict", "spec_gap")
RESOLUTIONS = ("applied", "rejected", "withdrawn")
# Who wins an authority conflict, in order: a shipped decision, the PRD for what the server can
# answer, then the design for layout and copy.
WINNERS = ("shipped_decision", "prd", "design")
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


def _stale_cite(db: Session, ds: DesignSet) -> bool:
    """Mark `ds` stale when the preamble it pins cites a superseded baseline. Run at every pin as
    well as in `refresh`, so a set's state never waits on some unrelated event to catch up."""
    pre = _preamble(db, ds.project_id)
    if pre is None:
        return False
    cited = _cited_hash(db, pre)
    if cited == ds.preamble_hash:
        return False
    _mark_stale(db, ds, "prd_baseline", ds.preamble_hash, cited, prd_id=pre.prd_id)
    return True


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
    _stale_cite(db, ds)
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
    # Re-pinning to a preamble that cites a superseded baseline does not clear staleness.
    _stale_cite(db, ds)
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
        "screens": [screen_dict(s) | {"findings": [
            finding_dict(f) for f in _findings(db, ds.id, s.screen)]} for s in ds.screens],
        "acceptance_blockers": acceptance_blockers(ds),
    }
    if ds.status == "stale":
        out["stale_reason"], out["stale_detail"] = ds.stale_reason, ds.stale_detail or {}
    return out


def get_design_set(db: Session, project_id: str, set_id: str) -> dict:
    return to_dict(db, _get(db, project_id, set_id))


def _cited_hash(db: Session, pre: Document | None) -> str:
    """The current baseline hash of the PRD the preamble cites ("" when it has none now)."""
    if pre is None or not pre.prd_id:
        return ""
    base = prd_svc.baseline_of(db, pre.prd_id)
    return content_hash(base.body) if base is not None else ""


def _mark_stale(db: Session, ds: DesignSet, reason: str, was: str, now: str,
                prd_id: str | None = None) -> None:
    ds.status, ds.stale_reason = "stale", reason
    ds.stale_detail = {reason: {"was": was, "now": now, "prd_id": prd_id or ds.prd_id}}
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
        # A set pinned to no preamble (version 0) was compiled without one, so the first preamble
        # write moves its inputs exactly as an edit does.
        if pre is not None and (
                pre.version != ds.preamble_version or pre.prd_hash != ds.preamble_hash):
            _mark_stale(db, ds, "preamble", ds.preamble_hash, pre.prd_hash)
            n += 1
            continue
        # The preamble's cited PRD was rebaselined: the hash the preamble pinned no longer matches.
        if _stale_cite(db, ds):
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


# ---- Review loop (GRPH-1280) -----------------------------------------------------------------


def _norm(who: str) -> str:
    return (who or "").removeprefix("key:").strip().lower()


def _person(db: Session, actor: str, on_behalf_of: str) -> tuple[str, str]:
    """(person, relayed_by). Every caller here is a relay, the way `answer_grill` records an MCP
    call as via=agent: a bare credential (`key:<name>`) is what any agent becomes by omitting
    agent_id, so it is never a person by itself. The named person cannot be a credential or a
    registered agent either."""
    person = (on_behalf_of or "").strip()
    if not person:
        raise errors.Validation(
            "this decision is a person's, and no person is named",
            hint="ask the person, then pass accepted_by with their name; an agent cannot decide")
    if person.lower().startswith("key:") or db.get(Agent, person.removeprefix("agent:")):
        raise errors.Validation(
            f"accepted_by {person!r} is an agent or a credential, not a person",
            hint="name the person whose decision this is")
    return person, actor


def _prompts(db: Session, ds_id: str, screen: str | None = None) -> list[Document]:
    q = select(Document).where(Document.set_id == ds_id, Document.kind == "design_prompt")
    if screen is not None:
        q = q.where(Document.screen == screen)
    return list(db.scalars(q.order_by(Document.version)))


def _latest_prompt(db: Session, ds_id: str, screen: str) -> Document | None:
    rows = _prompts(db, ds_id, screen)
    return rows[-1] if rows else None


def _screen_row(ds: DesignSet, screen: str) -> DesignScreen:
    row = next((s for s in ds.screens if s.screen == screen), None)
    if row is None:
        raise errors.NotFound(f"{ds.id} has no screen {screen!r}")
    return row


def _findings(db: Session, ds_id: str, screen: str | None = None) -> list[DesignFinding]:
    q = select(DesignFinding).where(DesignFinding.design_set_id == ds_id)
    if screen is not None:
        q = q.where(DesignFinding.screen == screen)
    return list(db.scalars(q.order_by(DesignFinding.created_at, DesignFinding.id)))


def finding_dict(f: DesignFinding) -> dict:
    return {"id": f.id, "screen": f.screen, "kind": f.kind, "text": f.text,
            "canvas_hash": f.canvas_hash, "status": f.status, "reason": f.reason,
            "winner": f.winner, "correction_id": f.correction_id, "created_by": f.created_by}


def _chunk_text(doc: Document) -> str:
    return "\n\n".join(c.text for c in sorted(doc.chunks, key=lambda c: c.ordinal)
                        if c.governing)


def _compile_text(db: Session, ds: DesignSet, row: DesignScreen, prd, base) -> str:
    pre = _preamble(db, ds.project_id)
    corrections = list(db.scalars(select(DesignCorrection).where(
        DesignCorrection.design_set_id == ds.id, DesignCorrection.screen == row.screen,
        DesignCorrection.status == "open").order_by(DesignCorrection.id)))
    body = base.body if base is not None else prd.body
    head = [f"## Screen\n{row.screen}"]
    if base is None:
        head.append("## Not acceptable\nThe PRD has no baseline; this prompt cannot be accepted.")
    head.append(f"## Tier\n{row.tier} - {row.tier_note}")
    head.append(f"## Governing PRD\n{prd.id} {ds.prd_hash}\n{body}")
    head.append(f"## Set constraints\n{ds.constraints_body}")
    head.append("## Standing preamble\n" + (_chunk_text(pre) if pre is not None else "(none)"))
    head.append("## Corrections\n" + ("\n".join(f"[C{c.id}] {c.text}" for c in corrections)
                                      if corrections else "(none)"))
    return "\n\n".join(head) + "\n"


def compile_prompts(db: Session, project_id: str, set_id: str, screen: str | None = None,
                    actor: str = "") -> dict:
    """Compile candidate `design_prompt` rows. A pure function of the PRD text, the accepted set
    constraints, the preamble, each confirmed tier and the open corrections: the same inputs give
    the same prompt hash and no new row. Writes nothing when any screen in it is refused."""
    ds = _get(db, project_id, set_id)
    _writable(ds)
    rows = [_screen_row(ds, screen)] if screen is not None else list(ds.screens)
    if not rows:
        raise errors.Validation(f"{ds.id} has no screens", hint="put_set_constraints lists them")
    unconfirmed = [r.screen for r in rows if not r.tier_confirmed]
    if unconfirmed or not ds.constraints_accepted:
        why = []
        if unconfirmed:
            why.append(f"tier not confirmed on: {', '.join(unconfirmed)}")
        if not ds.constraints_accepted:
            why.append("the set constraints are not accepted")
        raise errors.Validation("; ".join(why), hint="confirm_tier names a tier; a prompt never "
                                "claims BACKED for an unconfirmed one")
    if ds.status == "stale":
        # Recompiling is what re-pins a stale set to the current hashes.
        prd = prd_svc.get_prd(db, ds.prd_id)
        ds.prd_hash, ds.prd_baselined = _prd_hash(db, prd)
        _pin_preamble(db, ds)
        ds.status, ds.stale_reason, ds.stale_detail = "open", "", None
        _stale_cite(db, ds)
    prd = prd_svc.get_prd(db, ds.prd_id)
    base = prd_svc.baseline_of(db, prd.id)
    out = []
    for row in rows:
        text = _compile_text(db, ds, row, prd, base)
        digest = content_hash(text)
        latest = _latest_prompt(db, ds.id, row.screen)
        new = latest is None or latest.content_hash != digest
        if new:
            from app.services import documents as documents_svc

            latest = Document(
                id=documents_svc.new_id(), project_id=project_id, kind="design_prompt",
                title=f"{row.screen} prompt", prd_id=ds.prd_id, prd_hash=ds.prd_hash,
                set_id=ds.id, screen=row.screen, version=(latest.version + 1) if latest else 1,
                status="active", content_hash=digest, governing=False, accepted=False,
                created_by=actor)
            documents_svc._chunk(latest, text)
            db.add(latest)
        out.append({"screen": row.screen, "prompt_hash": digest, "document_id": latest.id,
                    "version": latest.version, "new": new, "acceptable": base is not None})
    db.commit()
    return {"design_set_id": ds.id, "prompts": out}


def accept_prompt(db: Session, project_id: str, set_id: str, screen: str, actor: str,
                  on_behalf_of: str = "") -> dict:
    """A person accepts the screen's current candidate prompt. Not the design, and not an embed."""
    ds = _get(db, project_id, set_id)
    _writable(ds)
    row = _screen_row(ds, screen)
    cand = _latest_prompt(db, ds.id, screen)
    if cand is None:
        raise errors.Validation(f"{screen!r} has no compiled prompt", hint="compile_prompts first")
    person, relayed = _person(db, actor, on_behalf_of)
    if _norm(cand.created_by) in (_norm(person), _norm(relayed)) and cand.created_by:
        raise errors.Validation(
            "the compiler cannot accept its own prompt",
            hint="a person other than the actor that compiled it accepts it")
    base = prd_svc.baseline_of(db, ds.prd_id)
    if base is None or content_hash(base.body) != cand.prd_hash:
        raise errors.Validation(
            "this prompt is not acceptable: its PRD is not the current baseline",
            hint="baseline the PRD, then compile_prompts again")
    if row.prompt_hash != cand.content_hash:
        row.prompt_hash = cand.content_hash
        row.canvas_hash = ""  # a canvas drawn from an older prompt is superseded, not deleted
    db.add(DesignAcceptance(design_set_id=ds.id, kind="prompt", screen=screen,
                            prompt_hash=cand.content_hash, person=person, relayed_by=relayed))
    db.commit()
    return {"design_set_id": ds.id, "screen": screen, "prompt_hash": row.prompt_hash,
            "accepted_by": person, "relayed_by": relayed}


def put_canvas(db: Session, project_id: str, set_id: str, screen: str, data: bytes, mime: str,
               prompt_hash: str, actor: str = "", backend=None) -> dict:
    """Always stores the bytes (immutable, keyed by hash). They become the screen's reviewable
    canvas only when `prompt_hash` is its accepted prompt hash; any other is an orphan."""
    from app.services import documents as documents_svc

    ds = _get(db, project_id, set_id)
    _writable(ds)
    row = _screen_row(ds, screen)
    if not data:
        raise errors.Validation("bytes are empty")
    if not (mime or "").strip():
        raise errors.Validation("mime is empty")
    # One identity per distinct bytes, so a second upload never replaces the first.
    import hashlib

    digest = hashlib.sha256(data).hexdigest()
    doc = documents_svc.put_blob(
        db, project_id=project_id, kind="canvas", data=data, mime=mime, prd_id=ds.prd_id,
        set_id=ds.id, screen=f"{screen}@{digest[:16]}", title=f"{screen} canvas",
        created_by=actor, backend=backend)
    have = db.scalars(select(DesignCanvas).where(
        DesignCanvas.design_set_id == ds.id, DesignCanvas.screen == screen,
        DesignCanvas.content_hash == digest, DesignCanvas.prompt_hash == prompt_hash)).first()
    if have is None:
        db.add(DesignCanvas(design_set_id=ds.id, screen=screen, content_hash=digest,
                            prompt_hash=prompt_hash, document_id=doc["id"], created_by=actor))
    bound = bool(prompt_hash) and prompt_hash == row.prompt_hash
    if bound:
        row.canvas_hash = digest
    db.commit()
    return {"design_set_id": ds.id, "screen": screen, "content_hash": digest,
            "document_id": doc["id"], "prompt_hash": prompt_hash, "bound": bound,
            "orphan": not bound}


def read_canvas(db: Session, project_id: str, set_id: str, content_hash_: str, backend=None):
    """Fetch stored canvas bytes by content hash (orphans included)."""
    from app.services import documents as documents_svc

    ds = _get(db, project_id, set_id)
    cv = db.scalars(select(DesignCanvas).where(
        DesignCanvas.design_set_id == ds.id, DesignCanvas.content_hash == content_hash_)).first()
    if cv is None:
        raise errors.NotFound(f"no canvas {content_hash_} in {ds.id}")
    return documents_svc.read_blob(db, project_id, cv.document_id, backend=backend)


def _headings(body: str) -> list[str]:
    import re

    return [m.group(1).strip() for m in re.finditer(r"^#{1,6}\s+(.*\S)\s*$", body or "", re.M)]


def _add_correction(db: Session, ds: DesignSet, screen: str, text: str, actor: str) -> DesignCorrection:
    digest = content_hash(text)
    have = db.scalars(select(DesignCorrection).where(
        DesignCorrection.design_set_id == ds.id, DesignCorrection.screen == screen,
        DesignCorrection.text_hash == digest)).first()
    if have is not None:
        have.status = "open"
        return have
    c = DesignCorrection(design_set_id=ds.id, screen=screen, text=text, text_hash=digest,
                         created_by=actor)
    db.add(c)
    db.flush()
    return c


def file_finding(db: Session, project_id: str, set_id: str, screen: str, kind: str, text: str,
                 canvas_hash: str = "", winner: str = "", actor: str = "") -> dict:
    ds = _get(db, project_id, set_id)
    _writable(ds)
    row = _screen_row(ds, screen)
    if kind not in FINDING_KINDS:
        raise errors.Validation(f"unknown kind {kind!r}", hint=f"one of {', '.join(FINDING_KINDS)}")
    text = (text or "").strip()
    if not text:
        raise errors.Validation("text is empty")
    cand = _latest_prompt(db, ds.id, screen)
    if cand is not None and cand.created_by and _norm(cand.created_by) == _norm(actor):
        raise errors.Validation(
            "the actor that compiled the current prompt cannot file a finding on it",
            hint="another writer files the finding")
    if kind != "spec_gap" or canvas_hash:
        known = db.scalars(select(DesignCanvas.id).where(
            DesignCanvas.design_set_id == ds.id, DesignCanvas.screen == screen,
            DesignCanvas.content_hash == canvas_hash)).first()
        if known is None:
            raise errors.Validation(
                f"canvas_hash {canvas_hash!r} is not a canvas stored for {screen!r}",
                hint="name the hash of the canvas that was reviewed")
    if kind == "authority_conflict" and winner not in WINNERS:
        raise errors.Validation(f"winner must be one of {', '.join(WINNERS)}",
                                hint="shipped decision, then the PRD, then the design")
    if kind == "spec_gap":
        prd = prd_svc.get_prd(db, ds.prd_id)
        base = prd_svc.baseline_of(db, ds.prd_id)
        heads = _headings(base.body if base is not None else prd.body)
        if not any(h.lower() in text.lower() for h in heads):
            raise errors.Validation(
                "a spec_gap cites a PRD section heading, and this text cites none",
                hint=f"name one of: {', '.join(heads) or '(the PRD has no headings)'}")
    digest = content_hash(text)
    dup = db.scalars(select(DesignFinding).where(
        DesignFinding.design_set_id == ds.id, DesignFinding.screen == screen,
        DesignFinding.text_hash == digest)).first()
    if dup is not None:
        return finding_dict(dup) | {"duplicate": True}
    f = DesignFinding(id=f"FND-{uuid.uuid4().hex[:10]}", design_set_id=ds.id, screen=screen,
                      kind=kind, text=text, text_hash=digest, canvas_hash=canvas_hash,
                      winner=winner if kind == "authority_conflict" else "", created_by=actor)
    if kind in ("prompt_defect", "design_edit", "authority_conflict"):
        ctext = text if kind != "authority_conflict" else (
            f"Authority conflict ({f.winner} governs): draw the absence of - {text}")
        f.correction_id = _add_correction(db, ds, screen, ctext, actor).id
        if canvas_hash and canvas_hash == row.canvas_hash:
            row.canvas_hash = ""  # the reviewed canvas is superseded, not deleted
    db.add(f)
    db.commit()
    recompiled = None
    if (f.correction_id and cand is not None and ds.status != "stale"
            and ds.constraints_accepted and row.tier_confirmed):
        # Only this screen recompiles, and only now that the correction is stored. The new
        # candidate keeps its compiler's name: the filer did not author the compile, and
        # stamping them would bar them from filing the next finding.
        recompiled = compile_prompts(db, project_id, ds.id, screen,
                                     actor=cand.created_by)["prompts"][0]
    return finding_dict(f) | {"duplicate": False, "recompiled": recompiled}


def resolve_finding(db: Session, project_id: str, finding_id: str, status: str, reason: str = "",
                    actor: str = "") -> dict:
    f = db.get(DesignFinding, finding_id)
    ds = db.get(DesignSet, f.design_set_id) if f is not None else None
    if f is None or ds is None or ds.project_id != project_id:
        raise errors.NotFound(f"finding not found: {finding_id}")
    _writable(ds)
    if status not in RESOLUTIONS:
        raise errors.Validation(f"unknown status {status!r}", hint=f"one of {', '.join(RESOLUTIONS)}")
    if f.status != "open":
        raise errors.Validation(f"{f.id} is already {f.status}")
    if status in ("rejected", "withdrawn") and not (reason or "").strip():
        raise errors.Validation(f"{status} needs a reason")
    corr = db.get(DesignCorrection, f.correction_id) if f.correction_id else None
    if status == "applied":
        latest = _latest_prompt(db, ds.id, f.screen)
        if corr is None or latest is None or f"[C{corr.id}]" not in _chunk_text(latest):
            raise errors.Validation(
                "applied needs its correction in the latest compiled prompt for the screen",
                hint="store the correction, then compile_prompts for this screen"
                     if corr else "a spec_gap has no correction; withdraw it or request_rebaseline")
    elif corr is not None:
        corr.status = "withdrawn"
    f.status, f.reason, f.resolved_by = status, (reason or "").strip(), actor
    db.commit()
    return finding_dict(f)


def withdraw_spec_gaps(db: Session, prd_id: str) -> int:
    """A rebaseline was requested on the PRD: its open spec gaps stay open no longer."""
    n = 0
    for f in db.scalars(select(DesignFinding).join(
            DesignSet, DesignSet.id == DesignFinding.design_set_id).where(
            DesignSet.prd_id == prd_id, DesignFinding.kind == "spec_gap",
            DesignFinding.status == "open")):
        f.status, f.reason = "withdrawn", "request_rebaseline on the PRD"
        n += 1
    db.commit()
    return n


def _review_record(db: Session, ds: DesignSet) -> str:
    lines = []
    for f in _findings(db, ds.id):
        if f.status != "applied":
            continue
        corr = db.get(DesignCorrection, f.correction_id) if f.correction_id else None
        lines.append(f"- [x] {f.screen}: {f.kind}: {corr.text if corr else f.text}")
    return "## Review record\n" + ("\n".join(lines) if lines else "No findings were applied.")


def accept_design(db: Session, project_id: str, set_id: str, actor: str,
                  on_behalf_of: str = "") -> dict:
    from app.services import documents as documents_svc

    ds = _get(db, project_id, set_id)
    _writable(ds)
    person, relayed = _person(db, actor, on_behalf_of)
    why = list(acceptance_blockers(ds))
    base = prd_svc.baseline_of(db, ds.prd_id)
    if base is not None and content_hash(base.body) != ds.prd_hash:
        why.append("the set's PRD hash is not the current baseline hash")
    pre = _preamble(db, project_id)
    if (pre.prd_hash if pre else "") != ds.preamble_hash:
        why.append("the set's preamble hash is not the current preamble")
    compilers = {_norm(d.created_by) for d in _prompts(db, ds.id) if d.created_by}
    if _norm(person) in compilers or (relayed and _norm(relayed) in compilers):
        why.append("the accepting person compiled a prompt of this set")
    if not ds.screens:
        why.append("the set has no screens")
    accepted_docs: list[Document] = []
    for s in ds.screens:
        doc = next((d for d in reversed(_prompts(db, ds.id, s.screen))
                    if s.prompt_hash and d.content_hash == s.prompt_hash), None)
        if doc is None:
            why.append(f"{s.screen}: no accepted prompt")
            continue
        accepted_docs.append(doc)
        latest = _latest_prompt(db, ds.id, s.screen)
        if latest is not None and latest.content_hash != s.prompt_hash:
            # A correction (or any input) moved the prompt after it was accepted: the canvas
            # drawn from the accepted one does not carry it.
            why.append(f"{s.screen}: a newer prompt was compiled after the accepted one; "
                       "accept_prompt it and export against its hash")
        bound = s.canvas_hash and db.scalars(select(DesignCanvas.id).where(
            DesignCanvas.design_set_id == ds.id, DesignCanvas.screen == s.screen,
            DesignCanvas.content_hash == s.canvas_hash,
            DesignCanvas.prompt_hash == s.prompt_hash)).first()
        if not bound:
            why.append(f"{s.screen}: no canvas stored against the accepted prompt")
    open_f = [f.id for f in _findings(db, ds.id) if f.status == "open"]
    if open_f:
        why.append(f"open findings: {', '.join(open_f)}")
    if why:
        raise errors.Validation("cannot accept the design: " + "; ".join(why),
                                hint="each reason names what to fix")
    # Accepted: pin the hashes, then (and only then) embed the prompts and the review record.
    db.add(DesignAcceptance(design_set_id=ds.id, kind="design", prd_hash=ds.prd_hash,
                            preamble_hash=ds.preamble_hash, person=person, relayed_by=relayed))
    ds.status, ds.stale_reason, ds.stale_detail = "accepted", "", None
    for doc in accepted_docs:
        for old in _prompts(db, ds.id, doc.screen):
            if old.governing and old.id != doc.id:
                old.governing = False
                for c in old.chunks:
                    c.governing = False
        doc.governing, doc.accepted, doc.status, doc.stale_reason = True, True, "active", ""
        documents_svc.embed_document(doc)
    reviews = list(db.scalars(select(Document).where(
        Document.set_id == ds.id, Document.kind == "design_review").order_by(Document.version)))
    for r in reviews:
        r.governing = False
        for c in r.chunks:
            c.governing = False
    review = Document(
        id=documents_svc.new_id(), project_id=project_id, kind="design_review",
        title="Review record", prd_id=ds.prd_id, prd_hash=ds.prd_hash, set_id=ds.id,
        version=(reviews[-1].version + 1) if reviews else 1, status="active",
        governing=True, accepted=True, created_by=actor)
    record = _review_record(db, ds)
    review.content_hash = content_hash(record)
    documents_svc._chunk(review, record)
    db.add(review)
    db.flush()
    documents_svc.embed_document(review)
    db.commit()
    return to_dict(db, ds) | {"accepted_by": person, "relayed_by": relayed,
                              "review_record_id": review.id}
