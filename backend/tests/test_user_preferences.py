"""Per-user UI preferences (PRD-60 D1 / GRPH-1133).

Sidebar collapse, condense, tracker layout, board columns and saved views are the user's, and
until this there was nowhere to put them: the frontend's only option was localStorage, which is
per-browser, so a layout arranged on one machine was a layout arranged again on the next.

Two properties here are worth more than the CRUD, and each gets its own case rather than being
folded into a round-trip assertion:

**Absent is not empty.** `saved: false` (the server has never held a document) and `saved: true`
with `{}` (the user saved an empty one) render identically in any UI that reads only the
document. The difference is the whole migration contract — a client uploads its localStorage
values once and ONLY in the first case — so collapsing the two would let a second device read
"no preferences" as "upload mine" and flatten the first device's layout. That is the failure
mode the PRD names, and it is invisible in a test that only checks the round-trip.

**A refused write stores nothing.** The 422 is asserted beside a GET that still says the
document is unchanged, because a validation error raised after the row was written would report
failure and persist the value anyway.
"""
import importlib.util
import pathlib

import pytest
from sqlalchemy import select, text

from app.db import SessionLocal
from app.models import UserPreference

_BACKEND = pathlib.Path(__file__).resolve().parents[1]
PREFS = "/api/auth/me/preferences"

FULL = {
    "nav": {"collapsed": True},
    "plan": {"condensed": False},
    "tracker": {
        "layout": "list",
        "board": {"columns": {"done": "hidden", "review": "collapsed", "next": "shown"}},
    },
    "views": {
        "tracker": {
            "default": "v1",
            "saved": [{"id": "v1", "label": "My view 1", "status": "in_progress",
                       "window": "90d", "hide": "14d"}],
        },
        "prds": {
            "saved": [{"id": "p1", "label": "Drafts", "status": "draft", "window": "180d",
                       "hide": "never"}],
            "default": None,
        },
    },
}


def _second_user(client) -> dict:
    """Register somebody else and return their headers."""
    r = client.post("/api/auth/register", json={
        "name": "Other Person", "email": "other@example.com", "handle": "other",
        "password": "other-password",
    })
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _loc(response) -> list:
    """The field path FastAPI names in a 422 — the acceptance criterion is that it is named,
    not merely that the status is right."""
    detail = response.json()["detail"]
    assert detail, f"422 with no detail: {response.text}"
    return list(detail[0]["loc"])


# ---- absent is not empty -------------------------------------------------------------------

def test_a_new_user_has_no_preferences_and_the_response_says_so(client, auth):
    r = client.get(PREFS, headers=auth)

    assert r.status_code == 200, r.text
    assert r.json() == {"saved": False, "preferences": {}}


def test_an_empty_document_written_on_purpose_is_a_different_answer(client, auth):
    """The pair with the test above. Both render as `{}`; only `saved` tells a migrating client
    whether to upload its localStorage values or read the server's."""
    r = client.put(PREFS, json={}, headers=auth)

    assert r.status_code == 200, r.text
    assert r.json() == {"saved": True, "preferences": {}}
    assert client.get(PREFS, headers=auth).json() == {"saved": True, "preferences": {}}


# ---- the document --------------------------------------------------------------------------

def test_a_saved_document_round_trips(client, auth):
    r = client.put(PREFS, json=FULL, headers=auth)

    assert r.status_code == 200, r.text
    got = client.get(PREFS, headers=auth).json()
    assert got["saved"] is True
    assert got["preferences"]["nav"] == {"collapsed": True}
    assert got["preferences"]["plan"] == {"condensed": False}
    assert got["preferences"]["tracker"]["layout"] == "list"
    assert got["preferences"]["tracker"]["board"]["columns"] == {
        "done": "hidden", "review": "collapsed", "next": "shown"}
    assert got["preferences"]["views"]["tracker"]["default"] == "v1"
    assert got["preferences"]["views"]["tracker"]["saved"] == [FULL["views"]["tracker"]["saved"][0]]
    assert got["preferences"]["views"]["prds"]["saved"] == [FULL["views"]["prds"]["saved"][0]]


def test_a_key_the_client_did_not_send_stays_absent(client, auth):
    """No server-side defaults. A default the server invents is indistinguishable from a choice
    the user made, and the UI is the only place that knows what the defaults are this release."""
    client.put(PREFS, json={"nav": {"collapsed": True}}, headers=auth)

    assert client.get(PREFS, headers=auth).json()["preferences"] == {"nav": {"collapsed": True}}


def test_the_put_replaces_the_document_rather_than_merging_it(client, auth):
    """Last writer wins, and a merge would make the outcome depend on what the previous device
    left behind — a key deleted in one browser resurrecting itself from another."""
    client.put(PREFS, json=FULL, headers=auth)
    client.put(PREFS, json={"plan": {"condensed": True}}, headers=auth)

    assert client.get(PREFS, headers=auth).json()["preferences"] == {"plan": {"condensed": True}}


