"""AL-44 / review finding F2: public-surface hardening — webhook HMAC, weak-secret
startup guard, proxy-aware rate-limit IP, and rate-limiter key eviction."""
import hashlib
import hmac
import json

import pytest

from app.config import settings


def _issue_payload(title="Hooked", repo="acme/app"):
    return {
        "action": "opened",
        "issue": {"title": title, "body": "b", "html_url": "https://x/1"},
        "repository": {"full_name": repo},
    }


# ---- GitHub webhook HMAC ----

def test_webhook_open_by_default_when_no_secret(client, monkeypatch):
    monkeypatch.setattr(settings, "github_webhook_secret", "")
    r = client.post("/api/public/github/webhook", json=_issue_payload())
    assert r.status_code == 200
    assert r.json()["created_item"]


def test_webhook_rejects_unsigned_when_secret_set(client, monkeypatch):
    monkeypatch.setattr(settings, "github_webhook_secret", "shhh")
    r = client.post("/api/public/github/webhook", json=_issue_payload())
    assert r.status_code == 401


def test_webhook_rejects_bad_signature(client, monkeypatch):
    monkeypatch.setattr(settings, "github_webhook_secret", "shhh")
    r = client.post(
        "/api/public/github/webhook",
        json=_issue_payload(),
        headers={"X-Hub-Signature-256": "sha256=deadbeef"},
    )
    assert r.status_code == 401


def test_webhook_accepts_valid_signature(client, monkeypatch):
    monkeypatch.setattr(settings, "github_webhook_secret", "shhh")
    raw = json.dumps(_issue_payload()).encode()
    sig = "sha256=" + hmac.new(b"shhh", raw, hashlib.sha256).hexdigest()
    r = client.post(
        "/api/public/github/webhook",
        content=raw,
        headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"},
    )
    assert r.status_code == 200
    assert r.json()["created_item"]


# ---- weak-secret startup guard ----

def test_check_security_raises_when_required(monkeypatch):
    from app.security import startup

    monkeypatch.setattr(settings, "database_url", "postgresql+psycopg://x/y")
    monkeypatch.setattr(settings, "jwt_secret", "dev-secret-change-me-in-production-0123456789abcdef")
    monkeypatch.setattr(settings, "require_strong_secret", True)
    with pytest.raises(RuntimeError):
        startup.check_security()


def test_check_security_warns_but_allows_by_default(monkeypatch, caplog):
    from app.security import startup

    monkeypatch.setattr(settings, "database_url", "postgresql+psycopg://x/y")
    monkeypatch.setattr(settings, "jwt_secret", "short")
    monkeypatch.setattr(settings, "require_strong_secret", False)
    startup.check_security()  # no raise
    assert "SECURITY WARNING" in caplog.text


def test_check_security_skips_sqlite(monkeypatch):
    from app.security import startup

    # Force SQLite explicitly — don't rely on the ambient DATABASE_URL, which is
    # Postgres in the CI proof job (that mismatch was the bug this test caught).
    monkeypatch.setattr(settings, "database_url", "sqlite:///./x.db")
    monkeypatch.setattr(settings, "jwt_secret", "short")
    monkeypatch.setattr(settings, "require_strong_secret", True)
    startup.check_security()  # sqlite (test DB) → skipped, no raise


def test_strong_secret_is_not_weak(monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret", "x" * 40)
    assert settings.jwt_secret_is_weak is False


# ---- proxy-aware client IP ----

def test_forwarded_for_ignored_without_trusted_proxy(monkeypatch):
    from app.routers import public

    monkeypatch.setattr(settings, "trusted_proxy", False)

    class _Req:
        headers = {"x-forwarded-for": "9.9.9.9"}
        client = type("C", (), {"host": "10.0.0.1"})()

    assert public._client_ip(_Req()) == "10.0.0.1"


def test_forwarded_for_used_when_trusted(monkeypatch):
    from app.routers import public

    monkeypatch.setattr(settings, "trusted_proxy", True)

    class _Req:
        headers = {"x-forwarded-for": "9.9.9.9, 10.0.0.1"}
        client = type("C", (), {"host": "10.0.0.1"})()

    assert public._client_ip(_Req()) == "9.9.9.9"


# ---- rate-limiter eviction ----

def test_sweep_frees_stale_keys():
    from app.services import spam

    spam._hits.clear()
    assert spam.check_rate("k1", 5) is True  # creates a live entry
    spam._sweep_stale(now=spam.time.monotonic() + spam._WINDOW + 1)  # everything expired
    assert "k1" not in spam._hits
