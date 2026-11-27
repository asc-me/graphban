"""GRPH-P104 S3: link requests and thread messages reach agents working the PRD as a directive."""
import json

import pytest

from app.models import Agent, Item
from app.services import fleet as fleet_svc
from app.services import threads as T
from app.services import links as L
from app.services import prds as prds_svc

from tests.test_prd_links import _a, _b, world  # noqa: F401


def _holder(db, prd, project, n):
    agent = Agent(id=f"{project}-A{n}", project_id=project, number=n, label=str(n))
    db.add(agent)
    db.add(Item(id=f"{project}-I{n}", project_id=project, number=n, title="work", status="in_progress",
                claimed_by=agent.id, prd_id=prd.id))
    db.commit()
    return agent.id


def test_message_is_a_directive_on_next_heartbeat_of_b_holder(world):
    db, a, b, _x = world
    holder = _holder(db, b, "pb", 1)
    link = L.request_prd_link(db, _a(), a.id, b.id, reason="need contract")
    # The request itself is delivered first, once.
    d = fleet_svc.collect_directive(db, holder)
    assert d["type"] == "prd_link_pending" and d["pending"][0]["type"] == "prd_link_request"
    assert fleet_svc.collect_directive(db, holder) is None
    L.accept_prd_link(db, _b(), link.id)
    T.post(db, _a(), link.id, kind="question", body="which field?")
    d = fleet_svc.collect_directive(db, holder)
    assert d["pending"][0]["type"] == "prd_link_messages" and d["pending"][0]["count"] == 1
    assert fleet_svc.collect_directive(db, holder) is None


def test_own_side_message_and_non_workers_get_nothing_and_it_stays_pending(world):
    db, a, b, _x = world
    link = L.request_prd_link(db, _a(), a.id, b.id, reason="r")
    L.accept_prd_link(db, _b(), link.id)
    a_holder = _holder(db, a, "pa", 2)
    idle = Agent(id="pb-idle", project_id="pb", number=3, label="idle")
    db.add(idle)
    db.commit()
    T.post(db, _a(), link.id, kind="note", body="fyi")
    assert fleet_svc.collect_directive(db, a_holder) is None  # wrote it themselves
    assert fleet_svc.collect_directive(db, idle.id) is None
    # Nobody on B works it: still pending, visible to get_context's source.
    pend = T.pending_for_prds(db, [b.id])
    assert [p["type"] for p in pend] == ["prd_link_messages"]
    # Then a B agent claims an item and is told.
    late = _holder(db, b, "pb", 4)
    assert fleet_svc.collect_directive(db, late)["pending"][0]["count"] == 1


# ---- bounce fixes: who "works" a PRD, and which side a two-project caller is on --------------

def test_a_review_holder_and_the_prd_owner_are_told(world):
    from sqlalchemy import select

    from app.models import ApiKey, User

    db, a, b, _x = world
    reviewer = Agent(id="pb-R", project_id="pb", number=7, label="rev")
    db.add(reviewer)
    db.add(Item(id="pb-IR", project_id="pb", number=7, title="w", status="review",
                claimed_by="someone-else", review_claimed_by=reviewer.id, prd_id=b.id))
    user = db.scalar(select(User).limit(1))
    b.created_by = user.id
    key = ApiKey(id="k-own", name="own", user_id=user.id, hashed_key="h-own", prefix="gb_own",
                 scopes=["read", "write"])
    owner_agent = Agent(id="pb-O", project_id="pb", number=8, label="own", api_key_id="k-own")
    db.add(key)
    db.flush()
    db.add(owner_agent)
    db.commit()
    assert set(T.prds_worked_by(db, reviewer.id)) == {b.id}
    assert set(T.prds_worked_by(db, owner_agent.id)) == {b.id}
    link = L.request_prd_link(db, _a(), a.id, b.id, reason="r")
    assert fleet_svc.collect_directive(db, owner_agent.id)["pending"][0]["link"] == link.id
    # A closed PRD is no longer worked by its owner.
    b.close_record = {"closed": True}
    db.commit()
    assert T.prds_worked_by(db, owner_agent.id) == []


def test_a_caller_reading_both_projects_is_on_its_sending_side(world):
    from app.services.links import Caller

    db, a, b, _x = world
    link = L.request_prd_link(db, _a(), a.id, b.id, reason="r")
    L.accept_prd_link(db, _b(), link.id)
    both = Caller(readable=["pa", "pb"], writable=["pb"], is_agent=True, label="both", owner="Bo")
    msg = T.post(db, both, link.id, kind="note", body="from b")
    from app.models import PrdLinkMessage
    assert db.get(PrdLinkMessage, msg["id"]).author_side == "b"
    b_holder = _holder(db, b, "pb", 5)
    a_holder = _holder(db, a, "pa", 6)
    assert fleet_svc.collect_directive(db, b_holder) is None  # its own side wrote it
    assert fleet_svc.collect_directive(db, a_holder)["pending"][0]["count"] == 1


