"""GRPH-P104 S1: PRD links request/accept, one thread per pair, metadata-only reads."""
import pytest
from sqlalchemy import select

from app.models import Item, Link, Membership, Organization, PrdLinkMessage, Project
from app.errors import NotFound, Refused
from app.services import links as L
from app.services import prds as prds_svc
from app.services import suggestions as S
from app.services import threads as T
from app.services.links import Caller


@pytest.fixture()
def world(client):
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        db.add_all([Organization(id="o1", name="O1"), Organization(id="o2", name="O2")])
        db.flush()
        db.add_all([Project(id="pa", name="A", tag="PAA", org_id="o1"),
                    Project(id="pb", name="B", tag="PBB", org_id="o1"),
                    Project(id="px", name="X", tag="PXX", org_id="o2")])
        db.flush()
        a = prds_svc.create_prd(db, title="A one", project_id="pa", body="SECRET A body")
        b = prds_svc.create_prd(db, title="B two", project_id="pb", body="B body")
        x = prds_svc.create_prd(db, title="X", project_id="px")
        yield db, a, b, x
    finally:
        db.close()


def _needs_person():
    return pytest.raises(Refused, match="needs_person")


def _a(**kw):
    return Caller(readable=["pa"], writable=["pa"], is_agent=True, label="agent-a", owner="Ann", **kw)


def _b(agent=False):
    return Caller(readable=["pb"], writable=["pb"], is_agent=agent, label="agent-b", owner="Bob")


def test_request_accept_one_thread(world):
    db, a, b, x = world
    link = L.request_prd_link(db, _a(), a.id, b.id, reason="need console contract")
    assert link.status == "pending"
    # Re-request, from either side, returns the same link.
    assert L.request_prd_link(db, _a(), a.id, b.id, reason="again").id == link.id
    # Default setting: an agent's accept needs a person; the link stays pending.
    with _needs_person():
        L.accept_prd_link(db, _b(agent=True), link.id)
    db.refresh(link)
    assert link.status == "pending"
    assert L.accept_prd_link(db, _b(), link.id).status == "accepted"
    assert L.request_prd_link(db, _a(), a.id, b.id, reason="x").id == link.id


def test_org_setting_lets_agent_accept(world):
    db, a, b, x = world
    db.get(Organization, "o1").prd_link_accept_agents = True
    db.commit()
    link = L.request_prd_link(db, _a(), a.id, b.id, reason="r")
    assert L.accept_prd_link(db, _b(agent=True), link.id).status == "accepted"


def test_cross_org_and_missing_get_the_same_refusal(world):
    db, a, b, x = world
    with pytest.raises(Refused) as e1:
        L.request_prd_link(db, _a(), a.id, x.id, reason="r")
    with pytest.raises(Refused) as e2:
        L.request_prd_link(db, _a(), a.id, "nope", reason="r")
    assert e1.value.status_code == e2.value.status_code == 403
    assert e1.value.rule_id == e2.value.rule_id == L.PRD_LINK_ORG_RULE
    assert e1.value.detail.replace(repr(x.id), "?") == e2.value.detail.replace("'nope'", "?")


def test_b_only_key_reads_and_posts_without_a_body(world):
    db, a, b, x = world
    link = L.request_prd_link(db, _a(), a.id, b.id, reason="r")
    L.accept_prd_link(db, _b(), link.id)
    item = Item(id="it1", project_id="pa", number=1, title="t", prd_id=a.id, status="next")
    db.add(item)
    db.commit()
    m = T.post(db, _b(True), link.id, kind="question", body="why?", cites=[item.id])
    r = T.post(db, _a(), link.id, kind="note", body="because", reply_to=m["id"])
    out = T.read_thread(db, _b(True), link.id)
    assert [x["id"] for x in out["messages"]] == [m["id"], r["id"]]
    assert out["messages"][0]["author"] == {"kind": "agent", "label": "agent-b", "owner": "Bob"}
    assert out["link"]["from"] == {"id": a.key, "title": "A one", "status": "draft"}
    assert "SECRET" not in repr(out) and "email" not in repr(out)
    with pytest.raises(ValueError):
        T.post(db, _b(), link.id, kind="note", body="x", cites=["nonexistent"])


def test_unlink_makes_thread_read_only_and_keeps_it(world):
    db, a, b, x = world
    link = L.request_prd_link(db, _a(), a.id, b.id, reason="r")
    L.accept_prd_link(db, _b(), link.id)
    T.post(db, _b(), link.id, kind="note", body="hi")
    with _needs_person():
        L.unlink_prd_link(db, _a(), link.id)
    L.unlink_prd_link(db, Caller(readable=["pa"], writable=["pa"], label="Ann"), link.id)
    with pytest.raises(ValueError):
        T.post(db, _b(), link.id, kind="note", body="more")
    assert len(T.read_thread(db, _b(), link.id)["messages"]) == 1
    # Re-requesting revives the same link and thread.
    assert L.request_prd_link(db, _a(), a.id, b.id, reason="back").id == link.id


