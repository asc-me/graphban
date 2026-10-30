"""Registry drift fails the PR that causes it (GRPH-1155)."""
import re

from app.services import rules as R


def test_ids_unique_and_vocab():
    assert len(R.RULES_BY_ID) == len(R.RULES)
    for r in R.RULES:
        assert re.fullmatch(r"[a-z_]+\.[a-z_]+", r.id)
        assert r.enforcement in R.ENFORCEMENTS
        assert r.enforcer.kind in R.ENFORCER_KINDS
        assert r.truth in R.TRUTHS and r.scope in R.SCOPES
        assert set(r.models) <= set(R.GITOPS_ALL_MODELS)
        assert (r.channel in R.CHANNELS) == (r.enforcement == "advisory")


def test_graphban_source_paths_exist():
    for r in R.RULES:
        if r.enforcer.kind == "graphban_source":
            assert (R.BACKEND_ROOT / r.enforcer.ref).is_file(), r.id


def test_enforced_rules_have_a_refusal_path():
    for r in R.RULES:
        if r.enforcement != "enforced":
            continue
        assert r.enforcer.kind == "graphban_source", r.id
        text = (R.BACKEND_ROOT / r.enforcer.ref).read_text()
        assert R.raises_symbol(text, r.enforcer.symbol, r.id), r.id


def test_raises_symbol_ties_the_name_to_a_raise():
    # A name in a comment, a string, or an unrelated raise is not a refusal path.
    assert not R.raises_symbol("# SelfReview\nraise ValueError('x')\n", "SelfReview")
    assert not R.raises_symbol("x = 'SelfReview'\nclass SelfReview(Exception): pass\n",
                               "SelfReview")
    assert R.raises_symbol("raise SelfReview('own work')\n", "SelfReview")
    assert R.raises_symbol("raise authz.Forbidden\n", "Forbidden")
    assert R.raises_symbol("def gate():\n    if 1:\n        raise Refused(409, 'x')\n", "gate")
    assert not R.raises_symbol("def gate():\n    if 1:\n        raise X()\n", "gate")
    assert not R.raises_symbol("def gate():\n    return 1\n", "gate")


def test_every_stage_is_a_flow_stage_id():
    assert {r.stage for r in R.RULES} <= set(R.STAGE_IDS)


def test_function_enforcer_must_raise_a_refusal():
    # An Unavailable is an error path, not a refusal (GRPH-1196, gap 17).
    assert not R.raises_symbol("def publish():\n    raise Unavailable('x')\n", "publish")
    assert R.raises_symbol("def gate():\n    raise Refused(409, 'x')\n", "gate")
    assert R.raises_symbol("def gate():\n    raise authz.Forbidden('x')\n", "gate")


def test_shared_refusal_class_must_name_the_rule():
    src = ("def a():\n    raise Refused(409, 'x', rule_id='r.one')\n"
           "def b():\n    raise Refused(409, 'x', rule_id='r.two')\n")
    assert R.raises_symbol(src, "Refused", "r.one") and R.raises_symbol(src, "Refused", "r.two")
    only_one = "raise Refused(409, 'x', rule_id='r.one')\n"
    assert not R.raises_symbol(only_one, "Refused", "r.two")


def test_deleting_either_reach_raise_fails():
    text = (R.BACKEND_ROOT / "app/services/delegation.py").read_text()
    for rid in ("reach.deploy_not_delegable", "reach.acknowledge_signals"):
        assert R.raises_symbol(text, "Refused", rid), rid
        gutted = text.replace(f'rule_id="{rid}"', 'rule_id="gone"')
        assert not R.raises_symbol(gutted, "Refused", rid), rid


def test_enforced_rules_do_not_rest_on_a_non_refusing_function(monkeypatch):
    # Sabotage: point memory.independent_adjudication back at agent_publish while it claims
    # enforced — the refusal-path check must fail it.
    import dataclasses
    r = R.RULES_BY_ID["memory.independent_adjudication"]
    assert r.enforcement != "enforced"
    text = (R.BACKEND_ROOT / r.enforcer.ref).read_text()
    assert not R.raises_symbol(text, r.enforcer.symbol, r.id)
    claimed = dataclasses.replace(r, enforcement="enforced")
    assert not R.raises_symbol(text, claimed.enforcer.symbol, claimed.id)


