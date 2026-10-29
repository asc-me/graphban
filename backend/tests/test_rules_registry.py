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
        assert R.raises_symbol(text, r.enforcer.symbol), r.id


def test_raises_symbol_ties_the_name_to_a_raise():
    # A name in a comment, a string, or an unrelated raise is not a refusal path.
    assert not R.raises_symbol("# SelfReview\nraise ValueError('x')\n", "SelfReview")
    assert not R.raises_symbol("x = 'SelfReview'\nclass SelfReview(Exception): pass\n",
                               "SelfReview")
    assert R.raises_symbol("raise SelfReview('own work')\n", "SelfReview")
    assert R.raises_symbol("raise authz.Forbidden\n", "Forbidden")
    assert R.raises_symbol("def gate():\n    if 1:\n        raise X()\n", "gate")
    assert not R.raises_symbol("def gate():\n    return 1\n", "gate")


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
