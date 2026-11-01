"""Document record (GRPH-1275 / GRPH-P64).

Ledger rows for the non-PRD governing documents. A PRD stays a PRD: the index reads
`baseline_of` for it and this module never writes a parallel row for the spec.

| kind            | indexed by default                | written by                |
| preamble        | never (stored, not embedded)      | a person (`put_document`) |
| set_constraints | never (stored, not embedded)      | design-set operations     |
| design_prompt   | only once accepted                | design-set operations     |
| design_review   | only once accepted                | design-set operations     |
| canvas          | never (blob only)                 | design-set operations     |

Absence is never reported as clean: an empty default search says drafts were excluded.
"""
from __future__ import annotations

import hashlib
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
# Stored with chunks for `get_document`, but outside the searchable corpus and never embedded.
NEVER_INDEXED = ("canvas", "preamble", "set_constraints")
BLOB_STATUSES = ("none", "pending", "stored")
# Bumped when chunking changes. Stored on every chunk; re-extraction is explicit.
EXTRACTOR_VERSION = "1"


def new_id() -> str:
    return f"DOC-{uuid.uuid4().hex[:10]}"


def indexed(doc: Document, *, include_design: bool = False) -> bool:
    """Whether search may return this row's chunks."""
    if doc.kind in NEVER_INDEXED or not doc.governing:
        return False
    return doc.kind in ACCEPT_GATED and doc.accepted and include_design


def _split_long(text: str, limit: int) -> list[str]:
    """Pieces of `text` no longer than `limit`, cut on paragraphs (a lone paragraph over the
    limit is cut hard — the embedder would clamp it to its head anyway)."""
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    cur = ""
    for para in re.split(r"\n\s*\n", text):
        while len(para) > limit:
            if cur:
                out.append(cur)
                cur = ""
            out.append(para[:limit])
            para = para[limit:]
        if cur and len(cur) + 2 + len(para) > limit:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n\n{para}" if cur else para
    if cur.strip():
        out.append(cur)
    return [p for p in out if p.strip()]


def extract(body: str) -> list[tuple[str, str]]:
    """(heading, text) per `## ` section; text before the first heading has heading ''.

    A section longer than the embed input yields several pieces that all cite its heading."""
    from app.config import settings

    sections: list[tuple[str, str]] = []
    head, buf = "", []
    for line in (body or "").splitlines():
        m = re.match(r"^##\s+(.*\S)\s*$", line)
        if m:
            if "".join(buf).strip():
                sections.append((head, "\n".join(buf).strip()))
            head, buf = m.group(1), []
        else:
            buf.append(line)
    if "".join(buf).strip():
        sections.append((head, "\n".join(buf).strip()))
    limit = max(1, int(settings.embed_max_chars))
    return [(h, piece) for h, t in sections for piece in _split_long(t, limit)]


def _vector(text: str) -> list[float] | None:
    """A chunk's vector, or None. The stub embedder is not a meaning (`embed_ok` false), and a
    down provider must not fail the write: the chunk is stored and matched lexically."""
    from app.config import settings
    from app.embeddings import safe_embed

    if settings.embed_provider == "stub":
        return None
    return safe_embed(text)


def _embeddable(chunk: DocumentChunk, doc: Document | None) -> bool:
    """Only governing text carries a vector: a PRD baseline, or an accepted prompt / review."""
    if doc is None:
        return chunk.kind == "prd"
    return doc.kind in ACCEPT_GATED and bool(doc.accepted)


def _chunk(doc: Document, body: str) -> None:
    """Store the row's extracted text. No vector here: a write is not an acceptance, and
    `embed_document` runs when a prompt or review becomes governing."""
    for i, (heading, text) in enumerate(extract(body) or [("", body or "")]):
        doc.chunks.append(DocumentChunk(
            project_id=doc.project_id, kind=doc.kind, ordinal=i, heading=heading, text=text,
            version=str(doc.version or ""), content_hash=content_hash(text),
            extractor_version=EXTRACTOR_VERSION, governing=True, embedding=None))


