"""Document record (GRPH-1275 / GRPH-P64).

Ledger rows for the non-PRD governing documents. A PRD stays a PRD: the index reads
`baseline_of` for it and this module never writes a parallel row for the spec.

| kind            | indexed by default                | written by                |
| preamble        | no                                | a person (`put_document`) |
| set_constraints | no                                | design-set operations     |
| design_prompt   | only once accepted                | design-set operations     |
| design_review   | only once accepted                | design-set operations     |
| canvas          | never (blob only)                 | design-set operations     |

Absence is never reported as clean: an empty default search says drafts were excluded.
"""
from __future__ import annotations

import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import errors
from app.hashing import content_hash
from app.models import Document, DocumentChunk, Prd
from app.services import prds as prd_svc

KINDS = ("preamble", "set_constraints", "design_prompt", "design_review", "canvas")
# Only these may be written through `put_document`; the rest belong to the design-set
# operations, so a general ingest can never mint a governing prompt or review.
PUT_KINDS = ("preamble",)
ACCEPT_GATED = ("design_prompt", "design_review")
NEVER_INDEXED = ("canvas",)
BLOB_STATUSES = ("none", "pending", "stored")
# Bumped when chunking changes. Stored on every chunk; re-extraction is explicit.
EXTRACTOR_VERSION = "1"


def new_id() -> str:
    return f"DOC-{uuid.uuid4().hex[:10]}"


def indexed(doc: Document, *, include_design: bool = False) -> bool:
    """Whether search may return this row's chunks."""
    if doc.kind in NEVER_INDEXED or not doc.governing:
        return False
    if doc.kind in ACCEPT_GATED:
        return doc.accepted and include_design
    # preamble / set_constraints are not indexed by default; include_design opts them in.
    return include_design


def extract(body: str) -> list[tuple[str, str]]:
    """(heading, text) per `## ` section; text before the first heading has heading ''."""
    out: list[tuple[str, str]] = []
    head, buf = "", []
    for line in (body or "").splitlines():
        m = re.match(r"^##\s+(.*\S)\s*$", line)
        if m:
            if "".join(buf).strip():
                out.append((head, "\n".join(buf).strip()))
            head, buf = m.group(1), []
        else:
            buf.append(line)
    if "".join(buf).strip():
        out.append((head, "\n".join(buf).strip()))
    return out


def _chunk(doc: Document, body: str) -> None:
    for i, (heading, text) in enumerate(extract(body) or [("", body or "")]):
        doc.chunks.append(DocumentChunk(
            ordinal=i, heading=heading, text=text, content_hash=content_hash(text),
            extractor_version=EXTRACTOR_VERSION, governing=True))


def put_document(
    db: Session, *, project_id: str, kind: str, body: str, prd_id: str,
    title: str = "", created_by: str = "",
) -> dict:
    if kind not in KINDS:
        raise errors.Validation(f"unknown kind {kind!r}", hint=f"one of {', '.join(KINDS)}")
    if kind not in PUT_KINDS:
        raise errors.Validation(
            f"put_document does not write {kind!r} rows",
            hint="prompt, constraint, review and canvas rows come from the design-set operations")
    if not created_by.startswith("key:"):
        # A registered agent id is not a person. `caller_identity` stamps a bare credential
        # as `key:<name>`; anything else is an agent seat.
        raise errors.Validation(
            "preamble rows are written by a person, not an agent seat",
            hint="call put_document with a human credential")
    if not (body or "").strip():
        raise errors.Validation("body is empty", hint="a preamble needs text")
    prd = prd_svc.get_prd(db, prd_id)
    if prd is None or prd.project_id != project_id:
        raise errors.NotFound(f"prd not found in this project: {prd_id}")
    base = prd_svc.baseline_of(db, prd.id)
    if base is None:
        # A precondition on the cited PRD, not a policy refusal: a Validation, so it stays
        # out of the bare-409 ratchet (scripts/refusal_ratchet.py).
        raise errors.Validation(
            f"{prd.id} has no baseline", hint="the preamble cites the baseline body hash; "
            "approve the visual-system PRD first")
    digest = content_hash(body)
    rows = list(db.scalars(select(Document).where(
        Document.project_id == project_id, Document.kind == kind,
        Document.set_id == "", Document.screen == "").order_by(Document.version)))
    current = next((r for r in reversed(rows) if r.governing), None)
    if current and current.content_hash == digest and current.prd_hash == content_hash(base.body):
        return to_dict(current)  # same text, same citation: not a new version
    for r in rows:
        r.governing = False
        for c in r.chunks:
            c.governing = False
    doc = Document(
        id=new_id(), project_id=project_id, kind=kind, title=title or "Preamble",
        prd_id=prd.id, prd_hash=content_hash(base.body), version=(rows[-1].version + 1) if rows else 1,
        status="active", content_hash=digest, governing=True, created_by=created_by)
    _chunk(doc, body)
    db.add(doc)
    db.commit()
    return to_dict(doc)


