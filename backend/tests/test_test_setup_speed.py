"""The two production knobs the suite's setup speed rests on (2026-11-01).

Both exist so the test suite does not pay production costs on every test, and both must keep
production exactly where it was: bcrypt at 12, and a database that is not at head always
taking the full Alembic upgrade.
"""
import pytest
from pydantic import ValidationError

from app.config import Settings


def test_bcrypt_defaults_to_twelve_rounds_in_production(monkeypatch):
    monkeypatch.delenv("BCRYPT_ROUNDS", raising=False)
    assert Settings(_env_file=None).bcrypt_rounds == 12


@pytest.mark.parametrize("bad", [3, 32])
def test_bcrypt_rounds_outside_bcrypts_bounds_are_refused(monkeypatch, bad):
    monkeypatch.setenv("BCRYPT_ROUNDS", str(bad))
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_new_hashes_carry_the_configured_cost_and_still_verify(monkeypatch):
    from app.config import settings
    from app.security.passwords import hash_password, verify_password

    monkeypatch.setattr(settings, "bcrypt_rounds", 5)
    h = hash_password("correct horse")
    assert h.startswith("$2b$05$")
    assert verify_password("correct horse", h)
    assert not verify_password("wrong", h)


def test_a_hash_written_at_twelve_still_verifies_after_the_cost_drops(monkeypatch):
    """The cost lives in the stored hash, so lowering the setting never invalidates a login."""
    from app.config import settings
    from app.security.passwords import hash_password, verify_password

    monkeypatch.setattr(settings, "bcrypt_rounds", 12)
    old = hash_password("pw-at-twelve")
    monkeypatch.setattr(settings, "bcrypt_rounds", 4)
    assert old.startswith("$2b$12$") and verify_password("pw-at-twelve", old)


def _spy_upgrade(monkeypatch):
    from app import migrate

    calls = []
    monkeypatch.setattr(migrate.command, "upgrade", lambda cfg, rev: calls.append(rev))
    monkeypatch.setattr("app.vector_schema.converge_all", lambda: None)
    return migrate, calls


def test_a_database_at_the_scripts_head_skips_the_upgrade(monkeypatch):
    migrate, calls = _spy_upgrade(monkeypatch)
    monkeypatch.setattr(migrate, "_script_heads", lambda: ("0170",))
    monkeypatch.setattr(migrate, "_database_heads", lambda: ("0170",))
    migrate.run_migrations()
    assert calls == []


@pytest.mark.parametrize("db_heads", [None, (), ("0169",), ("0170", "0171"), ("0171",)])
def test_anything_but_an_exact_head_match_runs_the_full_upgrade(monkeypatch, db_heads):
    """Behind, ahead, branched, empty or unreadable: the full upgrade is the authority."""
    migrate, calls = _spy_upgrade(monkeypatch)
    monkeypatch.setattr(migrate, "_script_heads", lambda: ("0170",))
    monkeypatch.setattr(migrate, "_database_heads", lambda: db_heads)
    migrate.run_migrations()
    assert calls == ["head"]


def test_the_script_heads_are_the_real_alembic_heads():
    from alembic.script import ScriptDirectory

    from app import migrate

    real = tuple(sorted(ScriptDirectory.from_config(migrate._config()).get_heads()))
    assert migrate._script_heads() == real and len(real) == 1