def embed_document(doc: Document) -> int:
    """Embed an accepted design_prompt / design_review's chunks — the acceptance writer calls
    this. Anything else is left without a vector. Returns the number of chunks embedded; the
    caller commits."""
    if doc.kind not in ACCEPT_GATED or not doc.accepted:
        return 0
    for c in doc.chunks:
        c.embedding = _vector(c.text)
    return len(doc.chunks)


def index_prd_baseline(db: Session, prd_id: str) -> int:
    """(Re)index the PRD's governing baseline as chunks. Runs when a baseline is frozen — not
    on a draft save. Idempotent for an unchanged baseline; an older baseline's chunks stay as
    `superseded`. Returns the number of chunks written."""
    prd = prd_svc.get_prd(db, prd_id)
    base = prd_svc.baseline_of(db, prd_id) if prd is not None else None
    if base is None:
        return 0
    digest = content_hash(base.body)
    live = list(db.scalars(select(DocumentChunk).where(
        DocumentChunk.kind == "prd", DocumentChunk.prd_id == prd_id,
        DocumentChunk.governing.is_(True))))
    if live and live[0].content_hash == digest:
        return 0
    for c in live:
        c.governing = False
    pieces = extract(base.body) or [("", base.body or "")]
    for i, (heading, text) in enumerate(pieces):
        db.add(DocumentChunk(
            project_id=prd.project_id, kind="prd", prd_id=prd_id, ordinal=i, heading=heading,
            text=text, version=base.version, content_hash=digest,
            extractor_version=EXTRACTOR_VERSION, governing=True, embedding=_vector(text)))
    db.commit()
    return len(pieces)


def drop_prd_chunks(db: Session, prd_id: str) -> None:
    """The baseline was removed (approval invalidated): its chunks no longer govern anything."""
    for c in db.scalars(select(DocumentChunk).where(
            DocumentChunk.kind == "prd", DocumentChunk.prd_id == prd_id)):
        db.delete(c)
    db.commit()


def index_missing_baselines(db: Session) -> int:
    """Index every PRD whose baseline was frozen before the chunk index existed."""
    n = 0
    for prd in db.scalars(select(Prd)):
        if prd_svc.baseline_of(db, prd.id) is not None:
            n += index_prd_baseline(db, prd.id)
    return n


def backfill_embeddings(db: Session) -> int:
    """Re-embed governing chunks from their stored text (after an EMBED_DIM or provider
    change), after indexing any baseline the index has not seen. A chunk outside the embedded
    corpus (preamble, set constraints, unaccepted prompt) is cleared, not embedded."""
    index_missing_baselines(db)
    n = 0
    for c, doc in db.execute(select(DocumentChunk, Document).outerjoin(
            Document, DocumentChunk.document_id == Document.id)).all():
        if _embeddable(c, doc):
            c.embedding = _vector(c.text)
            n += 1
        else:
            c.embedding = None
    db.commit()
    return n


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
    # No byte key, pending or stored: the key is omitted, never `null` or `""`. Bytes come
    # only from `read_blob`; `blob_status` is the field a client branches on.
    if doc.blob_status != "none":
        out["blob_mime"], out["blob_size"] = doc.blob_mime, doc.blob_size
    return out


def default_backend():
    from app.config import settings
    from app.services.drive_sync import LocalFolderBackend

    return LocalFolderBackend(settings.sync_dir)


def _store(db: Session, doc: Document, data: bytes, backend) -> None:
    try:
        backend.put_bytes(doc.content_hash, data)
    except Exception as e:  # noqa: BLE001 — any backend failure leaves the row pending
        name = getattr(backend, "name", type(backend).__name__)
        raise errors.Unavailable(
            f"blob backend {name} failed: {e}",
            hint=f"the row is kept with blob_status pending; retry the put to store the same "
                 f"hash {doc.content_hash}") from e
    doc.blob_status, doc.blob_key = "stored", doc.content_hash
    db.commit()


