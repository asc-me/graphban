"""GRPH-1368 — specs and designs align to a linked PRD instead of being accepted."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.mcp_server import TOOLS
from app.models import DesignCorrection, DesignSet, Document, Project
from app.services import design_sets as ds_svc
from app.services import documents as docs
from app.services import prds as prd_svc
from app.services import spec_alignment as sa
from tests.prd_approve import approve

BODY = "## Tokens\nUse the teal accent everywhere.\n\n## Spacing\nEight point grid.\n"
KEY = "key:alex"


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def pid(db):
    return db.scalars(select(Project.id)).first()


def _set(db, pid, prd, constraints="Teal only."):
    sid = ds_svc.open_design(db, pid, prd.id)["id"]
    ds_svc.put_set_constraints(db, pid, sid, constraints, screens=[{"screen": "home"}])
    return sid


def test_no_linked_prd_is_never_approved(db):
    out = sa.assess(db, None, ["## Tokens\n## Spacing"])
    assert out == {"linked_prd": None, "prd_passed": False, "gaps": [],
                   "functionally_complete": False, "approved_to_build": False}


def test_a_preamble_with_no_prd_is_stored_and_not_approved(db, pid):
    out = docs.put_document(db, project_id=pid, kind="preamble", body="Be calm.", prd_id=None,
                            created_by=KEY)
    assert out["prd_id"] is None
    got = docs.get_document(db, pid, out["id"])["alignment"]
    assert got["linked_prd"] is None and got["approved_to_build"] is False


def test_a_prd_with_no_baseline_is_not_approved(db, pid):
    prd = prd_svc.create_prd(db, title="Draft", project_id=pid, body=BODY)
    out = sa.assess(db, prd.id, [BODY])
    assert out["linked_prd"] == prd.key and out["prd_passed"] is False
    assert out["gaps"] == [] and out["functionally_complete"] is True
    assert out["approved_to_build"] is False


def test_one_missing_heading_is_a_gap(db, pid):
    prd = prd_svc.create_prd(db, title="P", project_id=pid, body=BODY)
    approve(db, prd)
    out = sa.assess(db, prd.id, ["## tokens\n\nonly this"])
    assert out["gaps"] == ["Spacing"]
    assert out["functionally_complete"] is False and out["approved_to_build"] is False
    assert out["prd_passed"] is True


def test_a_full_match_is_approved_to_build_case_and_space_folded(db, pid):
    prd = prd_svc.create_prd(db, title="P", project_id=pid, body=BODY)
    approve(db, prd)
    out = sa.assess(db, prd.id, ["TOKENS here", "the   SPACING\nrules"])
    assert out["gaps"] == [] and out["approved_to_build"] is True


def test_design_set_aligns_on_constraints_and_compiles_with_no_prior_accept(db, pid):
    prd = prd_svc.create_prd(db, title="P", project_id=pid, body=BODY)
    approve(db, prd)
    sid = _set(db, pid, prd, constraints="Tokens: teal.")
    ds = db.get(DesignSet, sid)
    assert sa.for_design_set(db, ds)["gaps"] == ["Spacing"]
    assert ds_svc.get_design_set(db, pid, sid)["alignment"]["approved_to_build"] is False
    # nothing accepted, no tier confirmed: compile still runs
    out = ds_svc.compile_prompts(db, pid, sid)
    assert out["prompts"][0]["new"] is True
    # the compiled prompt quotes the whole PRD, but that quote is not the spec: still a gap
    got = ds_svc.get_design_set(db, pid, sid)["alignment"]
    assert got["gaps"] == ["Spacing"] and got["approved_to_build"] is False
    ds_svc.put_set_constraints(db, pid, sid, "Tokens: teal. Spacing: 8pt.")
    ds_svc.compile_prompts(db, pid, sid)
    got = ds_svc.get_design_set(db, pid, sid)["alignment"]
    assert got["gaps"] == [] and got["approved_to_build"] is True


def test_a_brief_approves_gated_sections_without_pasted_headings(db, pid):
    """The design states the behavior in the brief. The heading title is not a second copy
    of that behavior, and an ungated heading is not this set's requirement."""
    prd = prd_svc.create_prd(db, title="P", project_id=pid, body=BODY)
    approve(db, prd)
    sid = ds_svc.open_design(db, pid, prd.id)["id"]
    ds_svc.put_set_constraints(
        db, pid, sid, "Unrelated nonsense.", sections=["Tokens"],
        screens=[{"screen": "home", "brief": "Name the organization. Finish does not wait."}])
    got = ds_svc.get_design_set(db, pid, sid)["alignment"]
    assert got["prd_passed"] is True
    assert got["gaps"] == [] and got["approved_to_build"] is True