def test_decline_needs_reason_and_outsider_sees_nothing(world):
    db, a, b, x = world
    link = L.request_prd_link(db, _a(), a.id, b.id, reason="r")
    with pytest.raises(ValueError):
        L.decline_prd_link(db, _b(), link.id, reason=" ")
    assert L.decline_prd_link(db, _b(), link.id, reason="no").status == "declined"
    with pytest.raises(NotFound) as hidden:
        T.read_thread(db, Caller(readable=["px"], writable=[]), link.id)
    with pytest.raises(NotFound) as missing:
        T.read_thread(db, Caller(readable=["px"], writable=[]), "plk_nope")
    assert str(hidden.value).replace(repr(link.id), "?") == str(missing.value).replace("'plk_nope'", "?")


def test_far_side_is_id_only_until_accepted_and_after_unlink(world):
    db, a, b, x = world
    link = L.request_prd_link(db, _a(), a.id, b.id, reason="r")
    # Pending: the requester names B's PRD but sees only its id.
    assert T.read_thread(db, _a(), link.id)["link"]["to"] == {"id": b.key}
    assert L.render_prd_link(db, _b(), link)["from"] == {"id": a.key}
    # Each caller still sees its own side in full.
    assert T.read_thread(db, _a(), link.id)["link"]["from"]["title"] == "A one"
    assert "B two" not in repr(L.render_prd_link(db, _a(), L.list_prd_links(db, _a())[0]))
    L.accept_prd_link(db, _b(), link.id)
    assert T.read_thread(db, _a(), link.id)["link"]["to"] == {
        "id": b.key, "title": "B two", "status": "draft"}
    L.unlink_prd_link(db, Caller(readable=["pa"], writable=["pa"], label="Ann"), link.id)
    assert T.read_thread(db, _a(), link.id)["link"]["to"] == {"id": b.key}


def test_declined_link_does_not_reveal_far_side(world):
    db, a, b, x = world
    link = L.request_prd_link(db, _a(), a.id, b.id, reason="r")
    L.decline_prd_link(db, _b(), link.id, reason="no")
    out = T.read_thread(db, _a(), link.id)["link"]
    assert out["to"] == {"id": b.key} and "B two" not in repr(out)


def test_link_traffic_is_a_directive_to_agents_working_the_prd(world):
    from app.models import Agent
    from app.services import fleet as fleet_svc

    db, a, b, x = world
    it = Item(id="itb", project_id="pb", number=1, title="t", status="in_progress", claimed_by="ag-b",
              prd_id=b.id)
    db.add(it)
    db.add(Agent(id="ag-b", project_id="pb", number=1))
    db.commit()
    link = L.request_prd_link(db, _a(), a.id, b.id, reason="need it")
    d = fleet_svc.collect_directive(db, "ag-b")
    assert d["type"] == "prd_link_pending" and d["items"][0]["type"] == "prd_link_request"
    assert fleet_svc.collect_directive(db, "ag-b") is None  # read; not told twice
    L.accept_prd_link(db, _b(), link.id)
    T.post(db, _a(), link.id, kind="note", body="hello B")
    d = fleet_svc.collect_directive(db, "ag-b")
    assert d["items"][0]["body"] == "hello B"
    from app.services import link_delivery
    # A's own post is never delivered back to A.
    assert link_delivery.pending_for_prd(db, a.id) == []
    T.post(db, _b(), link.id, kind="note", body="hello A")
    # Nobody working A's PRD: B's message stays pending there.
    pend = link_delivery.pending_for_prd(db, a.id)
    assert [e["body"] for e in pend if e["type"] == "prd_link_message"] == ["hello A"]
    # ...and B's own post is not a directive for B's agent.
    assert fleet_svc.collect_directive(db, "ag-b") is None


def test_author_prd_gets_no_directive_for_its_own_post(world):
    from app.models import Agent
    from app.services import fleet as fleet_svc

    db, a, b, x = world
    db.add(Item(id="ita", project_id="pa", number=1, title="t", status="in_progress", claimed_by="ag-a",
                prd_id=a.id))
    db.add(Agent(id="ag-a", project_id="pa", number=1))
    db.commit()
    link = L.request_prd_link(db, _a(), a.id, b.id, reason="need it")
    L.accept_prd_link(db, _b(), link.id)
    T.post(db, _a(), link.id, kind="note", body="from A")
    assert fleet_svc.collect_directive(db, "ag-a") is None
    T.post(db, _b(), link.id, kind="note", body="from B")
    d = fleet_svc.collect_directive(db, "ag-a")
    assert [e["body"] for e in d["items"]] == ["from B"]


