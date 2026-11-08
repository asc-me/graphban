"""GRPH-1365: the instance posture read and `projects.repo_url`."""
import pytest

from app.config import DEFAULT_JWT_SECRET, settings

SECRET = "a-very-long-random-secret-value-1234567890"


def _login(client):
    """Tokens are signed with `jwt_secret`, so sign in AFTER changing it."""
    r = client.post("/api/auth/login", json={"email": "alex@ascme-labs.com", "password": "graphban"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _stranger(client):
    """A signed-in user who owns no project: not an instance admin."""
    body = {"name": "Sam Rowe", "email": "sam@example.com", "handle": "samrowe", "password": "c0rrect-horse-battery"}
    r = client.post("/api/auth/register", json=body)
    assert r.status_code in (200, 201), r.text
    tok = client.post("/api/auth/login", json={"email": body["email"], "password": body["password"]}).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}


def test_non_admin_gets_403_and_anonymous_401(client):
    assert client.get("/api/setup/instance").status_code == 401
    r = client.get("/api/setup/instance", headers=_stranger(client))
    assert r.status_code == 403 and r.json()["rule_id"] == "setup.posture_needs_instance_admin"


def test_never_returns_a_value(client, auth, monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret", SECRET)
    monkeypatch.setattr(settings, "secret_encryption_key", "super-secret-fernet-key-xyz")
    r = client.get("/api/setup/instance", headers=_login(client))
    assert r.status_code == 200, r.text
    assert all(isinstance(v, bool) for v in r.json().values())
    assert SECRET not in r.text and "super-secret-fernet-key-xyz" not in r.text


def test_placeholder_jwt_secret_is_flagged_and_fixing_it_clears(client, auth, monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret", DEFAULT_JWT_SECRET)
    bad = client.get("/api/setup/instance", headers=_login(client)).json()
    assert bad["jwt_secret_set"] is False and bad["jwt_secret_strong"] is False
    monkeypatch.setattr(settings, "jwt_secret", SECRET)
    good = client.get("/api/setup/instance", headers=_login(client)).json()
    assert good["jwt_secret_set"] is True and good["jwt_secret_strong"] is True


def test_other_settings_and_loopback(client, auth, monkeypatch):
    monkeypatch.setattr(settings, "signup_mode", "invite_only")
    monkeypatch.setattr(settings, "secret_encryption_key", "")
    monkeypatch.setattr(settings, "app_base_url", "https://gb.example.com")
    d = client.get("/api/setup/instance", headers=auth).json()
    assert d["signup_open"] is False
    assert d["secret_encryption_key_set"] is False
    assert d["app_base_url_set"] is True
    assert isinstance(d["request_on_loopback"], bool)
    monkeypatch.setattr(settings, "signup_mode", "open")
    assert client.get("/api/setup/instance", headers=auth).json()["signup_open"] is True


@pytest.mark.parametrize("raw,want", [
    ("https://github.com/Acme/App.git", "github.com/acme/app"),
    ("git@github.com:acme/app", "github.com/acme/app"),
    ("ssh://git@github.com:22/acme/app/", "github.com/acme/app"),
    ("github.com/acme/app", "github.com/acme/app"),
    ("git@github.com:1password/cli.git", "github.com/1password/cli"),
    ("https://github.com/1password/cli", "github.com/1password/cli"),
    ("ssh://git@github.com/37signals/x", "github.com/37signals/x"),
    ("git@github.com:37signals/x", "github.com/37signals/x"),
    ("https://user:tok@gitlab.com/a/b/c.git", "gitlab.com/a/b/c"),
    ("  ", None), (None, None),
])
def test_normalize_repo_url_table(raw, want):
    from app.services.projects import normalize_repo_url
    assert normalize_repo_url(raw) == want


def test_repo_url_round_trips_normalized_and_clears(client, auth):
    pid = client.get("/api/projects", headers=auth).json()[0]["id"]
    r = client.patch(f"/api/projects/{pid}", json={"repo_url": "git@github.com:Acme/App.git"}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["repo_url"] == "github.com/acme/app"
    listed = [p for p in client.get("/api/projects", headers=auth).json() if p["id"] == pid][0]
    assert listed["repo_url"] == "github.com/acme/app"
    assert client.patch(f"/api/projects/{pid}", json={"repo_url": ""}, headers=auth).json()["repo_url"] is None
    assert client.patch(f"/api/projects/{pid}", json={"repo_url": "nonsense"}, headers=auth).status_code == 422