def put_blob(
    db: Session, *, project_id: str, kind: str, data: bytes, mime: str, prd_id: str | None = None,
    set_id: str = "", screen: str = "", title: str = "", created_by: str = "", backend=None,
) -> dict:
    """Content-addressed put. The ledger row is written (and committed) before the backend
    call, so a backend failure leaves a `pending` row and a retry stores the same hash."""
    if kind not in KINDS:
        raise errors.Validation(f"unknown kind {kind!r}", hint=f"one of {', '.join(KINDS)}")
    backend = backend or default_backend()
    digest = hashlib.sha256(data).hexdigest()
    rows = list(db.scalars(select(Document).where(
        Document.project_id == project_id, Document.kind == kind,
        Document.set_id == set_id, Document.screen == screen).order_by(Document.version)))
    current = next((r for r in reversed(rows) if r.governing), None)
    if current and current.content_hash == digest and current.blob_status != "none":
        doc = current  # same bytes: not a new version; a pending one is retried
    else:
        for r in rows:
            r.governing = False
            for c in r.chunks:
                c.governing = False
        doc = Document(
            id=new_id(), project_id=project_id, kind=kind, title=title or kind, prd_id=prd_id,
            set_id=set_id, screen=screen, version=(rows[-1].version + 1) if rows else 1,
            status="active", content_hash=digest, governing=True, created_by=created_by,
            blob_status="pending", blob_mime=mime, blob_size=len(data))
        # A canvas has no extraction, so there are no chunks to write before the backend call.
        db.add(doc)
        db.commit()
    if doc.blob_status != "stored":
        _store(db, doc, data, backend)
    return to_dict(doc)


def read_blob(db: Session, project_id: str, doc_id: str, backend=None) -> tuple[bytes, str]:
    doc = db.get(Document, doc_id)
    if doc is None or doc.project_id != project_id:
        raise errors.NotFound(f"document not found: {doc_id}")
    if doc.blob_status == "pending":
        raise errors.BlobPending(
            f"bytes for {doc_id} are not stored yet",
            hint=f"blob {doc.content_hash} is pending; a retry of the put stores that same hash")
    if doc.blob_status != "stored":
        raise errors.NotFound(f"document has no bytes: {doc_id}")
    data = (backend or default_backend()).get_bytes(doc.blob_key)
    if data is None:
        raise errors.NotFound(f"blob {doc.blob_key} missing from the backend")
    return data, doc.blob_mime


def get_document(db: Session, project_id: str, doc_id: str) -> dict:
    doc = db.get(Document, doc_id)
    if doc is None or doc.project_id != project_id:
        raise errors.NotFound(f"document not found: {doc_id}")
    return to_dict(doc)


def _doc_status(doc: Document, chunk: DocumentChunk) -> str:
    if not chunk.governing:
        return "superseded"
    if doc.status == "stale":
        return "stale"
    return "accepted" if (doc.accepted or doc.kind not in ACCEPT_GATED) else "draft"