def test_a_brief_does_not_approve_a_name_decompose_will_not_attach(db, pid):
    """`Overview` is not the heading `1. Overview`. A brief must not clear it."""
    body = "## 1. Overview\nThe product.\n\n## Spacing\nEight point grid.\n"
    prd = prd_svc.create_prd(db, title="P", project_id=pid, body=body)
    approve(db, prd)
    sid = ds_svc.open_design(db, pid, prd.id)["id"]
    ds_svc.put_set_constraints(
        db, pid, sid, "Unrelated nonsense.", sections=["Overview"],
        screens=[{"screen": "home", "brief": "Name the organization."}])
    got = ds_svc.get_design_set(db, pid, sid)["alignment"]
    assert got["gaps"] == ["Overview"] and got["approved_to_build"] is False
    assert ds_svc.gating_set(db, prd.id, "1. Overview") is None


def test_a_padded_section_is_stored_as_the_heading_decompose_matches(db, pid):
    prd = prd_svc.create_prd(db, title="P", project_id=pid, body=BODY)
    approve(db, prd)
    sid = ds_svc.open_design(db, pid, prd.id)["id"]
    ds_svc.put_set_constraints(
        db, pid, sid, "Unrelated nonsense.", sections=[" Tokens "],
        screens=[{"screen": "home", "brief": "Name the organization."}])
    assert db.get(DesignSet, sid).sections == ["Tokens"]
    assert ds_svc.gating_set(db, prd.id, "Tokens") is not None
    got = ds_svc.get_design_set(db, pid, sid)["alignment"]
    assert got["gaps"] == [] and got["approved_to_build"] is True


def test_a_shorter_heading_is_not_satisfied_by_a_longer_one(db, pid):
    body = "## 2. API\nShort.\n\n## 12. API\nLong.\n"
    prd = prd_svc.create_prd(db, title="P", project_id=pid, body=body)
    approve(db, prd)
    sid = ds_svc.open_design(db, pid, prd.id)["id"]
    ds_svc.put_set_constraints(
        db, pid, sid, "See 12. API for the long one.", sections=["2. API"],
        screens=[{"screen": "home"}])
    got = ds_svc.get_design_set(db, pid, sid)["alignment"]
    assert got["gaps"] == ["2. API"] and got["approved_to_build"] is False
    ds_svc.put_set_constraints(
        db, pid, sid, "2. API is the short one.", sections=["2. API"],
        screens=[{"screen": "home"}])
    got = ds_svc.get_design_set(db, pid, sid)["alignment"]
    assert got["gaps"] == [] and got["approved_to_build"] is True


def test_punctuation_does_not_make_a_shorter_heading_present(db, pid):
    body = ("## Goals\nYes.\n\n## Non-goals\nNo.\n\n## Login\nForm.\n\n"
            "## 1.2\nPoint.\n")
    prd = prd_svc.create_prd(db, title="P", project_id=pid, body=body)
    approve(db, prd)
    sid = ds_svc.open_design(db, pid, prd.id)["id"]
    ds_svc.put_set_constraints(
        db, pid, sid,
        "Non-goals: we will not add themes. The pre-login hint stays visible. See 1.2.3.",
        sections=["Goals", "Login", "1.2"],
        screens=[{"screen": "home"}])
    got = ds_svc.get_design_set(db, pid, sid)["alignment"]
    assert got["gaps"] == ["Goals", "Login", "1.2"] and got["approved_to_build"] is False
    ds_svc.put_set_constraints(
        db, pid, sid, "Goals. Login is the form. 1.2 is the point.",
        sections=["Goals", "Login", "1.2"],
        screens=[{"screen": "home"}])
    got = ds_svc.get_design_set(db, pid, sid)["alignment"]
    assert got["gaps"] == [] and got["approved_to_build"] is True


