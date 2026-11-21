"""Ratchet (GRPH-1790, P93 S4): live labels read a handle through `services/handles`.

A direct `.handle` attribute read outside the allowlist fails. The allowlist is a per-file
count, so a new read in an allowed file fails too. Lower a number when a read goes away;
never raise one without a reason written next to it.
"""
import ast
import pathlib

APP = pathlib.Path(__file__).resolve().parents[1] / "app"

ALLOWED = {
    "models/__init__.py": 3,          # the column, and the membership default
    "services/handles.py": 8,         # the helper itself and the box-uniqueness check
    "routers/auth.py": 9,             # signup, /me and the account-handle change
    "routers/orgs.py": 12,            # seat vs account-handle comparisons in serializers
    "services/orgs.py": 6,            # seat creation / rename; the platform invite (no org yet)
    "routers/admin.py": 2,            # members list (seat column) and the account serializer
    # Outside this item's scope (rule decisions, sync payload, spec person label).
    "routers/rules.py": 1,
    "routers/specs.py": 1,
    "services/custom_rules.py": 1,
    "services/project_sync_payload.py": 1,
    "services/rule_proposals.py": 4,
}


def _reads() -> dict[str, int]:
    found: dict[str, int] = {}
    for path in APP.rglob("*.py"):
        n = sum(isinstance(x, ast.Attribute) and x.attr == "handle"
                for x in ast.walk(ast.parse(path.read_text())))
        if n:
            found[path.relative_to(APP).as_posix()] = n
    return found


def test_no_new_direct_handle_reads():
    over = {f: (n, ALLOWED.get(f, 0)) for f, n in _reads().items() if n > ALLOWED.get(f, 0)}
    assert not over, (
        "direct `.handle` read outside the allowlist; use handles.org_handle / "
        f"handles.project_handle: {over}")


def test_org_only_handle_labels_live_rows_and_not_stored_history(client):
    from app.db import SessionLocal
    from app.models import Event, Organization, OrgMembership, Project, User
    from app.services import events, handles

    db = SessionLocal()
    try:
        u = User(id="u_rat", name="", handle="alexcain", email="rat@x.test", password_hash="x")
        db.add_all([u, Organization(id="org_rat", name="B"),
                    Project(id="p_rat", name="P", tag="PR", org_id="org_rat"),
                    Project(id="p_free", name="F", tag="FR")])
        db.flush()
        db.add(OrgMembership(org_id="org_rat", user_id=u.id, role="member", handle="acain"))
        db.flush()
        assert handles.project_handle(db, u, "p_rat") == "acain"
        assert handles.project_handle(db, u, "p_free") == "alexcain"
        assert handles.project_handle(db, u, None) == "alexcain"
        events.record_user(db, u, action="x", project_id="p_rat")
        events.record_user(db, u, action="y", project_id="p_free")
        rows = {e.action: e.actor_label for e in db.query(Event).filter(Event.actor_id == u.id)}
        assert rows == {"x": "acain", "y": "alexcain"}
        # A later rename leaves the stored row alone.
        db.query(OrgMembership).filter_by(user_id=u.id).update({"handle": "other"})
        assert db.query(Event).filter_by(action="x").one().actor_label == "acain"
    finally:
        db.rollback()
        db.close()


def _org_seat(db, *, user_id: str, org_id: str, project_id: str, handle: str):
    """Put `project_id` in org `org_id` and give `user_id` the seat handle `handle` there."""
    from app.models import Organization, OrgMembership, Project
    if db.get(Organization, org_id) is None:
        db.add(Organization(id=org_id, name="B"))
        db.flush()
    db.get(Project, project_id).org_id = org_id
    db.add(OrgMembership(org_id=org_id, user_id=user_id, role="owner", handle=handle))
    db.commit()