# ---- the five MCP tools, end to end over /api/mcp ---------------------------------------------

def _tool(client, key, name, args):
    r = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": name, "arguments": args}},
                    headers={"X-API-Key": key})
    assert r.status_code == 200, r.text
    return r.json()["result"]


@pytest.fixture()
def pair(client, auth):
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Membership, Organization, Project, User

    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.email == "alex@ascme-labs.com"))
        db.add(Organization(id="olk", name="Links Org", prd_link_accept_agents=True))
        db.flush()
        db.add_all([Project(id="pcon", name="Console", tag="PCON", org_id="olk"),
                    Project(id="pbox", name="Box", tag="PBOX", org_id="olk")])
        db.flush()
        db.add_all([Membership(user_id=user.id, project_id=p, role="owner", access="write")
                    for p in ("pcon", "pbox")])
        db.commit()
        pids = ["pcon", "pbox"]
        prds = [prds_svc.create_prd(db, title=f"P{i}", project_id=p).id
                for i, p in enumerate(pids)]
    finally:
        db.close()
    keys = [client.post("/api/api-keys", json={"name": f"k{i}", "project_id": p,
                                                "scopes": ["read", "write"]},
                        headers=auth).json()["plaintext"] for i, p in enumerate(pids)]
    return pids, prds, keys


def test_the_five_tools_carry_a_request_a_thread_and_a_suggestion(client, auth, pair):
    pids, (pa, pb), (ka, kb) = pair
    res = _tool(client, ka, "request_prd_link", {"prd": pa, "other_prd": pb, "reason": "contract"})
    assert not res.get("isError"), res
    link = res["structuredContent"]["id"]
    assert res["structuredContent"]["status"] == "pending"
    # B sees it in list_threads, accepts it (the org lets agents decide).
    listed = _tool(client, kb, "list_threads", {})["structuredContent"]["links"]
    assert [x["id"] for x in listed] == [link]
    assert _tool(client, kb, "decide_prd_link",
                 {"link": link, "decision": "accept"})["structuredContent"]["status"] == "accepted"
    # A asks; B reads the thread.
    q = _tool(client, ka, "post_comment", {"link": link, "kind": "question", "body": "which field?"})
    assert not q.get("isError"), q
    thread = _tool(client, kb, "list_threads", {"link": link})["structuredContent"]
    assert [m["body"] for m in thread["messages"]] == ["which field?"]
    assert thread["read_only"] is False
    # A suggests an item for B; B files it with its own key, in its own project.
    s = _tool(client, ka, "post_comment", {"link": link, "kind": "suggested_item",
                                           "item": {"title": "add the field"}})
    assert not s.get("isError"), s
    filed = _tool(client, kb, "file_suggested_item",
                  {"link": link, "message": s["structuredContent"]["id"], "decision": "file"})
    out = filed["structuredContent"]
    assert out["decided"] is True and out["outcome"]["outcome"] == "filed", out
    # A cannot file its own suggestion: it does not write B's project.
    s2 = _tool(client, ka, "post_comment", {"link": link, "kind": "suggested_item",
                                            "item": {"title": "another"}})
    mine = _tool(client, ka, "file_suggested_item",
                 {"link": link, "message": s2["structuredContent"]["id"], "decision": "file"})
    assert mine.get("isError"), mine


def test_an_agent_decision_is_needs_person_by_default(client, auth, pair):
    from app.db import SessionLocal
    from app.models import Organization, Project

    pids, (pa, pb), (ka, kb) = pair
    db = SessionLocal()
    try:
        db.get(Organization, db.get(Project, pids[0]).org_id).prd_link_accept_agents = False
        db.commit()
    finally:
        db.close()
    link = _tool(client, ka, "request_prd_link",
                 {"prd": pa, "other_prd": pb, "reason": "r"})["structuredContent"]["id"]
    res = _tool(client, kb, "decide_prd_link", {"link": link, "decision": "accept"})
    assert res.get("isError") and "needs_person" in json.dumps(res), res
    res = _tool(client, kb, "decide_prd_link", {"link": link, "decision": "unlink"})
    assert res.get("isError") and "needs_person" in json.dumps(res), res
    # A post on a pending link is refused: the thread is read-only until accepted.
    res = _tool(client, ka, "post_comment", {"link": link, "kind": "note", "body": "hi"})
    assert res.get("isError"), res