def test_a_dot_slash_or_dash_does_not_join_a_shorter_heading(db, pid):
    en, em = "\u2013", "\u2014"
    body = ("## 2. API\nShort.\n\n## 3. Data\nRows.\n\n## Login\nForm.\n\n"
            "## Goals\nYes.\n\n## Tokens\nTeal.\n")
    prd = prd_svc.create_prd(db, title="P", project_id=pid, body=body)
    approve(db, prd)
    sid = ds_svc.open_design(db, pid, prd.id)["id"]
    ds_svc.put_set_constraints(
        db, pid, sid,
        f"See 1.2. API for the long one. See 1.3. Data. "
        f"pre{en}login and pre{em}login. Non{en}goals. Goals/extra.",
        sections=["2. API", "3. Data", "Login", "Goals"],
        screens=[{"screen": "home"}])
    got = ds_svc.get_design_set(db, pid, sid)["alignment"]
    assert got["gaps"] == ["2. API", "3. Data", "Login", "Goals"]
    assert got["approved_to_build"] is False
    ds_svc.put_set_constraints(
        db, pid, sid,
        "2. API. 3. Data is rows. Login is the form. Goals. Tokens: teal.",
        sections=["2. API", "3. Data", "Login", "Goals", "Tokens"],
        screens=[{"screen": "home"}])
    got = ds_svc.get_design_set(db, pid, sid)["alignment"]
    assert got["gaps"] == [] and got["approved_to_build"] is True


def test_a_sentence_period_counts_unless_it_begins_a_longer_heading(db, pid):
    body = "## 1.2\nPoint.\n\n## 1.2. Overview\nLonger.\n"
    prd = prd_svc.create_prd(db, title="P", project_id=pid, body=body)
    approve(db, prd)
    sid = ds_svc.open_design(db, pid, prd.id)["id"]
    ds_svc.put_set_constraints(
        db, pid, sid, "1.2. Overview is longer.", sections=["1.2"],
        screens=[{"screen": "home"}])
    got = ds_svc.get_design_set(db, pid, sid)["alignment"]
    assert got["gaps"] == ["1.2"] and got["approved_to_build"] is False
    ds_svc.put_set_constraints(
        db, pid, sid, "1.2. The short one.", sections=["1.2"],
        screens=[{"screen": "home"}])
    got = ds_svc.get_design_set(db, pid, sid)["alignment"]
    assert got["gaps"] == [] and got["approved_to_build"] is True


def test_a_fenced_heading_is_not_a_section_a_brief_can_clear(db, pid):
    body = "## Tokens\nUse teal.\n\n```\n## Overview\nnot a heading\n```\n\n## Spacing\nEight.\n"
    prd = prd_svc.create_prd(db, title="P", project_id=pid, body=body)
    approve(db, prd)
    sid = ds_svc.open_design(db, pid, prd.id)["id"]
    ds_svc.put_set_constraints(
        db, pid, sid, "Unrelated nonsense.", sections=["Overview"],
        screens=[{"screen": "home", "brief": "Name the organization."}])
    got = ds_svc.get_design_set(db, pid, sid)["alignment"]
    assert "Overview" not in prd_svc.parse_sections(body)
    assert got["gaps"] == ["Overview"] and got["approved_to_build"] is False


def test_a_blank_heading_line_does_not_hide_the_next_title(db, pid):
    body = "##\n\n## Tokens\nUse the teal accent.\n\n## Spacing\nEight point grid.\n"
    prd = prd_svc.create_prd(db, title="P", project_id=pid, body=body)
    approve(db, prd)
    sid = ds_svc.open_design(db, pid, prd.id)["id"]
    ds_svc.put_set_constraints(
        db, pid, sid, "Unrelated nonsense.", sections=["Tokens"],
        screens=[{"screen": "home", "brief": "Name the organization."}])
    assert ds_svc.gating_set(db, prd.id, "Tokens") is not None
    got = ds_svc.get_design_set(db, pid, sid)["alignment"]
    assert got["gaps"] == [] and got["approved_to_build"] is True