def search_docs(
    db: Session, project_id: str, query: str, *, top_k: int = 10, include_drafts: bool = False,
    include_design: bool = False,
) -> dict:
    """Search the governing text: baselined PRD chunks, plus design chunks on request.

    Ranked by vector when the query and a chunk both have one; a chunk stored without a vector
    is matched by substring and the response says so. A hit is not the spec: open the section
    with `get_prd` / `get_document`.
    """
    from app.embeddings import cosine_similarity

    terms = [t for t in re.findall(r"\w+", (query or "").lower()) if t]
    if not terms:
        raise errors.Validation("query is empty")

    def lexical(text: str) -> int:
        low = text.lower()
        return sum(low.count(t) for t in terms)

    unbaselined = 0
    cands: list[tuple[dict, list[float] | None]] = []
    indexed_prds = set(db.scalars(select(DocumentChunk.prd_id).where(
        DocumentChunk.project_id == project_id, DocumentChunk.kind == "prd",
        DocumentChunk.governing.is_(True))))
    for prd in db.scalars(select(Prd).where(Prd.project_id == project_id)):
        base = prd_svc.baseline_of(db, prd.id)
        if base is None:
            unbaselined += 1
            if include_drafts:
                for heading, text in extract(prd.body):
                    cands.append(({"source_id": prd.id, "prd_id": prd.id, "kind": "prd",
                                   "version": prd.version, "status": "draft", "heading": heading,
                                   "text": text, "content_hash": content_hash(prd.body)}, None))
            continue
        if prd.id not in indexed_prds:
            # A baseline frozen before the index existed. Search is a read: match its body
            # lexically here and leave the write (and the embedding) to `/memory/backfill`.
            digest = content_hash(base.body)
            for heading, text in extract(base.body) or [("", base.body or "")]:
                cands.append(({"source_id": prd.id, "prd_id": prd.id, "kind": "prd",
                               "version": base.version, "status": "baseline",
                               "heading": heading, "text": text, "content_hash": digest}, None))
    rows = db.execute(
        select(DocumentChunk, Document)
        .outerjoin(Document, DocumentChunk.document_id == Document.id)
        .where(DocumentChunk.project_id == project_id)).all()
    for c, doc in rows:
        if doc is None:  # PRD baseline chunk
            status = "baseline" if c.governing else "superseded"
            if status == "superseded" and not include_drafts:
                continue
            hit = {"source_id": c.prd_id, "prd_id": c.prd_id, "kind": "prd", "status": status}
        else:
            if doc.kind in NEVER_INDEXED or (not doc.governing and not include_drafts):
                continue
            status = _doc_status(doc, c)
            if status in ("draft", "superseded"):
                if not include_drafts:
                    continue
            elif not include_design:
                continue
            hit = {"source_id": doc.id, "document_id": doc.id, "kind": doc.kind, "status": status}
            if status == "stale" and doc.stale_reason in ("preamble", "prd_baseline"):
                hit["stale_reason"] = doc.stale_reason
        hit.update(version=c.version, heading=c.heading, text=c.text, content_hash=c.content_hash)
        cands.append((hit, c.embedding))

    from app.config import settings
    qvec = None
    if settings.embed_provider != "stub" and any(v is not None for _, v in cands):
        from app.embeddings import safe_embed
        qvec = safe_embed(query)
    semantic: list[dict] = []
    lexical_hits: list[dict] = []
    for hit, vec in cands:
        if qvec is not None and vec is not None:
            semantic.append({**hit, "score": round(cosine_similarity(qvec, vec), 6)})
        elif (s := lexical(f"{hit['heading']}\n{hit['text']}")):
            lexical_hits.append({**hit, "score": s})
    semantic.sort(key=lambda h: -h["score"])
    lexical_hits.sort(key=lambda h: -h["score"])
    hits = (semantic + lexical_hits)[:max(1, int(top_k))]
    ranking = ("lexical" if not semantic else "semantic" if not lexical_hits else "mixed")
    out = {"results": hits, "ranking": ranking,
           "include_drafts": include_drafts, "include_design": include_design}
    if ranking != "semantic":
        out["ranking_note"] = ("ranking is lexical (substring match over stored text)"
                               if ranking == "lexical" else
                               "some chunks have no vector; those were matched by substring "
                               "and listed after the vector-ranked ones")
    if not hits:
        # Empty is not "no draft exists".
        out["note"] = ("no match; draft PRDs were excluded" if not include_drafts
                       else "no match") + (f" ({unbaselined} PRD(s) have no baseline)"
                                           if unbaselined and not include_drafts else "")
    return out