# ---- S2: suggested items ----------------------------------------------------------------


def _linked(world):
    db, a, b, x = world
    link = L.request_prd_link(db, _a(), a.id, b.id, reason="r")
    L.accept_prd_link(db, _b(), link.id)
    return db, a, b, link


def test_person_in_b_files_item_on_b_prd_and_outcome_posts_back(world):
    db, a, b, link = _linked(world)
    src = Item(id="it1", project_id="pa", number=1, title="t", prd_id=a.id, status="next")
    db.add(src)
    db.commit()
    m = S.suggest(db, _a(), link.id, {"title": "Console contract", "description": "d",
                                      "tags": ["x"], "link": {"item": "it1", "direction": "blocks"}})
    assert m["kind"] == "suggested_item" and m["outcome"] is None
    out = S.file_suggestion(db, _b(), link.id, m["id"])
    assert out["decided"] and out["outcome"]["outcome"] == "filed"
    item = db.scalar(select(Item).where(Item.title == "Console contract"))
    assert item.project_id == "pb" and item.prd_id == b.id
    assert out["outcome"]["item"] == item.id
    dep = db.scalar(select(Link).where(Link.a == "it1", Link.b == item.id))
    # The edge is stored with its source (it1, on A's side), as link_items stores one.
    assert dep is not None and dep.type == "dependency" and dep.project_id == "pa"
    thread = T.read_thread(db, _a(), link.id)["messages"]
    assert thread[-1]["reply_to"] == m["id"] and thread[-1]["body"].startswith("filed: filed")


def test_sender_key_cannot_file_and_agent_needs_person(world):
    db, a, b, link = _linked(world)
    m = S.suggest(db, _a(), link.id, {"title": "T"})
    with pytest.raises(Refused, match="not_allowed"):
        S.file_suggestion(db, _a(), link.id, m["id"])
    with _needs_person():
        S.file_suggestion(db, _b(agent=True), link.id, m["id"])
    assert db.get(PrdLinkMessage, m["id"]).outcome == ""
    db.get(Organization, "o1").prd_link_accept_agents = True
    db.commit()
    assert S.file_suggestion(db, _b(agent=True), link.id, m["id"])["outcome"]["outcome"] == "filed"


def test_decline_creates_nothing_and_first_decision_wins(world):
    db, a, b, link = _linked(world)
    m = S.suggest(db, _a(), link.id, {"title": "Nope"})
    with pytest.raises(ValueError):
        S.decline_suggestion(db, _b(), link.id, m["id"], reason=" ")
    out = S.decline_suggestion(db, _b(), link.id, m["id"], reason="out of scope")
    assert out["outcome"]["outcome"] == "declined" and out["outcome"]["code"] == "declined"
    again = S.file_suggestion(db, _b(), link.id, m["id"])
    assert again["code"] == "already_decided" and again["outcome"]["outcome"] == "declined"
    assert db.query(Item).filter(Item.title == "Nope").count() == 0


def test_validation_failure_carries_create_item_refusal(world):
    db, a, b, link = _linked(world)
    m = S.suggest(db, _a(), link.id, {"title": "Bad", "status": "bogus"})
    out = S.file_suggestion(db, _b(), link.id, m["id"])
    assert out["outcome"]["outcome"] == "conflict"
    assert out["outcome"]["code"] == "validation" and "invalid status" in out["outcome"]["message"]
    assert db.query(Item).filter(Item.title == "Bad").count() == 0


def test_unlinked_and_second_link_decision(world):
    db, a, b, link = _linked(world)
    m = S.suggest(db, _a(), link.id, {"title": "Late"})
    L.unlink_prd_link(db, Caller(readable=["pa"], writable=["pa"], label="Ann"), link.id)
    out = S.file_suggestion(db, _b(), link.id, m["id"])
    assert out["outcome"]["code"] == "unlinked" and db.query(Item).filter(Item.title == "Late").count() == 0
    with pytest.raises(Exception, match="already_decided"):
        L.accept_prd_link(db, _b(), link.id)


def test_suggestion_shape_and_link_scope(world):
    db, a, b, link = _linked(world)
    with pytest.raises(ValueError):
        S.suggest(db, _a(), link.id, {"description": "no title"})
    db.add(Item(id="bx", project_id="pb", number=1, title="b item", prd_id=b.id, status="next"))
    db.commit()
    with pytest.raises(ValueError, match="your own PRD"):
        S.suggest(db, _a(), link.id, {"title": "T", "link": {"item": "bx", "direction": "blocks"}})
