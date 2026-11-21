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
    "services/handles.py": 5,         # the helper itself and the box-uniqueness check
    "routers/auth.py": 9,             # signup, /me and the account-handle change
    "routers/orgs.py": 12,            # seat vs account-handle comparisons in serializers
    "services/orgs.py": 5,            # seat creation / rename on OrgMembership.handle
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