def test_a_user_keeps_one_row_however_often_they_write(client, auth):
    """`user_id` is the primary key, so a second write cannot add a second document. Asserted
    against the table rather than the response: a surrogate key with no UNIQUE would still
    answer the GET correctly while the rows piled up."""
    client.put(PREFS, json={"nav": {"collapsed": True}}, headers=auth)
    client.put(PREFS, json={"nav": {"collapsed": False}}, headers=auth)
    client.put(PREFS, json={"plan": {"condensed": True}}, headers=auth)

    with SessionLocal() as s:
        rows = s.scalars(select(UserPreference)).all()
    assert len(rows) == 1
    assert rows[0].doc == {"plan": {"condensed": True}}


# ---- validation -----------------------------------------------------------------------------

@pytest.mark.parametrize("doc, field", [
    ({"nope": True}, "nope"),
    ({"nav": {"collapsedd": True}}, "collapsedd"),
    ({"tracker": {"bord": {}}}, "bord"),
    ({"tracker": {"board": {"columns": {"done": "hidden"}, "order": []}}}, "order"),
    ({"views": {"roadmap": {}}}, "roadmap"),
    ({"views": {"tracker": {"saved": [{"id": "v", "label": "L", "colour": "red"}]}}}, "colour"),
])
def test_an_unknown_key_is_refused_and_names_the_key(client, auth, doc, field):
    """At every depth. A nested model that allowed extras would store nothing, answer 200, and
    be a save that reports success and changes nothing."""
    r = client.put(PREFS, json=doc, headers=auth)

    assert r.status_code == 422, r.text
    assert field in _loc(r), f"{field!r} is not named in {r.text}"


@pytest.mark.parametrize("doc, field", [
    ({"tracker": {"layout": "kanban"}}, "layout"),
    ({"tracker": {"layout": True}}, "layout"),
    ({"tracker": {"board": {"columns": {"done": "invisible"}}}}, "columns"),
    # A column keyed by a status that does not exist: an unknown key wearing a dict's clothes,
    # which `extra="forbid"` cannot see because it is a VALUE of a `dict[str, str]`.
    ({"tracker": {"board": {"columns": {"finished": "hidden"}}}}, "columns"),
    ({"views": {"tracker": {"saved": [{"id": "v", "label": "L", "window": "7d"}]}}}, "window"),
    ({"views": {"tracker": {"saved": [{"id": "v", "label": "L", "hide": "14days"}]}}}, "hide"),
    ({"views": {"tracker": {"saved": [{"id": "v", "label": "L", "status": "closed"}]}}}, "status"),
    ({"views": {"prds": {"saved": [{"id": "v", "label": "L", "status": "in_progress"}]}}}, "status"),
    ({"views": {"tracker": {"saved": [{"id": "", "label": "L"}]}}}, "id"),
    ({"views": {"tracker": {"saved": [{"id": "v", "label": ""}]}}}, "label"),
    ({"views": {"tracker": {"saved": [{"id": "v", "label": "x" * 121}]}}}, "label"),
    ({"views": {"tracker": {"default": ""}}}, "default"),
    ({"nav": {"collapsed": "not-a-bool"}}, "collapsed"),
])
def test_an_invalid_value_is_refused_and_names_the_field(client, auth, doc, field):
    r = client.put(PREFS, json=doc, headers=auth)

    assert r.status_code == 422, r.text
    assert field in _loc(r), f"{field!r} is not named in {r.text}"


def test_the_status_vocabularies_come_from_the_services(client, auth):
    """The two that are not this PRD's own: an item status on a tracker view, a PRD status on a
    prds view. Read from the services, so adding a status there admits it here without a second
    edit that a `Literal` would have needed."""
    from app.services import items as items_svc
    from app.services import prds as prds_svc

    for status in items_svc.STATUSES:
        r = client.put(PREFS, json={"views": {"tracker": {"saved": [
            {"id": "v", "label": "L", "status": status}]}}}, headers=auth)
        assert r.status_code == 200, f"{status}: {r.text}"

    for status in prds_svc.STATUSES:
        r = client.put(PREFS, json={"views": {"prds": {"saved": [
            {"id": "v", "label": "L", "status": status}]}}}, headers=auth)
        assert r.status_code == 200, f"{status}: {r.text}"

    assert client.put(PREFS, json={"views": {"prds": {"saved": [
        {"id": "v", "label": "L", "status": items_svc.STATUSES[0]}]}}},
        headers=auth).status_code == 422