def test_a_gated_set_without_a_brief_still_needs_its_heading(db, pid):
    """No brief, and the set's own text never says the section it gates. Compiling quotes
    the PRD, and that quote still does not approve it."""
    prd = prd_svc.create_prd(db, title="P", project_id=pid, body=BODY)
    approve(db, prd)
    sid = ds_svc.open_design(db, pid, prd.id)["id"]
    ds_svc.put_set_constraints(
        db, pid, sid, "Unrelated nonsense.", sections=["Tokens"],
        screens=[{"screen": "home", "brief": "   "}])
    ds_svc.compile_prompts(db, pid, sid)
    got = ds_svc.get_design_set(db, pid, sid)["alignment"]
    assert got["gaps"] == ["Tokens"] and got["approved_to_build"] is False


def test_an_unrelated_set_stays_unapproved_after_compile(db, pid):
    prd = prd_svc.create_prd(db, title="P", project_id=pid, body=BODY)
    approve(db, prd)
    sid = _set(db, pid, prd, constraints="Unrelated nonsense.")
    ds_svc.compile_prompts(db, pid, sid)
    got = ds_svc.get_design_set(db, pid, sid)["alignment"]
    assert got["gaps"] == ["Tokens", "Spacing"] and got["approved_to_build"] is False
    hits = docs.search_docs(db, pid, "teal accent", include_design=True)["results"]
    assert not [h for h in hits if h["kind"] == "design_prompt"]


def test_a_correction_counts_once_compiled(db, pid):
    prd = prd_svc.create_prd(db, title="P", project_id=pid, body=BODY)
    approve(db, prd)
    sid = _set(db, pid, prd, constraints="Tokens: teal.")
    db.add(DesignCorrection(design_set_id=sid, screen="home", text="Spacing on an 8pt grid",
                            text_hash="h1"))
    db.commit()
    ds = db.get(DesignSet, sid)
    assert sa.for_design_set(db, ds)["gaps"] == ["Spacing"]  # not part of any prompt yet
    ds_svc.compile_prompts(db, pid, sid)
    assert sa.for_design_set(db, db.get(DesignSet, sid))["approved_to_build"] is True


def test_an_approved_compiled_prompt_is_searchable_and_embedded(db, pid, monkeypatch):
    from app.config import settings
    import app.embeddings as emb

    monkeypatch.setattr(settings, "embed_provider", "openai")
    monkeypatch.setattr(emb, "safe_embed", lambda t: [0.1] * int(settings.embed_dim))
    prd = prd_svc.create_prd(db, title="P", project_id=pid, body=BODY)
    approve(db, prd)
    sid = _set(db, pid, prd, constraints="Tokens: okapi teal. Spacing: 8pt.")
    out = ds_svc.compile_prompts(db, pid, sid)
    doc = db.get(Document, out["prompts"][0]["document_id"])
    assert doc.governing and all(c.governing for c in doc.chunks)
    assert docs.indexed(db, doc, include_design=True)
    assert all(c.embedding is not None for c in doc.chunks)
    hits = docs.search_docs(db, pid, "okapi", include_design=True)["results"]
    assert [(h["kind"], h["status"]) for h in hits if h["kind"] == "design_prompt"] \
        and all(h["status"] == "approved" for h in hits if h["kind"] == "design_prompt")
    # constraints that drop a heading take the set (and its prompt's vectors) back out
    ds_svc.put_set_constraints(db, pid, sid, "Tokens: okapi teal.")
    db.refresh(doc)
    assert not docs.indexed(db, doc, include_design=True)
    assert all(c.embedding is None for c in doc.chunks)
    assert not [h for h in docs.search_docs(db, pid, "okapi", include_design=True)["results"]
                if h["kind"] == "design_prompt"]


def test_a_design_set_on_an_unbaselined_prd_is_not_approved(db, pid):
    prd = prd_svc.create_prd(db, title="P", project_id=pid, body=BODY)
    sid = _set(db, pid, prd, constraints="Tokens and Spacing.")
    out = ds_svc.get_design_set(db, pid, sid)["alignment"]
    assert out["prd_passed"] is False and out["approved_to_build"] is False


def test_the_accept_tools_are_gone_and_accept_gated_is_empty():
    names = {t["name"] for t in TOOLS}
    assert not names & {"accept_set_constraints", "accept_prompt", "accept_prompts",
                        "accept_design"}
    assert docs.ACCEPT_GATED == ()
