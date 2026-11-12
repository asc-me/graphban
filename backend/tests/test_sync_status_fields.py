"""GRPH-1537: /api/sync/status reports can_link and console_url_env."""
from app.config import settings

from tests.test_sync_link import _login, _status


def test_box_admin_can_link_unlinked_and_linked(client, auth):
    assert _status(client, auth)["can_link"] is True
    client.post("/api/sync/link", json={"cloud_url": "cloud.a.dev", "api_key": "gb_sk_x"}, headers=auth)
    assert _status(client, auth)["can_link"] is True


def test_non_admin_cannot_link_and_link_start_is_refused(client, auth):
    ops = _login(client, "ops@ascme-labs.com")
    assert _status(client, ops)["can_link"] is False
    client.post("/api/sync/link", json={"cloud_url": "cloud.a.dev", "api_key": "gb_sk_x"}, headers=auth)
    assert _status(client, ops)["can_link"] is False
    assert client.post("/api/sync/link/start", json={}, headers=ops).status_code == 403


def test_console_url_env_only_url(client, auth, monkeypatch):
    monkeypatch.setattr(settings, "sync_cloud_url", "https://console.example.dev")
    monkeypatch.setattr(settings, "sync_api_key", "")
    s = _status(client, auth)
    assert s["console_url_env"] == "https://console.example.dev"
    assert s["linked"] is False and s["source"] == ""


def test_console_url_env_unset(client, auth, monkeypatch):
    monkeypatch.setattr(settings, "sync_cloud_url", "")
    monkeypatch.setattr(settings, "sync_api_key", "")
    assert _status(client, auth)["console_url_env"] == ""


def test_console_url_env_with_both_and_no_secret(client, auth, monkeypatch):
    monkeypatch.setattr(settings, "sync_cloud_url", "https://console.example.dev")
    monkeypatch.setattr(settings, "sync_api_key", "gb_sk_topsecret")
    r = client.get("/api/sync/status", headers=auth)
    s = r.json()
    assert s["console_url_env"] == "https://console.example.dev" and s["source"] == "env"
    assert "gb_sk_topsecret" not in r.text
