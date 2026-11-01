"""Flow model (GRPH-1156): stage state is the weakest used rule; unused rules stay drawn."""
import pytest

from app.models import Project
from app.services import rules as R


def _stage(f, sid):
    return next(s for s in f["stages"] if s["id"] == sid)


def _rule(f, rid):
    return next(r for s in f["stages"] for r in s["rules"] if r["id"] == rid)


def _restage(monkeypatch, moves):
    """Move rules between stages: `moves` maps a rule id, or a stage id (all its rules), to a
    new stage. The stage lives on the rule itself, so the registry is what gets patched."""
    import dataclasses
    rules = tuple(
        dataclasses.replace(r, stage=moves.get(r.id, moves.get(r.stage, r.stage))) for r in R.RULES)
    monkeypatch.setattr(R, "RULES", rules)


def test_every_registry_rule_is_placed_on_a_fixed_stage():
    # One vocabulary: the registry's stage IS the §4 stage id (GRPH-1196).
    assert {r.stage for r in R.RULES} <= set(R.STAGE_IDS)
    assert not hasattr(R, "RULE_STAGE")


def test_flow_carries_control_and_resolves_gitops_once(db, monkeypatch):
    from app.services import gitops
    calls = []
    real = gitops.resolve
    monkeypatch.setattr(gitops, "resolve", lambda *a, **k: calls.append(1) or real(*a, **k))
    db.add(Project(id="flowp", name="p", tag="FP", gitops_model="prs_to_base"))
    db.commit()
    f = R.flow(db, "flowp")
    assert len(calls) == 1
    assert f["control"]["state"] == "local" and set(f["control"]) >= {"writable", "org_name"}


def test_budget_cap_carries_value_and_source(db):
    from app.services import fleet_profiles
    p = Project(id="flowp", name="p", tag="FP", gitops_model="prs_to_base")
    db.add(p)
    db.commit()
    cap = _rule(R.flow(db, "flowp"), "plan.budget_cap")
    assert cap["truth"] == "db" and cap["enforcement"] == "external"
    assert (cap["value"], cap["source"]) == (None, "unmeasured")
    fleet_profiles.set_policy(db, p, {"caps": {"per_item_tokens": 5000}})
    cap = _rule(R.flow(db, "flowp"), "plan.budget_cap")
    assert (cap["value"], cap["source"]) == ("5000", "project")
    assert _stage(R.flow(db, "flowp"), "plan")["rules"].count(cap) == 1


def test_weakest_order():
    assert R.weakest(["enforced", "checked"]) == "checked"
    assert R.weakest(["checked", "external"]) == "external"
    assert R.weakest(["external", "advisory"]) == "advisory"
    assert R.weakest(["advisory", "unmeasured", "enforced"]) == "unmeasured"


def _flow_for(db, model, **cols):
    p = Project(id="flowp", name="p", tag="FP", **{f"gitops_{k}": v for k, v in cols.items()}, gitops_model=model)
    db.add(p)
    db.commit()
    return R.flow(db, p.id)


@pytest.fixture
def db():
    from app.db import SessionLocal
    s = SessionLocal()
    yield s
    s.query(Project).filter_by(id="flowp").delete()
    s.commit()
    s.close()


def test_flow_is_whole_and_ordered(db):
    f = _flow_for(db, "prs_to_base", reviewer_bar="both")
    assert [s["id"] for s in f["stages"]] == list(R.STAGE_IDS)
    assert len(f["edges"]) == 6
    # Plan holds the external budget cap beside its enforced rules -> weakest.
    assert _stage(f, "plan")["state"] == "external"
    # Review mixes enforced and advisory rules -> weakest.
    assert _stage(f, "review")["state"] == "advisory"


def test_a_stage_with_no_rules_is_unmeasured_not_clean(db, monkeypatch):
    # Every Learn rule moved off it: nothing on the stage was looked at.
    _restage(monkeypatch, {"learn": "build"})
    f = _flow_for(db, "prs_to_base", reviewer_bar="both")
    assert _stage(f, "learn")["rules"] == [] and _stage(f, "learn")["state"] == "unmeasured"


def test_unset_gitops_value_makes_its_stage_unmeasured(db):
    f = _flow_for(db, "prs_to_base")
    assert _stage(f, "review")["state"] == "unmeasured"


def test_push_to_base_marks_pr_step_rules_not_used(db):
    f = _flow_for(db, "push_to_base", reviewer_bar="forge")
    for rid in ("gitops.pr_title_pattern", "gitops.no_push_to_base", "gitops.reviewer_bar"):
        r = _rule(f, rid)
        assert r["used"] is False and r["label"] == R.NOT_USED_LABEL, rid
    assert _rule(f, "gitops.base_branch")["used"] is True
    assert _rule(f, "merge.ci_green")["used"] is True


def test_reviewer_bar_still_used_under_push_to_base_unless_forge(db):
    f = _flow_for(db, "push_to_base", reviewer_bar="sign_off")
    assert _rule(f, "gitops.reviewer_bar")["used"] is True


