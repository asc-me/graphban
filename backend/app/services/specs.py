"""What Specs shows (GRPH-P66).

`library` is the one list the Specs page reads (GRPH-1318): PRD rows, the preamble, and
design-set documents, with each PRD's sync fact and no document bodies. `prd_facts`,
`preamble_view`, and `design_set_view` are the Spec reader (GRPH-1319). Every write goes
through the owning service (`prds`, `design_sets`, `documents`); nothing here writes.
`sync_storage.status` may create an empty config row on its first read. That is the only
side effect, and this module never runs a sync.

The body of a document has three honest states, never two: `stored` (the text, whose hash
matches the row — including an empty body), `pending` (the row exists, its body has not
landed), and `unreadable` (what is held does not hash to the row). A pending or unreadable
body is never returned as an empty string, because a blank reader reads as an empty spec.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import errors
from app.hashing import content_hash
from app.models import DesignAcceptance, DesignSet, Document
from app.services import design_sets as design_svc
from app.services import prds as prd_svc


def _body(text: str | None, digest: str, blob_status: str = "none") -> dict:
    if not digest:
        return {"body_state": "absent", "body": None}  # nothing has been written yet
    if blob_status == "pending":
        return {"body_state": "pending", "body": None}
    if text is None or content_hash(text) != digest:
        return {"body_state": "unreadable", "body": None}
    return {"body_state": "stored", "body": text}


def _reassemble(doc: Document) -> str:
    """The preamble text as `documents.extract` cut it. The row keeps sections, not the raw
    upload, so this matches `content_hash` only when the author's text was already in this
    shape; otherwise the body is `unreadable` and the sections are still returned."""
    parts = []
    for c in sorted((c for c in doc.chunks if c.governing), key=lambda c: c.ordinal):
        parts.append(f"## {c.heading}\n{c.text}" if c.heading else c.text)
    return "\n\n".join(parts)


def prd_facts(db: Session, project_id: str, prd_id: str) -> dict:
    """Hashes for the reader's header, and the PRD's design set when one exists."""
    prd = prd_svc.get_prd(db, prd_id)
    if prd is None or prd.project_id != project_id:
        raise errors.NotFound(f"prd not found in this project: {prd_id}")
    base = prd_svc.baseline_of(db, prd.id)
    ds = db.scalars(select(DesignSet).where(DesignSet.prd_id == prd.id)
                    .order_by(DesignSet.created_at.desc())).first()
    return {
        "prd_id": prd.id, "status": prd.status, "version": prd.version,
        "content_hash": content_hash(prd.body or ""),
        # None is "no baseline", which the reader labels — never an empty hash.
        "baseline_hash": content_hash(base.body) if base is not None else None,
        "baseline_version": base.version if base is not None else None,
        "design_set": design_set_view(db, project_id, ds.id) if ds is not None else None,
    }


def _preamble_rows(db: Session, project_id: str) -> list[Document]:
    return list(db.scalars(select(Document).where(
        Document.project_id == project_id, Document.kind == "preamble",
        Document.set_id == "", Document.screen == "").order_by(Document.version)))


def preamble_view(db: Session, project_id: str) -> dict | None:
    """The governing preamble, its versions, and the sets that pin an older one. None when
    the project has no preamble row: the reader says not found."""
    rows = _preamble_rows(db, project_id)
    current = next((r for r in reversed(rows) if r.governing), None)
    if current is None:
        return None
    sets = db.scalars(select(DesignSet).where(DesignSet.project_id == project_id)).all()
    pinned_old = [
        {"id": s.id, "prd_id": s.prd_id, "status": s.status,
         "stale_reason": s.stale_reason if s.status == "stale" else "",
         "preamble_version": s.preamble_version}
        for s in sets
        if s.preamble_version and s.preamble_version != current.version
    ]
    return {
        "id": current.id, "version": current.version, "title": current.title,
        "cites_prd_id": current.prd_id, "prd_hash": current.prd_hash,
        "content_hash": current.content_hash, "created_by": current.created_by,
        "sections": [{"heading": c.heading, "text": c.text}
                     for c in sorted(current.chunks, key=lambda c: c.ordinal) if c.governing],
        **_body(_reassemble(current), current.content_hash, current.blob_status),
        "history": [{"version": r.version, "content_hash": r.content_hash,
                     "created_by": r.created_by, "governing": r.governing} for r in rows],
        "stale_sets": pinned_old,
    }


def _prompt_state(db: Session, ds: DesignSet, screen: str) -> str:
    latest = design_svc._latest_prompt(db, ds.id, screen)
    if latest is None:
        return "none"
    if latest.status == "stale":
        return "stale"
    return "accepted" if latest.accepted else "compiled"


def design_set_view(db: Session, project_id: str, set_id: str) -> dict:
    """One set as the reader shows it: constraints body, acceptance, and per screen whether
    a compile would be refused and the state of its latest prompt."""
    out = design_svc.get_design_set(db, project_id, set_id)
    ds = db.get(DesignSet, set_id)
    screens = []
    for s in ds.screens:
        why = []
        if ds.status == "frozen":
            why.append("the set is frozen: its PRD is closed")
        screens.append({"screen": s.screen, "tier": s.tier, "tier_confirmed": s.tier_confirmed,
                        "prompt": _prompt_state(db, ds, s.screen),
                        "compile_refused": why})
    accepted = db.scalars(select(DesignAcceptance).where(
        DesignAcceptance.design_set_id == ds.id, DesignAcceptance.kind == "constraints")
        .order_by(DesignAcceptance.id)).all()
    return {
        "id": out["id"], "prd_id": out["prd_id"], "status": out["status"],
        "stale_reason": out.get("stale_reason", ""),
        "constraints_hash": ds.constraints_hash, "constraints_accepted": ds.constraints_accepted,
        "preamble_version": ds.preamble_version,
        **_body(ds.constraints_body, ds.constraints_hash),
        "screens": screens,
        "history": [{"constraints_hash": a.prompt_hash, "person": a.person,
                     "relayed_by": a.relayed_by,
                     "current": a.prompt_hash == ds.constraints_hash} for a in accepted],
    }


def _conflict_fact(detail: dict) -> dict:
    """Hashes and the file's name, length, and path. Never either body."""
    return {
        "ledger_hash": detail["ledger_hash"],
        "file_hash": detail["file_hash"],
        "file_name": detail["file_name"],
        "byte_length": detail["byte_length"],
        "path": detail["path"],
    }


