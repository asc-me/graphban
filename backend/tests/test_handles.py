"""GRPH-1787 (P93 S1): org-scoped handles, holds and the user dot."""
import importlib.util
import pathlib
from datetime import timedelta

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.models import HandleHold, Organization, OrgMembership, User, utcnow
from app.services import handles

_MIGRATION = pathlib.Path(__file__).parents[1] / "alembic" / "versions" / "0222_org_scoped_handles.py"


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


def _user(db, n, handle="alex"):
    u = User(id=f"u_{n}", name=n, handle=handle, email=f"{n}@x.test", password_hash="x")
    db.add(u)
    db.flush()
    return u


def _org(db, n):
    o = Organization(id=f"org_{n}", name=n)
    db.add(o)
    db.flush()
    return o


def test_same_handle_in_two_orgs_but_not_twice_in_one(db):
    a, b, c = _user(db, "a", "al-a"), _user(db, "b", "al-b"), _user(db, "c", "al-c")
    o1, o2 = _org(db, "1"), _org(db, "2")
    db.add_all([OrgMembership(org_id=o1.id, user_id=a.id, handle="alex"),
                OrgMembership(org_id=o2.id, user_id=b.id, handle="alex")])
    db.flush()
    db.add(OrgMembership(org_id=o1.id, user_id=c.id, handle="alex"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_membership_handle_defaults_from_the_account_handle(db):
    u, o = _user(db, "d", "dee"), _org(db, "d")
    m = OrgMembership(org_id=o.id, user_id=u.id)
    db.add(m)
    db.flush()
    assert m.handle == "dee"
    assert handles.org_handle(db, u, o.id) == "dee"
    assert handles.org_handle(db, u, "org_none") == "dee"


def test_check_free_reads_only_the_given_orgs(db):
    u = _user(db, "e", "eve")
    o1, o2 = _org(db, "e1"), _org(db, "e2")
    db.add(OrgMembership(org_id=o2.id, user_id=u.id, handle="zed"))
    db.flush()
    assert handles.check_free(db, "zed", [o1.id]) is None
    assert handles.check_free(db, "zed", [o1.id, o2.id]) == o2.id
    assert handles.check_free(db, "zed", [o2.id], exclude_user=u.id) is None
    assert handles.check_free(db, "zed", []) is None


def test_in_use_and_held_are_indistinguishable_and_holds_expire(db):
    u = _user(db, "f", "fay")
    o1, o2 = _org(db, "f1"), _org(db, "f2")
    db.add(OrgMembership(org_id=o1.id, user_id=u.id, handle="used"))
    db.add(HandleHold(org_id=o2.id, handle="held", user_id=u.id,
                      expires_at=utcnow() + timedelta(days=29)))
    db.add(HandleHold(org_id=o2.id, handle="old", user_id=u.id,
                      expires_at=utcnow() - timedelta(seconds=1)))
    db.flush()
    assert handles.check_free(db, "used", [o1.id]) == o1.id
    assert handles.check_free(db, "held", [o2.id]) == o2.id
    assert handles.check_free(db, "old", [o2.id]) is None


def test_write_holds_is_thirty_days_and_cloud_only(db, monkeypatch):
    u = _user(db, "g", "gus")
    o = _org(db, "g")
    monkeypatch.setattr(settings, "hosted_mode", False)
    assert handles.write_holds(db, u, "gus", [o.id]) == []
    monkeypatch.setattr(settings, "hosted_mode", True)
    (hold,) = handles.write_holds(db, u, "gus", [o.id])
    exp = hold.expires_at if hold.expires_at.tzinfo else hold.expires_at.replace(tzinfo=utcnow().tzinfo)
    assert timedelta(days=29, hours=23) < exp - utcnow() <= timedelta(days=30)
    assert handles.check_free(db, "gus", [o.id], exclude_user="other") == o.id


@pytest.mark.parametrize("h", ["admin", "root", "support", "graphban"])
def test_reserved_words(h):
    with pytest.raises(ValueError, match="reserved"):
        handles.validate(h)


@pytest.mark.parametrize("h", ["alice_new", "ab", "-abc", "abc-", "a--bc", "Abcd", "a" * 25, ""])
def test_invalid_handles(h):
    with pytest.raises(ValueError, match="invalid"):
        handles.validate(h)


@pytest.mark.parametrize("h", ["abc", "a-b-c", "a1" * 12, "alex"])
def test_valid_handles(h):
    assert handles.validate(h) == h


def test_dot_stores_equal_second_colour_as_null_and_refuses_bad_pattern(db):
    u = _user(db, "h", "hal")
    handles.apply_dot(u, {"avatar": "#112233", "avatar2": "#112233", "avatar_pattern": "stripes"})
    assert (u.avatar2, u.avatar_pattern) == (None, "stripes")
    handles.apply_dot(u, {"avatar2": "#AABBCC"})
    assert u.avatar2 == "#aabbcc"
    with pytest.raises(ValueError):
        handles.apply_dot(u, {"avatar_pattern": "plaid"})


def test_box_handle_taken(db):
    u = _user(db, "i", "ida")
    assert handles.box_handle_taken(db, "ida")
    assert not handles.box_handle_taken(db, "ida", exclude_user=u.id)


def test_migration_backfills_only_the_membership_handle_and_round_trips():
    eng = create_engine("sqlite://")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE users (id VARCHAR PRIMARY KEY, handle VARCHAR, avatar VARCHAR)"))
        c.execute(text("CREATE TABLE organizations (id VARCHAR PRIMARY KEY)"))
        c.execute(text("CREATE TABLE org_memberships (id INTEGER PRIMARY KEY, org_id VARCHAR, user_id VARCHAR, role VARCHAR)"))
        c.execute(text("INSERT INTO users VALUES ('u1','alex','#fff000')"))
        c.execute(text("INSERT INTO organizations VALUES ('o1')"))
        c.execute(text("INSERT INTO org_memberships VALUES (1,'o1','u1','owner')"))
    spec = importlib.util.spec_from_file_location("mig_0222", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def run(fn):
        with eng.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                fn()

    run(mod.upgrade)
    with eng.connect() as c:
        assert c.execute(text("SELECT org_id, user_id, role, handle FROM org_memberships")).one() == (
            "o1", "u1", "owner", "alex")
        assert c.execute(text("SELECT handle, avatar, avatar2, avatar_pattern FROM users")).one() == (
            "alex", "#fff000", None, "none")
    with pytest.raises(IntegrityError), eng.begin() as c:
        c.execute(text("INSERT INTO org_memberships (org_id, user_id, role, handle) VALUES ('o1','u2','m','alex')"))
    run(mod.downgrade)
    with eng.connect() as c:
        assert c.execute(text("SELECT org_id, user_id, role FROM org_memberships")).one() == ("o1", "u1", "owner")


@pytest.mark.skipif(
    __import__("app.db", fromlist=["engine"]).engine.url.drivername.startswith("sqlite"),
    reason="Alembic owns the schema on Postgres only")
def test_postgres_downgrade_and_upgrade_round_trip_the_handle_schema(client):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    from app.db import engine
    from app.migrate import _BACKEND_ROOT

    cfg = Config(str(_BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))

    def uniques(table):
        return {c["name"] for c in inspect(engine).get_unique_constraints(table)}

    assert "uq_org_membership_handle" in uniques("org_memberships")
    assert "users_handle_key" not in uniques("users")
    assert inspect(engine).has_table("handle_holds")

    command.downgrade(cfg, "0221")
    assert "handle" not in {c["name"] for c in inspect(engine).get_columns("org_memberships")}
    assert {"avatar2", "avatar_pattern"}.isdisjoint(
        {c["name"] for c in inspect(engine).get_columns("users")})
    assert "users_handle_key" in uniques("users")
    assert not inspect(engine).has_table("handle_holds")

    command.upgrade(cfg, "head")
    assert "uq_org_membership_handle" in uniques("org_memberships")
    assert "users_handle_key" not in uniques("users")
    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT count(*) FROM org_memberships m JOIN users u ON u.id = m.user_id "
            "WHERE m.handle <> u.handle")).scalar() == 0