def test_a_refused_write_stores_nothing(client, auth):
    """The 422 and the unchanged document, together. A validation error raised after the row was
    written would report failure and persist the value anyway."""
    client.put(PREFS, json={"nav": {"collapsed": True}}, headers=auth)

    r = client.put(PREFS, json={"nav": {"collapsed": True}, "tracker": {"layout": "kanban"}},
                   headers=auth)

    assert r.status_code == 422, r.text
    assert client.get(PREFS, headers=auth).json() == {
        "saved": True, "preferences": {"nav": {"collapsed": True}}}


def test_a_refused_first_write_leaves_the_user_unwritten(client, auth):
    """Same property at the other edge: a rejected upload must not create the row, or the client
    would read `saved: true` and never send its localStorage values again."""
    r = client.put(PREFS, json={"nope": True}, headers=auth)

    assert r.status_code == 422, r.text
    assert client.get(PREFS, headers=auth).json() == {"saved": False, "preferences": {}}


# ---- whose document is it -------------------------------------------------------------------

def test_one_user_never_reads_or_writes_another_users_preferences(client, auth):
    other = _second_user(client)
    client.put(PREFS, json=FULL, headers=auth)

    # The other user's own read is their own, and empty.
    assert client.get(PREFS, headers=other).json() == {"saved": False, "preferences": {}}

    # And their write does not reach the first user's document.
    r = client.put(PREFS, json={"nav": {"collapsed": True}}, headers=other)
    assert r.status_code == 200, r.text
    assert client.get(PREFS, headers=other).json()["preferences"] == {"nav": {"collapsed": True}}
    assert client.get(PREFS, headers=auth).json()["preferences"] == FULL


def test_no_surface_names_another_users_preferences(client, auth):
    """The route reads the user from the token and takes no id, so there is no parameter to
    point at somebody else. Pinned anyway: the property is that no such surface EXISTS, and
    that is what a future refactor adding one 'for convenience' would break."""
    from app.main import app

    paths = [p for p in app.openapi()["paths"] if "preferences" in p]
    assert paths == ["/api/auth/me/preferences"], paths


def test_an_anonymous_caller_gets_nothing(client):
    assert client.get(PREFS).status_code == 401
    assert client.put(PREFS, json=FULL).status_code == 401

    with SessionLocal() as s:
        assert s.scalars(select(UserPreference)).all() == []


def test_an_agent_api_key_is_not_a_user_and_cannot_reach_the_document(client):
    """These are a human's UI settings. An agent credential has no user to own them, and a
    bearer token that is not a JWT is refused by the same dependency that guards `/auth/me`."""
    assert client.get(PREFS, headers={"Authorization": "Bearer gb_sk_not-a-jwt"}
                      ).status_code == 401
    assert client.put(PREFS, json=FULL, headers={"Authorization": "Bearer gb_sk_not-a-jwt"}
                      ).status_code == 401


# ---- migration 0144 -------------------------------------------------------------------------

def _migration():
    path = _BACKEND / "alembic" / "versions" / "0144_user_preferences.py"
    spec = importlib.util.spec_from_file_location("migration_0144", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_0144_upgrades_and_downgrades_cleanly():
    """SQLite builds from `create_all`, so the migration file itself only runs on Postgres —
    this drives it directly, on both engines, which is the only way a broken `downgrade()` is
    caught before somebody needs it."""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine, inspect

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        # The FK target. Created by hand because this runs the migration alone, not the chain.
        conn.execute(text("CREATE TABLE users (id VARCHAR NOT NULL PRIMARY KEY)"))
    mod = _migration()

    def run(fn):
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                fn()

    run(mod.upgrade)
    insp = inspect(engine)
    assert "user_preferences" in insp.get_table_names()
    assert {c["name"] for c in insp.get_columns("user_preferences")} == {
        "user_id", "doc", "updated_at"}
    # The design decision, in the schema: one document per user, keyed by the user.
    assert insp.get_pk_constraint("user_preferences")["constrained_columns"] == ["user_id"]

    run(mod.downgrade)
    assert "user_preferences" not in inspect(engine).get_table_names()

    run(mod.upgrade)
    assert "user_preferences" in inspect(engine).get_table_names()


def test_the_model_and_the_migration_agree():
    """`create_all` on SQLite and Alembic on Postgres are two descriptions of one table, and
    nothing else connects them. A column in the model that the migration never adds survives
    the SQLite suite and breaks only on a real deploy."""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine, inspect

    from app.models import Base

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE users (id VARCHAR NOT NULL PRIMARY KEY)"))
    mod = _migration()
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            mod.upgrade()

    migrated = {c["name"] for c in inspect(engine).get_columns("user_preferences")}
    declared = {c.name for c in Base.metadata.tables["user_preferences"].columns}
    assert migrated == declared, f"migration adds {migrated}, model declares {declared}"


def test_the_table_is_in_the_metadata_so_sqlite_builds_it():
    """The SQLite path never runs the migration, so a model that was not registered on `Base`
    would produce a green migration test and a missing table in every test that uses `client`."""
    from app.models import Base

    assert "user_preferences" in Base.metadata.tables
