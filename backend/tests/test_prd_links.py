"""GRPH-P104 S1: PRD links request/accept, one thread per pair, metadata-only reads."""
import pytest

from app.models import Item, Membership, Organization, Project
from app.errors import NotFound, Refused
from app.services import links as L
from app.services import prds as prds_svc
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
