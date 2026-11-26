"""P92 D4 (GRPH-1952): the skill's half of an import run.

The customer's agent files through `propose_rule` with `import_run`, so its findings land on the
run the CLI opened and in that run's adopt-the-batch, while every agent refusal still applies.
A run no agent attached to lists every prose source as unread and never reads as clean.
"""
import pytest

OWNER = "alex@ascme-labs.com"
SHA = "abc123"


def _call(client, key, tool, args):
    return client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                         "params": {"name": tool, "arguments": args}},
                       headers={"X-API-Key": key}).json()["result"]


@pytest.fixture()
def env(client, monkeypatch):
    from app.services import rule_proposals as RP
    monkeypatch.setattr(RP, "FILE_READER", lambda repo, path, commit: path != "gone.md")
    r = client.post("/api/auth/login", json={"email": OWNER, "password": "graphban"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    pid = client.post("/api/projects", json={"name": "Rocket"}, headers=h).json()["id"]
    key = client.post("/api/api-keys", json={"name": "a", "project_id": pid},
                      headers=h).json()["plaintext"]
    base = f"/api/projects/{pid}/rules/import-runs"
    # What `gban rules import --prose` posts: the commit and the prose sources, all unread.
    run = client.post(base, headers=h, json={
        "commit": SHA, "sources_read": [{"path": "AGENTS.md"}],
        "sources_unread": [{"path": "CONTRIBUTING.md"}, {"path": "docs/adr/0001-db.md"}]})
    assert run.status_code == 201, run.text
    return {"h": h, "pid": pid, "key": key, "base": base, "run": run.json()}


def _cite(path="CONTRIBUTING.md", commit=SHA):
    return [{"kind": "file", "ref": {"path": path, "commit": commit, "lines": "12"}}]


def _rule(env, **kw):
    a = {"import_run": env["run"]["id"], "kind": "new_rule", "quote": "migrations are serialised",
         "draft": {"primitive": "exclusive_areas", "slug": "migs", "title": "Migrations",
                   "why": "migrations clash", "params": {"paths": ["db/migrations"]}},
         "citations": _cite()}
    a.update(kw)
    return a


def test_no_agent_attached_lists_every_prose_source_unread(client, env):
    rep = env["run"]
    unread = {s["path"]: s for s in rep["sources_unread"]}
    # AGENTS.md was claimed read by a person's call; only an agent reads prose.
    assert set(unread) == {"AGENTS.md", "CONTRIBUTING.md", "docs/adr/0001-db.md"}
    assert all(s["reader"] == "agent" for s in unread.values())
    assert rep["sources_read"] == [] and rep["clean"] is False
    listed = client.get(env["base"], headers=env["h"]).json()[0]
    assert listed["unread"] == 3 and listed["unread_prose"] == 3


def test_agent_marks_prose_read_and_the_run_can_be_clean(client, env):
    paths = ["AGENTS.md", "CONTRIBUTING.md", "docs/adr/0001-db.md"]
    r = _call(client, env["key"], "propose_rule",
              {"import_run": env["run"]["id"], "sources_read": paths})
    assert not r.get("isError"), r
    assert r["structuredContent"]["sources_unread"] == []
    rep = client.get(f"{env['base']}/{env['run']['id']}", headers=env["h"]).json()
    assert rep["clean"] is True and {s["path"] for s in rep["sources_read"]} == set(paths)


def test_agent_cannot_clear_a_source_that_is_not_prose_awaiting_it(client, env):
    other = client.post(env["base"], headers=env["h"], json={
        "commit": SHA, "sources_unread": [
            {"path": "branch protection", "reason": "token lacks admin:read"}]}).json()
    r = _call(client, env["key"], "propose_rule",
              {"import_run": other["id"], "sources_read": ["branch protection"]})
    assert r["isError"] is True and "not a prose source" in str(r)
    rep = client.get(f"{env['base']}/{other['id']}", headers=env["h"]).json()
    assert rep["clean"] is False and len(rep["sources_unread"]) == 1


def test_agent_finding_lands_on_the_run_and_its_batch(client, env):
    r = _call(client, env["key"], "propose_rule", {**_rule(env), "agent_id": "GRPH-A1"})
    assert not r.get("isError"), r
    out = r["structuredContent"]
    assert out["import_run"] == env["run"]["id"] and out["bucket"] == "proposed"
    # Filed as the agent, not as the run: a run does not lend an agent a person's latitude.
    assert out["proposer_kind"] == "agent" and out["proposer"] == "GRPH-A1"
    g = _call(client, env["key"], "propose_rule", {
        "import_run": env["run"]["id"], "kind": "guidance", "quote": "keep modules small",
        "draft": {"stage": "Build", "label": "module size", "text": "keep modules small"},
        "citations": _cite()})
    assert not g.get("isError"), g
    ext = _call(client, env["key"], "propose_rule", {
        "import_run": env["run"]["id"], "kind": "external", "quote": "CI must be green",
        "draft": {"stage": "Merge", "label": "required checks", "text": "branch protection"},
        "citations": _cite()})
    assert not ext.get("isError"), ext
    rep = client.get(f"{env['base']}/{env['run']['id']}", headers=env["h"]).json()
    by = {f["bucket"]: f for f in rep["findings"]}
    assert set(by) == {"proposed", "gap", "external"}
    assert by["proposed"]["source"] == {"path": "CONTRIBUTING.md", "lines": "12", "commit": SHA}
    assert by["proposed"]["quote"] == "migrations are serialised"
    assert by["proposed"]["stage"] == "Plan" and by["proposed"]["proposal_id"] == out["id"]
    assert by["gap"]["guidance_id"] and by["external"]["proposal_id"] is None
    ok = client.post(f"{env['base']}/{env['run']['id']}/adopt", headers=env["h"],
                     json={"finding_ids": [by["proposed"]["id"]], "reason": "ours"})
    assert ok.status_code == 200, ok.text
    assert ok.json()[0]["status"] == "adopted"


def test_uncited_skill_finding_is_refused_and_kept_on_the_run(client, env):
    """The citation refusal is the server's, so it holds whatever the skill's text says."""
    for cites, says in (([], "cites the line"),
                        ([{"kind": "item", "ref": "NOPE-1"}], "cites the line"),
                        (_cite("gone.md"), "does not exist"),
                        (_cite(commit="def456"), "read abc123")):
        r = _call(client, env["key"], "propose_rule", _rule(env, citations=cites))
        assert r["isError"] is True and says in str(r), (cites, r)
    # A file citation that passes the run's checks but no other citation is still verified.
    r = _call(client, env["key"], "propose_rule",
              _rule(env, citations=_cite() + [{"kind": "item", "ref": "NOPE-1"}]))
    assert r["isError"] is True and "NOPE-1" in str(r)
    rep = client.get(f"{env['base']}/{env['run']['id']}", headers=env["h"]).json()
    assert len(rep["findings"]) == 5 and all(f["refused"] for f in rep["findings"])
    assert all(f["proposal_id"] is None for f in rep["findings"]) and rep["clean"] is False
    props = client.get(f"/api/projects/{env['pid']}/rules/proposals", headers=env["h"]).json()
    assert props == []


def test_agent_refusals_hold_on_a_run(client, env):
    r = _call(client, env["key"], "propose_rule", _rule(env, kind="turn_off",
              draft={"rule_id": "review.independent"}))
    assert r["isError"] is True
    r = _call(client, env["key"], "propose_rule", _rule(env, quote=""))
    assert r["isError"] is True and "quote" in str(r)
    r = _call(client, env["key"], "propose_rule", _rule(env, import_run=99999))
    assert r["isError"] is True and "not found" in str(r)
