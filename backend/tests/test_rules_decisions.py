"""PRD-P63 §5 conformance (GRPH-1259): the built slices still match decisions D1-D13.

Each test pins one decision to a fact in the code; the behaviour itself is proven in the slice's
own tests, named in each docstring. D7's hosted filing is not built (see the last test).
"""
from app.services import observations, quotas, rule_pack, rule_toggles, rules_org
from app.services import rule_measure, rules_recommend


def test_d2_observation_kinds_cover_the_v1_list():
    # tests/test_observations.py proves each is derived from a structured record.
    v1 = {"bounce", "reviewer_sabotage_caught", "reviewer_sabotage_missed",
          "ci_red_after_sign_off", "reopened_after_done", "predicate_exempted",
          "gap_noted_at_sign_off", "refusal", "evidence_missing"}
    assert v1 <= set(observations.KINDS)


def test_d4_lock_is_a_named_refusal_rule():
    # tests/test_rule_toggles.py: project writes on a locked rule are refused with this id.
    assert rule_toggles.LOCK_RULE == "rules.org_lock_holds"
    assert callable(rule_toggles.set_org_toggle) and callable(rule_toggles.set_project_toggle)


def test_d6_org_min_projects_is_larger_of_two_and_a_fifth():
    assert rules_org.org_min_projects(3) == 2
    assert rules_org.org_min_projects(10) == 2
    assert rules_org.org_min_projects(11) == 3
    assert rules_org.org_min_projects(50) == 10


def test_d5_org_results_are_cached_for_a_week():
    assert rules_org.CACHE_TTL_SECONDS == 7 * 86400.0


def test_d9_escape_kinds_and_observed_change_label():
    assert {"ci_red_after_sign_off", "reopened_after_done", "bug_escaped"} <= set(
        rule_measure.ESCAPE_KINDS)
    assert "not a proven cause" in rule_measure.NOTE


def test_d11_premium_split_is_team_and_enterprise_only():
    on = {n for n, p in quotas.PLANS.items() if p.org_rules}
    assert on == {"team", "enterprise"}


def test_d12_pack_is_semver_and_has_both_majors_pinned():
    # tests/test_rule_pack.py loads the api0 and api1 fixtures.
    assert rule_pack.PRIMITIVE_API.count(".") == 2
    from pathlib import Path
    fx = Path(__file__).parent / "fixtures" / "rule_pack"
    assert (fx / "api0").is_dir() and (fx / "api1").is_dir()


def test_d7_known_gap_hosted_proposals_are_exported_not_filed():
    """Not conformant yet: only the self-host export report exists (rules_recommend 'proposal'
    with report={..., 'tag': 'rule-library'}). Filing on GBR/GRPH, the 'proposed to the library'
    status link, and the cross-project item link are not built. Delete this test when they are."""
    src = open(rules_recommend.__file__).read()
    assert "exportable report" in src and "not filed" in src
