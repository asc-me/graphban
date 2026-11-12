"""GRPH-1517 (P67 §6 L2/L6): the deployment link on the box.

`link/start` and `link/poll` relay the console's device-code API (GBC-77); the 0194 migration
turns a key link into a deployment link with `legacy_key` set and one mapped project; and
`GET /api/sync/status` reports the link with `legacy`, never the token or the key.

The console is faked at `httpx.post`, the one call `services.link_device` makes. A fake that
records nothing is the proof that the migration made no network call.
"""
import importlib.util
import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from app.db import SessionLocal, engine
from app.models import CodeSyncState, Project, SyncLink, SyncProject, utcnow

KEY = "gb_sync_ab12cdef0123456789secretsecret"
KEY_ID = "gb_sync_ab12"
TOKEN = "gb_dep_tokensecretTOKENSECRET"
DEVICE_CODE = "dev-code-SECRET-1"
CONSOLE = "https://console.example"


class Console:
    """The console's two device-code endpoints. `poll_answer` is (status_code, json) or an
    exception to raise."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.start_answer = (200, {"device_code": DEVICE_CODE, "user_code": "ABCD-EFGH",
                                   "verification_url": f"{CONSOLE}/link",
                                   "expires_at": "2030-01-01T00:00:00Z", "interval": 5})
        self.poll_answer = (200, {"status": "pending"})

    def __call__(self, url, json=None, timeout=None, **kw):
        self.calls.append((url, json))
        answer = self.start_answer if url.endswith("/device/start") else self.poll_answer
        if isinstance(answer, Exception):
            raise answer
        code, body = answer
        return httpx.Response(code, json=body, request=httpx.Request("POST", url))

    def bodies(self, suffix):
        return [b for u, b in self.calls if u.endswith(suffix)]


@pytest.fixture()
def console(monkeypatch):
    c = Console()
    monkeypatch.setattr(httpx, "post", c)
    return c


def _migration():
    path = Path(__file__).resolve().parents[1] / "alembic/versions/0194_sync_link_deployment.py"
    spec = importlib.util.spec_from_file_location("mig_0194", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _key_link(client, auth):
    """A pre-0194 key link, as `POST /api/sync/link` saved it."""
    r = client.post("/api/sync/link", json={"cloud_url": "console.example", "api_key": KEY},
                    headers=auth)
    assert r.status_code == 200, r.text


def _migrate():
    with engine.begin() as conn:
        return _migration().migrate_key_links(conn)


def _status(client, auth):
    r = client.get("/api/sync/status", headers=auth)
    assert r.status_code == 200, r.text
    return r


def _start(client, auth, **body):
    return client.post("/api/sync/link/start", json={"label": "box", "console_url": CONSOLE,
                                                     **body}, headers=auth)


def _poll(client, auth):
    return client.get("/api/sync/link/poll", headers=auth)


def _assert_no_secrets(*responses):
    for r in responses:
        for secret in (TOKEN, KEY, DEVICE_CODE):
            assert secret not in r.text, f"{secret!r} leaked in {r.request.url}"


def _link_row() -> dict:
    with SessionLocal() as db:
        link = db.get(SyncLink, "instance")
        if link is None:
            return {}
        return {c: getattr(link, c) for c in (
            "cloud_url", "api_key_enc", "org", "console_url", "linked_email", "token_enc",
            "legacy_key", "linked_at")}


def _mapped() -> set[str]:
    with SessionLocal() as db:
        return {m.local_project_id for m in db.query(SyncProject).all()}


# ---- the migration (L6) ------------------------------------------------------------------

def test_the_migration_makes_a_key_link_a_legacy_link_with_one_project_and_no_network(
        client, auth, console):
    _key_link(client, auth)
    assert _migrate() == 1
    assert console.calls == []  # the migration never calls the console
    assert _mapped() == {"core"}  # nothing pushed yet: the push default
    s = _status(client, auth)
    body = s.json()
    assert body["legacy"] is True and body["signed_in"] is False and body["state"] == "ok"
    core = next(p for p in body["projects"] if p["project_id"] == "core")
    assert core["mapped"] is True and core["last_push_result"] is None  # pending
    others = [p for p in body["projects"] if p["project_id"] != "core"]
    assert all(p["mapped"] is False for p in others)
    _assert_no_secrets(s)
    # Idempotent: a second run migrates nothing and adds no mapping.
    assert _migrate() == 0 and _mapped() == {"core"}


def test_the_migration_maps_the_project_the_key_last_pushed(client, auth, console):
    _key_link(client, auth)
    with SessionLocal() as db:
        db.add(Project(id="side", name="Side", tag="SIDE"))
        db.flush()
        now = utcnow()
        db.add(CodeSyncState(project_id="core", manifest={}, last_synced_at=now - timedelta(days=1)))
        db.add(CodeSyncState(project_id="side", manifest={}, last_synced_at=now))
        db.commit()
    _migrate()
    assert _mapped() == {"side"}


def test_the_migration_leaves_an_unlinked_box_alone(client, auth):
    assert _migrate() == 0
    assert _mapped() == set()
    assert _status(client, auth).json()["legacy"] is False


# ---- start -------------------------------------------------------------------------------

def test_start_returns_the_code_and_sends_no_legacy_key_id_without_a_legacy_key(
        client, auth, console):
    r = _start(client, auth)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "started" and out["user_code"] == "ABCD-EFGH"
    assert out["verification_url"] == f"{CONSOLE}/link" and out["expires_at"]
    (body,) = console.bodies("/device/start")
    assert body["label"] == "box" and "host" in body
    assert "legacy_key_id" not in body
    _assert_no_secrets(r)
    assert _link_row()["token_enc"] == ""  # start writes no link


def test_start_on_a_legacy_link_sends_the_keys_public_prefix_and_never_the_secret(
        client, auth, console):
    _key_link(client, auth)
    _migrate()
    r = _start(client, auth)
    assert r.status_code == 200, r.text
    (body,) = console.bodies("/device/start")
    assert body["legacy_key_id"] == KEY_ID
    assert KEY not in json.dumps(body)
    _assert_no_secrets(r)


def test_a_console_that_does_not_answer_start_is_unknown(client, auth, console):
    console.start_answer = httpx.ConnectError("refused")
    r = _start(client, auth)
    assert r.status_code == 200 and r.json()["status"] == "unknown"
    console.start_answer = (503, {"detail": "down"})
    assert _start(client, auth).json()["status"] == "unknown"


def test_start_without_a_console_url_is_422(client, auth, console):
    r = client.post("/api/sync/link/start", json={}, headers=auth)
    assert r.status_code == 422
    assert console.calls == []


# ---- poll --------------------------------------------------------------------------------

def test_approval_stores_the_token_and_clears_the_legacy_key(client, auth, console):
    _key_link(client, auth)
    _migrate()
    s1 = _start(client, auth)
    console.poll_answer = (200, {"status": "pending"})
    p1 = _poll(client, auth)
    assert p1.json() == {"status": "pending"}
    assert _link_row()["legacy_key"] != ""  # pending writes nothing

    console.poll_answer = (200, {"status": "approved", "org": {"id": "o1", "name": "Acme"},
                                 "email": "eng@acme.dev", "token": TOKEN,
                                 "legacy_project_id": "cloud-alpha"})
    p2 = _poll(client, auth)
    assert p2.status_code == 200
    assert p2.json() == {"status": "approved", "org": "Acme", "email": "eng@acme.dev"}
    assert console.bodies("/device/poll")[-1] == {"device_code": DEVICE_CODE}

    row = _link_row()
    assert row["token_enc"] and row["legacy_key"] == ""
    assert row["org"] == "Acme" and row["linked_email"] == "eng@acme.dev"
    assert row["console_url"] == CONSOLE and row["linked_at"] is not None

    s = _status(client, auth)
    body = s.json()
    assert body["legacy"] is False and body["signed_in"] is True and body["state"] == "ok"
    assert body["org"] == "Acme" and body["linked_email"] == "eng@acme.dev"
    assert body["sign_in_pending"] is False
    assert _mapped() == {"core"}  # the mapping survives sign-in
    _assert_no_secrets(s1, p1, p2, s)

    # The sign-in is spent: polling again has nothing in flight.
    assert _poll(client, auth).status_code == 404


def test_the_token_is_encrypted_at_rest_when_a_key_is_configured(client, auth, console,
                                                                 monkeypatch):
    from app.config import settings
    from app.security import secrets

    monkeypatch.setattr(settings, "secret_encryption_key", "k" * 32)
    secrets._fernet.cache_clear()
    try:
        _start(client, auth)
        console.poll_answer = (200, {"status": "approved", "org": {"id": "o", "name": "O"},
                                     "email": "e@o.dev", "token": TOKEN})
        assert _poll(client, auth).json()["status"] == "approved"
        stored = _link_row()["token_enc"]
        assert TOKEN not in stored and secrets.decrypt(stored) == TOKEN
    finally:
        secrets._fernet.cache_clear()


@pytest.mark.parametrize("reason", ["declined", "expired", "not_member", "no_seat"])
def test_each_refusal_says_which_and_stores_nothing(client, auth, console, reason):
    _key_link(client, auth)
    _migrate()
    _start(client, auth)
    before = _link_row()
    console.poll_answer = (200, {"status": "refused", "reason": reason})
    r = _poll(client, auth)
    assert r.json() == {"status": "refused", "reason": reason}
    assert _link_row() == before
    assert _status(client, auth).json()["legacy"] is True
    _assert_no_secrets(r)


@pytest.mark.parametrize("answer", [
    httpx.ConnectError("connection refused"),
    httpx.ReadTimeout("timed out"),
    (500, {"detail": "boom"}),
    (502, {"detail": "bad gateway"}),
    (503, {"status": "refused", "reason": "declined"}),  # a 5xx body is not an answer
    (200, {"status": "refused", "reason": "something_new"}),
    (200, {"status": "approved", "org": {"name": "Acme"}, "email": "e@a.dev"}),  # no token
    (200, {"status": "weird"}),
    (400, {"detail": "bad request"}),
])
def test_no_answer_is_unknown_never_refused_and_stores_nothing(client, auth, console, answer):
    _start(client, auth)
    before = _link_row()
    console.poll_answer = answer
    r = _poll(client, auth)
    assert r.status_code == 200
    out = r.json()
    assert out["status"] == "unknown", out
    assert out["reason"]
    assert _link_row() == before


def test_a_code_the_console_does_not_know_is_refused_expired(client, auth, console):
    _start(client, auth)
    console.poll_answer = (404, {"detail": "unknown_code"})
    assert _poll(client, auth).json() == {"status": "refused", "reason": "expired"}


def test_poll_with_no_sign_in_in_flight_is_404(client, auth, console):
    assert _poll(client, auth).status_code == 404
    assert console.calls == []


# ---- who may sign in ---------------------------------------------------------------------

def _login(client, email):
    r = client.post("/api/auth/login", json={"email": email, "password": "graphban"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_a_non_admin_is_refused_on_start_and_poll(client, auth, console):
    ops = _login(client, "ops@ascme-labs.com")  # seeded `member` on every project
    r = _start(client, ops)
    assert r.status_code == 403 and r.json()["rule_id"] == "sync.link_needs_box_admin"
    _start(client, auth)  # an admin's sign-in in flight
    console.poll_answer = (200, {"status": "approved", "org": {"name": "Evil"}, "email": "x@y",
                                 "token": TOKEN})
    r = _poll(client, ops)
    assert r.status_code == 403 and r.json()["rule_id"] == "sync.link_needs_box_admin"
    assert len(console.bodies("/device/start")) == 1 and console.bodies("/device/poll") == []
    assert _link_row()["token_enc"] == ""


# ---- status and unlink -------------------------------------------------------------------

def _set_push(project_id, result, reason=None):
    with SessionLocal() as db:
        m = db.get(SyncProject, project_id)
        m.last_push_at, m.last_push_result, m.last_push_reason = utcnow(), result, reason
        db.commit()


def test_status_reports_each_mapped_projects_last_result_and_the_links_state(
        client, auth, console):
    _key_link(client, auth)
    _migrate()
    _set_push("core", "ok")
    body = _status(client, auth).json()
    core = next(p for p in body["projects"] if p["project_id"] == "core")
    assert core["last_push_result"] == "ok" and core["last_push_at"] and body["state"] == "ok"
    # One project's refusal is that project's, not the link's (L5)...
    _set_push("core", "refused", "no_access")
    body = _status(client, auth).json()
    assert body["state"] == "ok"
    assert next(p for p in body["projects"] if p["project_id"] == "core")[
        "last_push_reason"] == "no_access"
    # ...a refused seat refuses the link, and no answer is unknown, not refused.
    _set_push("core", "refused", "seat_removed")
    assert _status(client, auth).json()["state"] == "refused"
    _set_push("core", "unknown", "timeout")
    assert _status(client, auth).json()["state"] == "unknown"


def test_unlink_deletes_the_token_and_the_mappings(client, auth, console):
    _key_link(client, auth)
    _migrate()
    _start(client, auth)
    console.poll_answer = (200, {"status": "approved", "org": {"name": "Acme"},
                                 "email": "e@a.dev", "token": TOKEN})
    _poll(client, auth)
    r = client.delete("/api/sync/link", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["linked"] is False and body["signed_in"] is False and body["state"] is None
    row = _link_row()
    assert row["token_enc"] == "" and row["legacy_key"] == ""
    assert _mapped() == set()


def _cloud_ids() -> dict[str, str]:
    with SessionLocal() as db:
        return {m.local_project_id: m.cloud_project_id for m in db.query(SyncProject).all()}


def test_sign_in_on_a_migrated_key_link_fills_the_pinned_project(client, auth, console):
    _key_link(client, auth)
    _migrate()
    assert _cloud_ids() == {"core": ""}
    _start(client, auth)
    console.poll_answer = (200, {"status": "approved", "org": {"name": "Acme"}, "email": "e@a.dev",
                                 "token": TOKEN, "legacy_project_id": "cloud-alpha"})
    assert _poll(client, auth).json()["status"] == "approved"
    assert _cloud_ids() == {"core": "cloud-alpha"}  # sabotage: drop the fill and this is ""


def test_sign_in_without_the_keys_project_leaves_the_row_unmapped(client, auth, console):
    _key_link(client, auth)
    _migrate()
    _start(client, auth)
    console.poll_answer = (200, {"status": "approved", "org": {"name": "Acme"}, "email": "e@a.dev",
                                 "token": TOKEN})
    assert _poll(client, auth).json()["status"] == "approved"
    assert _cloud_ids() == {}