def test_budget_cap_and_forge_rules():
    cap = R.RULES_BY_ID["plan.budget_cap"]
    assert (cap.stage, cap.truth, cap.enforcement, cap.enforcer.kind) == (
        "plan", "db", "external", "supervisor")
    assert "budget.cap" not in R.RULES_BY_ID
    checks = R.RULES_BY_ID["forge.required_checks"]
    assert checks.enforcement == "external" and checks.enforcer.kind == "forge"
    assert checks.truth != "code" and R.RULES_BY_ID["forge.branch_protection"].truth != "code"


def test_gitops_rules_cover_the_view():
    g = [r for r in R.RULES if r.gitops_field]
    assert {r.gitops_field for r in g} == R.gitops_view_fields()
    assert {"version_from", "release_defined_in"} <= R.gitops_view_fields()
    assert all(r.enforcement == "advisory" and r.channel == "context" for r in g)


def test_db_values_read_from_the_gitops_view(client):
    from app.db import SessionLocal
    from app.services import gitops
    from app.models import Project
    db = SessionLocal()
    try:
        # A measured value must render as itself, not as `unmeasured` (the bounce on 7909e932).
        p = db.get(Project, "core")
        p.gitops_version_scheme, p.gitops_release_defined_in = "calver", "docs/release.md"
        db.commit()
        view = gitops.resolve(db, "core")
        assert view.version_from.source == "project"
        out = {x["id"]: x for x in R.registry(db, "core")}
    finally:
        db.close()
    for r in R.RULES:
        if not r.gitops_field:
            continue
        holder = view.fields if r.gitops_field in gitops.FIELDS else view
        f = getattr(holder, r.gitops_field)
        assert (out[r.id]["value"], out[r.id]["source"]) == (f.value, f.source), r.id
        expected = "unmeasured" if f.source == "unmeasured" else "advisory"
        assert out[r.id]["enforcement"] == expected, r.id
    assert out["release.version_from"]["value"] == "calver"
    assert out["release.version_from"]["enforcement"] == "advisory"
    assert out["release.release_defined_in"]["value"] == "docs/release.md"


def test_source_url_uses_release_tag():
    url = R.source_url("app/x.py", "2026.10.24")
    assert url == f"{R.REPO_URL}/blob/2026.10.24/backend/app/x.py"
    assert "/blob/v" not in url


def test_url_tag_matches_release_script_tag_format():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import graphban_release as rel

    tag = rel.format_calver(2026, 10, 24)
    assert rel.normalize_tag(tag) == tag  # the script publishes the tag unprefixed
    assert R.source_url("app/x.py", tag).split("/blob/")[1].split("/")[0] == tag
    assert R.repo_file_url("AGENTS.md", tag).split("/blob/")[1].split("/")[0] == tag


def test_ci_job_and_doc_enforcers_have_urls():
    seen = set()
    for r in R.RULES:
        if r.enforcer.kind in ("ci_job", "doc"):
            seen.add(r.enforcer.kind)
            out = R._enforcer_out(r.enforcer)
            assert out["url"].startswith(R.REPO_URL + "/blob/"), r.id
            assert out["url"].endswith("/" + r.enforcer.ref), r.id
        elif r.enforcer.kind in ("forge", "supervisor"):
            assert R._enforcer_out(r.enforcer)["url"] == "", r.id
    assert seen == {"ci_job", "doc"}


def test_endpoint(client, auth):
    r = client.get("/api/rules?project_id=core", headers=auth)
    assert r.status_code == 200
    body = r.json()
    ids = {x["id"] for x in body["rules"]}
    assert "gate.approval_earned_by_grill" in ids
    gi = next(x for x in body["rules"] if x["id"] == "gitops.base_branch")
    assert gi["enforcement"] in ("advisory", "unmeasured")
    assert gi["source"] in ("project", "org", "unmeasured")
    assert client.get("/api/rules").status_code in (401, 403)


def test_flow_endpoint(client, auth):
    r = client.get("/api/rules/flow?project_id=core", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert [s["id"] for s in body["stages"]] == list(R.STAGE_IDS)
    assert body["edges"] == [list(e) for e in R.EDGES]
    placed = {x["id"] for s in body["stages"] for x in s["rules"]}
    assert set(R.RULES_BY_ID) <= placed and "agents.skills_pack" in placed
    for s in body["stages"]:
        assert s["state"] in R.ENFORCEMENT_ORDER + ("not_used",)
        assert all(isinstance(x["used"], bool) for x in s["rules"])
    assert client.get("/api/rules/flow").status_code in (401, 403)
