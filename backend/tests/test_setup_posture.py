"""GRPH-1447: the first account on a fresh self-host box may read the posture before owning a project."""
from app.config import settings
from app.db import SessionLocal
from app.models import User
from app.services import setup as setup_svc


def _user(db, n):
    u = User(id=f"usr-t{n}", name=f"U{n}", email=f"u{n}@example.com", handle=f"u{n}", password_hash="x")
    db.add(u)
    db.commit()
    return u


def test_first_account_without_a_project_is_admin_on_self_host(monkeypatch):
    monkeypatch.setattr(settings, "hosted_mode", False)
    with SessionLocal() as db:
        first = _user(db, 1)
        assert setup_svc.is_instance_admin(db, first) is True
        second = _user(db, 2)
        assert setup_svc.is_instance_admin(db, second) is False
        assert setup_svc.is_instance_admin(db, first) is False


def test_hosted_mode_is_not_loosened(monkeypatch):
    monkeypatch.setattr(settings, "hosted_mode", True)
    with SessionLocal() as db:
        assert setup_svc.is_instance_admin(db, _user(db, 1)) is False
