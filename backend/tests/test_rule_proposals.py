"""P91 S3 (GRPH-1775): rule proposals, direction enforcement, folding."""
import pytest

from app.services import rule_proposals as RP

OWNER = "alex@ascme-labs.com"


def _login(client):
    r = client.post("/api/auth/login", json={"email": OWNER, "password": "graphban"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def env(client, monkeypatch):
    h = _login(client)
    pid = client.post("/api/projects", json={"name": "Rocket"}, headers=h).json()["id"]
    key = client.post("/api/api-keys", json={"name": "a", "project_id": pid},
                      headers=h).json()["plaintext"]
    from app.db import SessionLocal
    from app.models import Observation
    item_id = _call(client, key, "create_item", {"title": "t"})["structuredContent"]["id"]
    db = SessionLocal()
    try:
        obs = [Observation(project_id=pid, kind="refusal", stage="Plan", dedupe_key=f"d{i}")
               for i in range(2)]
        db.add_all(obs)
        db.commit()
        oids = [o.id for o in obs]
    finally:
        db.close()
    monkeypatch.setattr(RP, "FILE_READER",
                        lambda repo, path, commit: path == "backend/ok.py" or None
                        if path != "gone.py" else False)
    cites = [{"kind": "item", "ref": item_id}] + [{"kind": "observation", "ref": o} for o in oids]
    return {"h": h, "pid": pid, "key": key, "cites": cites}


def _call(client, key, tool, args):
    r = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": tool, "arguments": args}},
                    headers={"X-API-Key": key}).json()["result"]
    return r


def _new_rule(env, **kw):
    a = {"kind": "new_rule",
         "draft": {"primitive": "exclusive_areas", "slug": "areas", "title": "Areas", "why": "migrations clash",
                   "params": {"paths": ["db/migrations"]}},
         "citations": env["cites"]}
    a.update(kw)
    return a


def test_agent_files_new_rule_with_citations(client, env):
    r = _call(client, env["key"], "propose_rule", {**_new_rule(env), "agent_id": "GRPH-A1"})
    assert not r.get("isError"), r
    out = r["structuredContent"]
    assert out["proposer"] == "GRPH-A1" and out["proposer_kind"] == "agent"
    assert out["status"] == "open" and out["direction"] == "tighten" and not out["loosens"]
    assert len(out["citations"]) == 3 and out["rule_id"] == "custom.areas"


def test_agent_cannot_turn_off_and_loosening_tighten_refused(client, env):
    _call(client, env["key"], "propose_rule", _new_rule(env))
    # turning off is refused for any proposer, naming the rule
    r = _call(client, env["key"], "propose_rule",
              {"kind": "turn_off", "draft": {"rule_id": "custom.areas"}, "citations": env["cites"]})
    assert r["isError"] is True and "custom.areas" in str(r)
    # adopt the rule as a person would so a tighten has a current value
    h = env["h"]
    ok = client.post(f"/api/projects/{env['pid']}/rules/custom", headers=h, json={
        "slug": "live", "primitive": "exclusive_areas", "title": "L", "why": "w",
        "params": {"paths": ["a", "b"]}, "reason": "r"})
    assert ok.status_code == 201, ok.text
    loosen = _call(client, env["key"], "propose_rule", {
        "kind": "tighten", "draft": {"rule_id": "custom.live", "params": {"paths": ["a"]}},
        "citations": env["cites"]})
    assert loosen["isError"] is True and "custom.live" in str(loosen)
    tight = _call(client, env["key"], "propose_rule", {
        "kind": "tighten", "draft": {"rule_id": "custom.live", "params": {"paths": ["a", "b", "c"]}},
        "citations": env["cites"]})
    assert not tight.get("isError"), tight
    assert tight["structuredContent"]["direction"] == "tighten"


def test_generated_files_loosening_agent_refused_import_accepted(client, env):
    args = {"kind": "new_rule",
            "draft": {"primitive": "generated_files", "slug": "gen", "title": "G", "why": "w",
                       "params": {"paths": ["x"]}},
            "citations": env["cites"]}
    r = _call(client, env["key"], "propose_rule", args)
    assert r["isError"] is True and "loosen" in str(r)
    ok = client.post(f"/api/projects/{env['pid']}/rules/proposals", headers=env["h"],
                     json={**args["draft"], **{k: v for k, v in args.items() if k != "draft"},
                           "import_run": "run-1"})
    assert ok.status_code == 201, ok.text
    body = ok.json()
    assert body["loosens"] is True and body["proposer_kind"] == "import"
    assert body["proposer"] == "import:run-1"


def test_no_citation_or_missing_file_refused(client, env):
    r = _call(client, env["key"], "propose_rule", _new_rule(env, citations=[]))
    assert r["isError"] is True
    gone = {"kind": "file", "ref": {"path": "gone.py", "commit": "abc123", "lines": "1-2"}}
    r = _call(client, env["key"], "propose_rule", _new_rule(env, citations=[gone]))
    assert r["isError"] is True and "does not exist" in str(r)
    unk = {"kind": "file", "ref": {"path": "other.py", "commit": "abc123", "lines": "1"}}
    r = _call(client, env["key"], "propose_rule", _new_rule(env, citations=[unk]))
    assert r["isError"] is True and "could not verify" in str(r)
    good = {"kind": "file", "ref": {"path": "backend/ok.py", "commit": "abc123", "lines": "1"}}
    r = _call(client, env["key"], "propose_rule", _new_rule(env, citations=[good]))
    assert not r.get("isError"), r
    bad_item = _call(client, env["key"], "propose_rule",
                     _new_rule(env, citations=[{"kind": "item", "ref": "NOPE-1"}]))
    assert bad_item["isError"] is True


def test_duplicate_folds_into_open_proposal(client, env):
    a = _call(client, env["key"], "propose_rule", _new_rule(env))["structuredContent"]
    b = _call(client, env["key"], "propose_rule", _new_rule(
        env, citations=[{"kind": "url", "ref": "https://example.com/x"}]))["structuredContent"]
    assert b["id"] == a["id"] and b["folded"] is True and len(b["citations"]) == 4


def test_invalid_draft_and_no_comparator_refused(client, env):
    r = _call(client, env["key"], "propose_rule", _new_rule(env, draft={"primitive": "nope", "slug": "areas", "title": "A", "why": "w"}))
    assert r["isError"] is True
    r = _call(client, env["key"], "propose_rule", _new_rule(env, draft={"primitive": "exclusive_areas", "slug": "areas", "title": "A",
                                                           "why": "w", "params": {"bogus": 1}}))
    assert r["isError"] is True
    client.post(f"/api/projects/{env['pid']}/rules/custom", headers=env["h"], json={
        "slug": "pc", "primitive": "path_classes", "title": "P", "why": "w",
        "params": {"docs": ["a"]}, "reason": "r"})
    r = _call(client, env["key"], "propose_rule", {
        "kind": "tighten", "draft": {"rule_id": "custom.pc", "params": {"docs": ["a", "b"]}},
        "citations": env["cites"]})
    assert r["isError"] is True and "comparator" in str(r)


def test_invariant_refused(client, env):
    r = _call(client, env["key"], "propose_rule", {
        "kind": "turn_on", "draft": {"rule_id": "rules.person_adopts"}, "citations": env["cites"]})
    assert r["isError"] is True and "invariant" in str(r)


def test_new_rule_folds_across_slugs(client, env):
    a = _call(client, env["key"], "propose_rule", _new_rule(env))["structuredContent"]
    other = _new_rule(env)
    other["draft"] = {**other["draft"], "slug": "areas2"}
    b = _call(client, env["key"], "propose_rule", other)["structuredContent"]
    assert b["id"] == a["id"] and b["folded"] is True


def test_rest_unknown_rule_is_404_not_500(client, env):
    """A `tighten` naming a rule the project does not have is a 404 on REST, never a 500."""
    r = client.post(f"/api/projects/{env['pid']}/rules/proposals", headers=env["h"],
                    json={"kind": "tighten", "rule_id": "custom.nope",
                          "params": {"paths": ["a"]}, "citations": env["cites"][:1]})
    assert r.status_code == 404, r.text
    assert "custom.nope" in r.json()["detail"]
