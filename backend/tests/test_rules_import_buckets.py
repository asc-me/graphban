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
    assert p.proposal["citations"][0]["commit"] == "abc"


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
