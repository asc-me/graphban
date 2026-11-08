"""Spec alignment (GRPH-1368 / GRPH-P64).

Only a PRD keeps the thorough gates (grill, baseline, close). A preamble, set constraints,
design prompt, design review, canvas and a design set are not accepted by anyone: each one
aligns to the PRD it links, and `assess` is the only check.

    {"linked_prd": "<rendered key or null>", "prd_passed": false, "gaps": ["<heading>"],
     "functionally_complete": false, "approved_to_build": false}

The requirements are the linked PRD baseline's `## ` headings. A gap is a heading whose text
does not appear in the spec body, case and whitespace folded. No linked PRD, or a linked PRD
with no baseline, is never approved to build: an absence is not a clean result.

This module imports no other service that reads documents, so `documents` and `design_sets`
can both call it.
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DesignCorrection, DesignSet, Document
from app.services import prds as prd_svc

_HEADING = re.compile(r"^##\s+(.*\S)\s*$", re.MULTILINE)


def _fold(text: str) -> str:
    return " ".join((text or "").lower().split())


def requirements(body: str) -> list[str]:
    """The `## ` headings of a baseline body, in order, without repeats."""
    seen: list[str] = []
    for m in _HEADING.finditer(body or ""):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def assess(db: Session, prd_id: str | None, bodies: list[str]) -> dict:
    """Align spec text to the PRD it links. `bodies` is every text that counts as the spec."""
    prd = prd_svc.get_prd(db, prd_id) if prd_id else None
    base = prd_svc.baseline_of(db, prd.id) if prd is not None else None
    gaps: list[str] = []
    if base is not None:
        hay = _fold("\n".join(b or "" for b in bodies))
        gaps = [h for h in requirements(base.body) if _fold(h) not in hay]
    complete = prd is not None and not gaps
    passed = base is not None
    return {"linked_prd": prd.key if prd is not None else None, "prd_passed": passed,
            "gaps": gaps, "functionally_complete": complete,
            "approved_to_build": complete and passed}


def _doc_text(doc: Document) -> str:
    # A chunk keeps its heading apart from its text; the heading is part of the spec body.
    return "\n\n".join(f"## {c.heading}\n{c.text}" if c.heading else c.text
                       for c in sorted(doc.chunks, key=lambda c: c.ordinal) if c.governing)


def for_design_set(db: Session, ds: DesignSet) -> dict:
    """Constraints body, screen briefs, and what each compiled prompt adds of its own: the
    standing preamble and the screen's open corrections.

    A compiled prompt also quotes the whole governing PRD under `## Governing PRD`. That quote
    is never matched: it carries every heading, so counting it would approve any set the moment
    it compiled (bounce of 865c6cb2). The rest of a prompt is exactly these inputs, so they are
    read at their source rather than cut back out of the chunked prompt."""
    bodies = [ds.constraints_body or ""] + [s.brief or "" for s in ds.screens]
    compiled = {d.screen for d in db.scalars(select(Document).where(
        Document.set_id == ds.id, Document.kind == "design_prompt"))}
    if compiled:
        pre = db.scalars(select(Document).where(
            Document.project_id == ds.project_id, Document.kind == "preamble",
            Document.set_id == "", Document.governing.is_(True))).first()
        if pre is not None:
            bodies.append(_doc_text(pre))
        bodies += [c.text or "" for c in db.scalars(select(DesignCorrection).where(
            DesignCorrection.design_set_id == ds.id, DesignCorrection.status == "open",
            DesignCorrection.screen.in_(compiled)))]
    return assess(db, ds.prd_id, bodies)


def for_document(db: Session, doc: Document) -> dict:
    """A document in a design set (prompt, review, constraints, canvas) is approved only when
    its set is; a standalone one (the preamble) is checked on its own text."""
    if doc.set_id:
        ds = db.get(DesignSet, doc.set_id)
        if ds is not None:
            return for_design_set(db, ds)
    return assess(db, doc.prd_id, [_doc_text(doc)])


def approved(db: Session, doc: Document) -> bool:
    return for_document(db, doc)["approved_to_build"]
