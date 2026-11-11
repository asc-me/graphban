"""GRPH-1517: deployment link — start/poll relay of the console device-code API, the
key-link migration, and status with `legacy`."""
import httpx
import pytest
from sqlalchemy import text

from app.db import engine

_IS_SQLITE = engine.url.drivername.startswith("sqlite")
SECRET_KEY = "gb_sk_abcd1234secretsecret"
TOKEN = "tok_super_secret_value"


class Console:
    """Stands in for httpx.request against the console."""

    def __init__(self):
        self.calls = []
        self.poll_answer = {"status": "pending"}
        self.poll_exc = None
        self.poll_code = 200

    def __call__(self, method, url, **kw):
        self.calls.append((method, url, kw))
        req = httpx.Request(method, url)
        if url.endswith("/device/start"):
            return httpx.Response(200, request=req, json={
                "device_code": "dev-code-1", "user_code": "ABCD-EFGH",
                "verification_url": "https://console.example/device", "expires_at": "2030-01-01T00:00:00Z"})
        if self.poll_exc:
            raise self.poll_exc
        return httpx.Response(self.poll_code, request=req, json=self.poll_answer)

    @property
    def start_body(self):
        return next(kw["json"] for m, u, kw in self.calls if u.endswith("/device/start"))


@pytest.fixture()
def console(monkeypatch):
    c = Console()
    monkeypatch.setattr(httpx, "request", c)
    return c


def _start(client, auth):
    return client.post("/api/sync/link/start", json={"label": "box", "console_url": "console.example"},
                       headers=auth)


def _status(client, auth):
    return client.get("/api/sync/status", headers=auth)


def _assert_no_secrets(*responses):
    for r in responses:
        assert TOKEN not in r.text and SECRET_KEY not in r.text and "dev-code-1" not in r.text


def test_start_returns_code_and_omits_legacy_key_id_without_a_legacy_key(client, auth, console):
    r = _start(client, auth)
    assert r.status_code == 200
    assert r.json()["user_code"] == "ABCD-EFGH" and r.json()["verification_url"]
    assert "legacy_key_id" not in console.start_body
    assert "device_code" not in r.json()


def test_legacy_link_sends_only_the_public_prefix_then_approval_clears_it(client, auth, console):
    client.post("/api/sync/link", json={"cloud_url": "console.example", "api_key": SECRET_KEY}, headers=auth)
    from app.db import SessionLocal
    from app.models import SyncLink

    with SessionLocal() as db:
        db.get(SyncLink, "instance").legacy_key = "legacy"
        db.commit()
    assert _status(client, auth).json()["legacy"] is True
    s = _start(client, auth)
    assert console.start_body["legacy_key_id"] == "gb_sk_abcd"
    assert SECRET_KEY not in str(console.start_body)

    console.poll_answer = {"status": "approved", "token": TOKEN, "org": "acme", "email": "a@acme.dev"}
    p = client.get("/api/sync/link/poll", headers=auth)
    st = _status(client, auth)
    _assert_no_secrets(s, p, st)
    assert p.json() == {"status": "approved", "org": "acme", "email": "a@acme.dev"}
    assert st.json()["legacy"] is False and st.json()["org"] == "acme"
    assert st.json()["linked_email"] == "a@acme.dev"
    from app.security import secrets

    with SessionLocal() as db:
        link = db.get(SyncLink, "instance")
        assert link.token_enc and secrets.decrypt(link.token_enc) == TOKEN
        assert link.legacy_key == ""


@pytest.mark.parametrize("reason", ["declined", "expired", "not_member", "no_seat"])
def test_refused_stores_nothing(client, auth, console, reason):
    _start(client, auth)
    console.poll_answer = {"status": "refused", "reason": reason}
    p = client.get("/api/sync/link/poll", headers=auth)
    assert p.json() == {"status": "refused", "reason": reason}
    from app.db import SessionLocal
    from app.models import SyncLink

    with SessionLocal() as db:
        link = db.get(SyncLink, "instance")
        assert link.token_enc == "" and link.org == "" and link.linked_email == ""


@pytest.mark.parametrize("mode", ["5xx", "timeout", "network"])
def test_unreachable_console_is_unknown_never_refused(client, auth, console, mode):
    _start(client, auth)
    if mode == "5xx":
        console.poll_code = 503
    elif mode == "timeout":
        console.poll_exc = httpx.ReadTimeout("slow")
    else:
        console.poll_exc = httpx.ConnectError("down")
    p = client.get("/api/sync/link/poll", headers=auth)
    assert p.status_code == 200 and p.json()["status"] == "unknown"
    assert p.json()["status"] != "refused"
    from app.db import SessionLocal
    from app.models import SyncLink

    with SessionLocal() as db:
        link = db.get(SyncLink, "instance")
        assert link.token_enc == "" and link.org == ""


def test_pending_stores_nothing(client, auth, console):
    _start(client, auth)
    assert client.get("/api/sync/link/poll", headers=auth).json() == {"status": "pending"}


def test_non_admins_are_refused_on_start_and_poll(client, auth, console):
    r = client.post("/api/auth/login", json={"email": "ops@ascme-labs.com", "password": "graphban"})
    ops = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert _start(client, ops).status_code == 403
    assert client.get("/api/sync/link/poll", headers=ops).status_code == 403
    assert console.calls == []


@pytest.mark.skipif(_IS_SQLITE, reason="Alembic owns the schema on Postgres only")
def test_migration_turns_a_key_link_into_a_legacy_deployment_link(client, auth, console):
    from alembic import command
    from alembic.config import Config

    from app.migrate import _BACKEND_ROOT

    cfg = Config(str(_BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    client.post("/api/sync/link", json={"cloud_url": "console.example", "api_key": SECRET_KEY}, headers=auth)
    command.downgrade(cfg, "0193")
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        legacy = conn.execute(text("SELECT legacy_key FROM sync_link")).scalar()
        mapped = conn.execute(text("SELECT local_project_id FROM sync_project")).fetchall()
    assert legacy and len(mapped) == 1
    assert console.calls == []  # never calls the console
    st = _status(client, auth).json()
    assert st["legacy"] is True and len(st["mapped_projects"]) == 1