def test_prs_to_base_uses_pr_rules(db):
    f = _flow_for(db, "prs_to_base", reviewer_bar="forge")
    assert _rule(f, "gitops.pr_title_pattern")["used"] is True
    assert _rule(f, "gitops.reviewer_bar")["used"] is True


def test_stage_not_used_only_when_every_rule_is(db, monkeypatch):
    f = _flow_for(db, "push_to_base", reviewer_bar="forge")
    # Merge keeps base_branch and CI, so it is not NOT_USED.
    assert _stage(f, "merge")["state"] != "not_used"
    # Force a stage whose only rules are unused: clear Learn, then put two unused rules on it.
    _restage(monkeypatch, {"learn": "build", "gitops.pr_title_pattern": "learn",
                           "gitops.no_push_to_base": "learn"})
    f = R.flow(db, "flowp")
    assert _stage(f, "learn")["state"] == "not_used"


def test_skills_are_one_collapsed_advisory_node(db):
    f = _flow_for(db, "prs_to_base")
    n = _rule(f, "agents.skills_pack")
    assert n["enforcement"] == "advisory" and n["truth"] == "repo_file"
    assert n["title"] == "Agent instructions (Graphban pack)"
    import tomllib
    from pathlib import Path
    version = tomllib.loads(
        (Path(__file__).resolve().parents[2] / "cli" / "pyproject.toml").read_text())["project"]["version"]
    assert n["skills_version"] == version
    assert len(n["skills"]) == 10
    base = f"https://github.com/asc-me/graphban/blob/cli-v{version}/cli/src/gban/skills"
    assert {"name": "graphban-work", "url": f"{base}/graphban-work/SKILL.md"} in n["skills"]


def test_shipped_skills_prefers_package_data_and_is_none_when_absent(tmp_path):
    from app.services import rules as R
    f = tmp_path / "s.json"
    f.write_text('{"version": "1.2.3", "skills": ["b", "a"]}')
    got = R.shipped_skills(f, tmp_path / "nocli")
    assert got["version"] == "1.2.3"
    assert [s["name"] for s in got["skills"]] == ["a", "b"]
    assert got["skills"][0]["url"].endswith("/blob/cli-v1.2.3/cli/src/gban/skills/a/SKILL.md")
    assert R.shipped_skills(tmp_path / "missing.json", tmp_path / "nocli") is None


def test_flow_endpoint_serves_the_linked_skills(client, auth):
    """The response schema carries `{name, url}` and the version, not bare names."""
    r = client.get("/api/rules/flow?project_id=core", headers=auth)
    assert r.status_code == 200
    n = _rule(r.json(), "agents.skills_pack")
    assert n["skills_version"] and len(n["skills"]) == 10
    assert all(f"/blob/cli-v{n['skills_version']}/" in s["url"] for s in n["skills"])


# --- merge.ci_green last_result ---------------------------------------------------------

def _item(db, n, status="done", passed=True, pr=True, ago=0):
    from datetime import timedelta
    from app.models import Item, utcnow
    ev = [{"kind": "attestation", "adapter": "ci", "commit": f"abc{n}234",
           "predicates": [{"name": "suite_green", "passed": passed}]}]
    if pr:
        ev.append({"kind": "url", "url": f"https://github.com/o/r/pull/{n}"})
    it = Item(id=f"ci-it-{n}", project_id="flowp", number=n, title="x", status=status,
              evidence=ev, updated_at=utcnow() - timedelta(hours=ago))
    db.add(it)
    db.commit()
    return it


def _ci(db):
    return _rule(R.flow(db, "flowp"), "merge.ci_green")["last_result"]


@pytest.fixture
def flowp(db):
    from app.models import Item
    db.add(Project(id="flowp", name="p", tag="FP", gitops_model="prs_to_base"))
    db.commit()
    yield
    db.query(Item).filter(Item.project_id == "flowp").delete()
    db.commit()


def test_ci_result_pass_carries_item_commit_and_date(db, flowp):
    _item(db, 1)
    r = _ci(db)
    assert r["outcome"] == "pass" and r["commit"] == "abc1234" and r["at"]
    assert r["item"].endswith("1")


def test_ci_result_null_when_none(db, flowp):
    assert _ci(db) is None


def test_ci_result_fail(db, flowp):
    _item(db, 1, passed=False)
    assert _ci(db)["outcome"] == "fail"


def test_ci_result_ignores_unmerged_items(db, flowp):
    # An attestation on an item whose PR never merged must not be read.
    _item(db, 1, status="review")
    _item(db, 2, pr=False)
    assert _ci(db) is None


def test_ci_result_is_the_latest_merged(db, flowp):
    _item(db, 1, passed=False, ago=5)
    _item(db, 2, passed=True, ago=1)
    assert _ci(db)["outcome"] == "pass"
