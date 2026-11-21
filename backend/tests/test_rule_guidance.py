"""P91 S8 (GRPH-1780): guidance for requirements no primitive expresses; gap counts stay home."""
import pytest

from app.services import box_push

OWNER = "alex@ascme-labs.com"
SECRET = "COPIED TEXT THAT MUST NOT LEAVE THE BOX"


def _login(client):
    r = client.post("/api/auth/login", json={"email": OWNER, "password": "graphban"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _call(client, key, tool, args):
    return client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                         "params": {"name": tool, "arguments": args}},
                       headers={"X-API-Key": key}).json()["result"]


@pytest.fixture()
def env(client):
    h = _login(client)
    pid = client.post("/api/projects", json={"name": "Rocket"}, headers=h).json()["id"]
    key = client.post("/api/api-keys", json={"name": "a", "project_id": pid},
                      headers=h).json()["plaintext"]
    item = _call(client, key, "create_item", {"title": "t"})["structuredContent"]
    return {"h": h, "pid": pid, "key": key, "item": item["id"],
            "cites": [{"kind": "item", "ref": item["id"]}]}


def _guidance(env, **kw):
    a = {"kind": "guidance",
         "draft": {"stage": "Build", "label": "style", "text": SECRET}, "citations": env["cites"]}
    a.update(kw)
    return a


def test_cited_guidance_is_stored_and_uncited_refused(client, env):
    r = _call(client, env["key"], "propose_rule", _guidance(env))
    assert not r.get("isError"), r
    out = r["structuredContent"]
    assert out["status"] == "open" and out["stage"] == "Build" and out["text"] == SECRET
    bad = _call(client, env["key"], "propose_rule", _guidance(env, citations=[]))
    assert bad.get("isError")
    listed = client.get(f"/api/projects/{env['pid']}/rules/guidance", headers=env["h"]).json()
    assert [g["id"] for g in listed] == [out["id"]]


def test_rest_files_guidance_too(client, env):
    r = client.post(f"/api/projects/{env['pid']}/rules/proposals", headers=env["h"],
                    json={"kind": "guidance", "stage": "Plan", "label": "naming", "text": "x",
                          "citations": env["cites"]})
    assert r.status_code == 201, r.text
    assert r.json()["stage"] == "Plan"


def test_adopted_guidance_reaches_the_brief_for_that_stage(client, env):
    g = _call(client, env["key"], "propose_rule", _guidance(env))["structuredContent"]
    base = f"/api/projects/{env['pid']}/rules/guidance/{g['id']}"
    # an agent key cannot adopt
    assert client.post(base + "/adopt", headers={"X-API-Key": env["key"]}).status_code in (401, 403)
    from app.db import SessionLocal
    from app.models import Item
    from app.services import delegation
    db = SessionLocal()
    try:
        item = db.get(Item, env["item"])
        item.status = "in_progress"
        db.commit()
        assert delegation.brief(db, item)["guidance"] == []
    finally:
        db.close()
    assert client.post(base + "/adopt", headers=env["h"]).json()["status"] == "adopted"
    db = SessionLocal()
    try:
        item = db.get(Item, env["item"])
        b = delegation.brief(db, item)
        assert b["guidance"] == [{"label": "style", "text": SECRET}]
        assert "advisory" in b["text"] and SECRET in b["text"]
        item.status = "review"  # a different stage sees none
        db.commit()
        assert delegation.brief(db, item)["guidance"] == []
    finally:
        db.close()


def _push_body(env, monkeypatch, with_counts):
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        return box_push.build_box_push(db, with_counts=with_counts)
    finally:
        db.close()


def test_gap_counts_follow_the_counts_opt_in_and_carry_no_text(client, env, monkeypatch):
    for _ in range(2):
        _call(client, env["key"], "propose_rule", _guidance(env))
    off = _push_body(env, monkeypatch, False)
    assert "primitive_gaps" not in str(off) and SECRET not in str(off)
    on = _push_body(env, monkeypatch, True)
    proj = [p for p in on["projects"] if p["project"] == "Rocket"][0]
    assert proj["counts"]["primitive_gaps"] == [{"stage": "Build", "label": "style", "count": 2}]
    assert SECRET not in str(on)