def library(db: Session, project_id: str) -> dict:
    """One list for the Specs page.

    PRD rows carry the live hash, the baseline hash (null when there is none), and the
    sync state from `sync_storage.status`. A conflict row also carries the ledger hash,
    the file hash, and the path. The preamble is null when the project has none — this
    list does not 404 for that. Design-set rows are set constraints and screen prompts.
    No body text is included. `conflict_ids` is only rows whose sync state is conflict:
    a failed export or a file the box could not read is not one of them.
    """
    # Imported here: sync_storage is a reader of prds, and this module is too.
    from app.services import sync_storage

    storage = sync_storage.frozen_status(db, project_id)
    sync_by = {r["prd_id"]: r for r in storage["rows"]}
    report = storage.get("report") or {}
    details = {
        d["prd_id"]: _conflict_fact(d)
        for d in (report.get("conflict_details") or [])
    }
    prd_rows = []
    for prd in prd_svc.list_prds(db, project_id=project_id):
        base = prd_svc.baseline_of(db, prd.id)
        state = sync_by.get(prd.id, {}).get("sync_state", "unknown")
        prd_rows.append({
            "id": prd.key,
            "title": prd.title,
            "version": prd.version,
            "status": prd.status,
            "content_hash": content_hash(prd.body or ""),
            "baseline_hash": content_hash(base.body) if base is not None else None,
            "baseline_version": base.version if base is not None else None,
            "sync_state": state,
            # Only a real conflict carries file facts. A ledger-newer or unreadable row does not.
            "conflict": ({**details[prd.id], "prd_id": prd.key}
                         if state == "conflict" and prd.id in details else None),
        })
    pre = preamble_view(db, project_id)
    preamble = None if pre is None else {
        "id": pre["id"],
        "title": pre["title"] or "Preamble",
        "version": pre["version"],
        "content_hash": pre["content_hash"],
    }
    documents = []
    sets = db.scalars(
        select(DesignSet).where(DesignSet.project_id == project_id).order_by(DesignSet.created_at)
    ).all()
    for ds in sets:
        documents.append({
            "kind": "constraints",
            "set_id": ds.id,
            "prd_id": ds.prd_id,
            "screen": "",
            "title": "Set constraints",
            "status": "accepted" if ds.constraints_accepted else "not accepted",
            "hash": ds.constraints_hash,
            "set_status": ds.status,
        })
        for screen in ds.screens:
            documents.append({
                "kind": "screen",
                "set_id": ds.id,
                "prd_id": ds.prd_id,
                "screen": screen.screen,
                "title": screen.screen,
                "status": _prompt_state(db, ds, screen.screen),
                "hash": screen.prompt_hash or "",
                "set_status": ds.status,
            })
    return {
        "prds": prd_rows,
        "preamble": preamble,
        "documents": documents,
        "conflict_ids": [p["id"] for p in prd_rows if p["sync_state"] == "conflict"],
    }