def test_admin_org_listing_shows_the_org_handle(client):
    """Acceptance 1, admin: org B's members list and owner read the seat handle."""
    from app.db import SessionLocal
    from app.models import Organization, OrgMembership, User
    from app.routers import admin

    db = SessionLocal()
    try:
        u = User(id="u_adm", name="", handle="alexcain", email="adm@example.com", password_hash="x")
        db.add(u)
        db.flush()
        db.add(Organization(id="org_adm", name="B", created_by="u_adm"))
        db.flush()
        db.add(OrgMembership(org_id="org_adm", user_id=u.id, role="owner", handle="acain"))
        db.commit()
        row = next(o for o in admin.list_orgs(db) if o.id == "org_adm")
        assert [m.handle for m in row.members] == ["acain"]
        assert row.owner_handle == "acain"
        assert row.owner_name == "acain"  # empty name falls back to the org handle
    finally:
        db.close()


def test_live_board_label_is_the_org_handle_and_account_handle_elsewhere(client):
    """Acceptance 1, live view: `_user` label reads the project's org handle; a project
    with no org reads the account handle. The user has no name, so the label is the handle."""
    from app.db import SessionLocal
    from app.models import ApiKey, Organization, OrgMembership, Project, User
    from app.services import fleet
    from app.services import live

    db = SessionLocal()
    try:
        u = User(id="u_lv", name="", handle="alexcain", email="lv@x.test", password_hash="x")
        db.add_all([u, Organization(id="org_lv", name="B"),
                    Project(id="p_lvb", name="P", tag="LB", org_id="org_lv"),
                    Project(id="p_lvf", name="F", tag="LF")])
        db.flush()
        db.add(OrgMembership(org_id="org_lv", user_id=u.id, role="member", handle="acain"))
        for pid in ("p_lvb", "p_lvf"):
            db.add(ApiKey(id=f"k_{pid}", user_id=u.id, project_id=pid, name="k",
                          prefix=f"gb_sk_{pid}", hashed_key="x", scopes=["read"], roles=[]))
        db.commit()
        for pid in ("p_lvb", "p_lvf"):
            fleet.register_agent(db, project_id=pid, api_key=db.get(ApiKey, f"k_{pid}"),
                                 label="w")

        def label(pid):
            return next(g["label"] for g in live.board(db, pid)["users"]
                        if g["user_id"] == "u_lv")

        assert label("p_lvb") == "acain"
        assert label("p_lvf") == "alexcain"
    finally:
        db.close()


def test_reporter_and_closer_keep_the_handle_they_were_written_with(client, auth):
    """Acceptance 2: an item reporter and an owner-close closer are written with the org
    handle, and a later seat rename does not rewrite them."""
    from app.db import SessionLocal
    from app.models import OrgMembership

    db = SessionLocal()
    try:
        _org_seat(db, user_id="u1", org_id="org_hist", project_id="core", handle="acain")
    finally:
        db.close()

    item = client.post("/api/items", json={"title": "history", "project_id": "core",
                                           "status": "review"}, headers=auth).json()
    assert item["reporter"]["handle"] == "acain"
    r = client.post(f"/api/items/{item['id']}/owner-close",
                    json={"commit": "abcdef1234567890aaaabbbbccccddddeeeeffff"}, headers=auth)
    assert r.status_code == 200, r.text

    db = SessionLocal()
    try:
        db.query(OrgMembership).filter_by(org_id="org_hist", user_id="u1").update(
            {"handle": "renamed"})
        db.commit()
    finally:
        db.close()

    got = client.get(f"/api/items/{item['id']}", headers=auth).json()
    assert got["reporter"]["handle"] == "acain"
    details = " ".join(p["detail"] for e in got["evidence"] if e.get("kind") == "attestation"
                       for p in e.get("predicates", []))
    assert "closed by acain" in details and "renamed" not in details
    # New writes after the rename carry the new handle.
    again = client.post("/api/items", json={"title": "after", "project_id": "core"},
                        headers=auth).json()
    assert again["reporter"]["handle"] == "renamed"
