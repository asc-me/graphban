"""GRPH-P104 S4: an accepted PRD link grants metadata-only visibility and cross-project deps."""
import pytest
from sqlalchemy import select

from app.models import Item, Link, Organization, Project
from app.security.authz import Forbidden
from app.services import items as items_svc
from app.services import links as L
from app.services import prds as prds_svc
from app.services.links import Caller
from tests.test_prd_links import world  # noqa: F401  (fixture)


def _a():
    return Caller(readable=["pa"], writable=["pa"], is_agent=True, label="a", owner="Ann")


def _b():
    return Caller(readable=["pb"], writable=["pb"], label="b", owner="Bob")


def _setup(db, a, b, accept=True):
    far = Item(id="far1", project_id="pa", number=1, title="A far", prd_id=a.id, status="next",
               description="SECRET far body")
    near = Item(id="near1", project_id="pb", number=1, title="B near", prd_id=b.id, status="next")
    db.add_all([far, near])
    db.commit()
    link = L.request_prd_link(db, _a(), a.id, b.id, reason="r")
    if accept:
        L.accept_prd_link(db, _b(), link.id)
    return far, near, link


def test_unlinked_pair_is_refused(world):
    db, a, b, x = world
    far, near, _ = _setup(db, a, b, accept=False)
    with pytest.raises(Forbidden):
        L.link_endpoints(db, near.id, far.id, readable=["pb"], writable=["pb"])


def test_accepted_link_renders_real_ref_and_unlink_hides_it(world):
    db, a, b, x = world
    far, near, link = _setup(db, a, b)
    L.link_endpoints(db, near.id, far.id, readable=["pb"], writable=["pb"])
    db.add(Link(project_id="pb", a=near.id, b=far.id, type="dependency"))
    db.commit()
    out = items_svc.get_item_details(db, near.id, ["pb"])
    assert out["links"][0]["b"] == far.key
    assert out["links"][0]["far"]["title"] == "A far" and out["links"][0]["far"]["status"] == "next"
    assert "SECRET" not in repr(out)
    # Readiness: blocked while the far item is open, ready once it is done (GRPH-1993).
    from app.services import prioritization as prio
    ctx = prio.context(db, "pb")
    assert not prio.ready(ctx, near)
    far.status = "done"
    db.commit()
    ctx = prio.context(db, "pb")
    assert prio.ready(ctx, near)
    far.status = "next"
    db.commit()
    # The far side sees the near item too.
    assert items_svc.render_ref(db, near.id, ["pa"]) == near.key
    # Unlink: still stored, hidden again.
    L.unlink_prd_link(db, _b(), link.id)
    out = items_svc.get_item_details(db, near.id, ["pb"])
    assert out["links"][0]["b"] == items_svc.HIDDEN_REF and "far" not in out["links"][0]
    assert db.scalar(select(Link).where(Link.a == near.id)) is not None
    # Unlinked, the hidden far dependency still blocks.
    assert not prio.ready(prio.context(db, "pb"), near)


def test_link_needs_write_on_the_dependent(world):
    db, a, b, x = world
    far, near, _ = _setup(db, a, b)
    with pytest.raises(Forbidden):
        L.link_endpoints(db, near.id, far.id, readable=["pb"], writable=[])
