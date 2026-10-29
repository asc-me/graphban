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
        assert r.enforcer.symbol and r.enforcer.symbol in text, r.id
        assert re.search(r"\braise\b", text), r.id


def test_gitops_rules_never_enforced_and_cover_fields():
    from app.services import gitops
    g = [r for r in R.RULES if r.gitops_field]
    assert {r.gitops_field for r in g} == set(gitops.FIELDS)
    assert all(r.enforcement == "advisory" and r.channel == "context" for r in g)


def test_source_url_uses_release_tag():
    assert R.source_url("app/x.py", "1.2.3").endswith("/blob/v1.2.3/backend/app/x.py")


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