def to_dict(doc: Document) -> dict:
    out = {
        "id": doc.id, "project_id": doc.project_id, "kind": doc.kind, "title": doc.title,
        "prd_id": doc.prd_id, "prd_hash": doc.prd_hash, "set_id": doc.set_id,
        "screen": doc.screen, "version": doc.version, "status": doc.status,
        "content_hash": doc.content_hash, "governing": doc.governing,
        "accepted": doc.accepted, "blob_status": doc.blob_status,
        "chunks": [{"heading": c.heading, "text": c.text, "content_hash": c.content_hash,
                    "extractor_version": c.extractor_version}
                   for c in doc.chunks if c.governing],
    }
    if doc.stale_reason:
        out["stale_reason"] = doc.stale_reason
    # The byte key is omitted when absent — never `null` or `""`, which would read as a key.
    if doc.blob_key:
        out["blob_key"] = doc.blob_key
    return out


def get_document(db: Session, project_id: str, doc_id: str) -> dict:
    doc = db.get(Document, doc_id)
    if doc is None or doc.project_id != project_id:
        raise errors.NotFound(f"document not found: {doc_id}")
    return to_dict(doc)


def search_docs(
    db: Session, project_id: str, query: str, *, include_drafts: bool = False,
    include_design: bool = False, limit: int = 10,
) -> dict:
    """Lexical search over baselined PRD bodies (+ indexed document chunks).

    A PRD with a baseline is read from `baseline_of` — a pending rebaseline leaves the
    previous baseline indexed. A PRD with none is absent unless `include_drafts`.
    """
    terms = [t for t in re.findall(r"\w+", (query or "").lower()) if t]
    if not terms:
        raise errors.Validation("query is empty")

    def score(text: str) -> int:
        low = text.lower()
        return sum(low.count(t) for t in terms)

    hits: list[dict] = []
    unbaselined = 0
    for prd in db.scalars(select(Prd).where(Prd.project_id == project_id)):
        base = prd_svc.baseline_of(db, prd.id)
        if base is None:
            unbaselined += 1
            if not include_drafts:
                continue
            body, ver, status = prd.body, prd.version, "draft"
        else:
            body, ver, status = base.body, base.version, "baseline"
        for heading, text in extract(body):
            s = score(f"{heading}\n{text}")
            if s:
                hits.append({"source": "prd", "prd_id": prd.id, "version": ver,
                             "status": status, "heading": heading, "text": text,
                             "content_hash": content_hash(body), "score": s})
    for doc in db.scalars(select(Document).where(Document.project_id == project_id)):
        if doc.status == "stale" and not include_design:
            continue
        if not indexed(doc, include_design=include_design):
            continue
        for c in doc.chunks:
            s = score(f"{c.heading}\n{c.text}") if c.governing else 0
            if s:
                hit = {"source": "document", "document_id": doc.id, "kind": doc.kind,
                       "status": doc.status, "heading": c.heading, "text": c.text,
                       "content_hash": c.content_hash, "score": s}
                if doc.stale_reason:
                    hit["stale_reason"] = doc.stale_reason
                hits.append(hit)
    hits.sort(key=lambda h: -h["score"])
    out = {"results": hits[:limit], "ranking": "lexical",
           "include_drafts": include_drafts, "include_design": include_design}
    if not hits:
        # Empty is not "no draft exists".
        out["note"] = ("no match; draft PRDs were excluded" if not include_drafts
                       else "no match") + (f" ({unbaselined} PRD(s) have no baseline)"
                                           if unbaselined and not include_drafts else "")
    return out
