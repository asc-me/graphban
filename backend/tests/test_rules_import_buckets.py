"""GRPH-1950 (P92 D2): every finding lands in exactly one bucket."""
from app.services.rules_import import Citation, Finding, classify, EXTERNAL_LABEL

C = Citation("f", "1-2", "abc", "q")


def _f(**kw):
    base = dict(source_kind="ci", citation=C, stage="Build", title="t")
    base.update(kw)
    return Finding(**base)


def test_tighten_existing_library_rule_is_a_proposal():
    p = classify(_f(rule_id="build.generated_files", params={"paths": ["x/"]}))
    assert p.bucket == "proposal" and p.proposal["kind"] == "tighten"
    assert p.proposal["citations"][0]["ref"]["commit"] == "abc"


def test_exclusive_areas_custom_rule_is_a_proposal():
    p = classify(_f(source_kind="codeowners", slug="billing", primitive="exclusive_areas",
                    params={"paths": ["billing/"]}))
    assert p.bucket == "proposal" and p.proposal["kind"] == "new_rule"


def test_unevaluated_primitive_is_guidance_never_a_proposal():
    p = classify(_f(source_kind="codeowners", slug="rev", primitive="reviewer_property",
                    gap="reviewer_property"))
    assert p.bucket == "guidance" and p.proposal is None
    assert p.guidance["label"] == "reviewer_property"


def test_unexpressible_prose_is_guidance_and_files_no_rule():
    p = classify(_f(source_kind="prose", title="layering rule"))
    assert p.bucket == "guidance" and p.proposal is None


def test_external_from_parser_carries_label_and_optional_proposal():
    p = classify(_f(source_kind="forge", external=True, title="two reviews"))
    assert p.bucket == "external" and p.why == EXTERNAL_LABEL and p.proposal is None
    q = classify(_f(source_kind="forge", external=True, rule_id="merge.done_predicates",
                    params={"predicates": ["suite_green"]}))
    assert q.bucket == "external" and q.proposal["kind"] == "tighten"


def test_prose_never_files_external():
    p = classify(_f(source_kind="prose", external=True))
    assert p.bucket == "guidance" and p.external is None


def test_loosening_and_invariants_file_nothing():
    p = classify(_f(source_kind="prose", loosens=True, title="self-merge is fine"))
    assert p.bucket == "nothing" and "person-only" in p.why
    q = classify(_f(rule_id="review.not_your_own_work"))
    assert q.bucket == "nothing" and "invariant" in q.why


# ---- the payloads are fileable: feed them to the pipeline itself (bounce on attempt 1) -------

import pytest  # noqa: E402

from app.services import rule_proposals as RP  # noqa: E402


@pytest.fixture()
def project(client, monkeypatch):
    r = client.post("/api/auth/login", json={"email": "alex@ascme-labs.com",
                                             "password": "graphban"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    pid = client.post("/api/projects", json={"name": "Imp"}, headers=h).json()["id"]
    from app.services import rule_toggles as T
    from app.services import rules as R
    monkeypatch.setattr(T, "LIBRARY", dict(T.LIBRARY))
    T.register(T.LibraryRule(
        id=T.GENERATED_FILES_RULE, stage="build", title="Generated files", summary="",
        enforcement="checked",
        enforcer=R.Enforcer("graphban_source", "fleet/src/gbfleet/supervisor.py"),
        # The repo's own markers name a subset of the default, so the change is a tighten.
        default_on=True, default_params={"paths": ["gen/", "AGENTS.md"]}))
    seen = []
    monkeypatch.setattr(RP, "FILE_READER",
                        lambda repo, path, commit: seen.append((path, commit)) or True)
    return pid, seen


def _file(pid, payload):
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        row, _ = RP.propose(db, project_id=pid, proposer_kind="import", proposer="rules-import",
                            **payload)
        db.commit()
        assert row.direction == "tighten", row.direction
        return row.citations
    finally:
        db.close()


CI = Citation(".gitattributes", "3-4", "deadbeef", "gen/** linguist-generated")


def test_tighten_payload_is_accepted_by_propose(project):
    pid, seen = project
    p = classify(_f(citation=CI, rule_id="build.generated_files", params={"paths": ["gen/"]}))
    cites = _file(pid, p.proposal)
    assert seen == [(".gitattributes", "deadbeef")]
    assert cites[0]["ref"] == {"path": ".gitattributes", "commit": "deadbeef", "lines": "3-4"}


def test_new_rule_payload_is_accepted_by_propose(project):
    pid, _ = project
    owners = Citation(".github/CODEOWNERS", "7", "deadbeef", "billing/ @org/billing")
    p = classify(_f(source_kind="codeowners", citation=owners, slug="billing",
                    primitive="exclusive_areas", title="billing/ is owned by one team",
                    params={"paths": ["billing/"]}))
    assert p.proposal["why"] == "billing/ @org/billing"
    _file(pid, p.proposal)


def test_external_alongside_payload_is_accepted_by_propose(project):
    pid, _ = project
    p = classify(_f(source_kind="forge", citation=CI, external=True, title="generated files",
                    rule_id="build.generated_files", params={"paths": ["gen/"]}))
    assert p.bucket == "external" and p.external["source"]["ref"]["path"] == ".gitattributes"
    _file(pid, p.proposal)
