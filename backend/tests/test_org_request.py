"""GRPH-1364: Request an org relays to the operator, then stores `org_requested_at`."""
from app.config import settings
from app.services import email as email_svc

BODY = {"name": "Alex", "email": "alex@example.com", "note": "We are four people."}


def _post(client, auth, body=BODY):
    return client.post("/api/auth/me/org-request", json=body, headers=auth)


def test_request_relays_then_stores_the_date(client, auth, monkeypatch):
    monkeypatch.setattr(settings, "org_request_to", "ops@example.com")
    email_svc.outbox.clear()
    r = _post(client, auth)
    assert r.status_code == 200, r.text
    stamped = r.json()["onboarding"]["org_requested_at"]
    assert stamped
    assert [m.to for m in email_svc.outbox] == ["ops@example.com"]
    assert "alex@example.com" in email_svc.outbox[0].text and "four people" in email_svc.outbox[0].text
    got = client.get("/api/auth/me/onboarding", headers=auth).json()["onboarding"]
    assert got["org_requested_at"] == stamped  # survives a reload


def test_request_again_overwrites_the_date_and_keeps_other_marks(client, auth, monkeypatch):
    monkeypatch.setattr(settings, "org_request_to", "ops@example.com")
    client.patch("/api/auth/me/onboarding", json={"mark": "quickstart_finished"}, headers=auth)
    first = _post(client, auth).json()["onboarding"]
    second = _post(client, auth).json()["onboarding"]
    assert second["org_requested_at"] >= first["org_requested_at"]
    assert second["quickstart_finished_at"]


def test_no_operator_address_stores_nothing(client, auth, monkeypatch):
    monkeypatch.setattr(settings, "org_request_to", "")
    monkeypatch.setattr(settings, "platform_admin_emails", "")
    r = _post(client, auth)
    assert r.status_code == 503
    assert client.get("/api/auth/me/onboarding", headers=auth).json()["onboarding"] is None


def test_failed_delivery_stores_nothing(client, auth, monkeypatch):
    monkeypatch.setattr(settings, "org_request_to", "ops@example.com")
    monkeypatch.setattr(email_svc, "send_email", lambda *a, **k: False)
    assert _post(client, auth).status_code == 503
    assert client.get("/api/auth/me/onboarding", headers=auth).json()["onboarding"] is None


def test_bad_input_is_422(client, auth, monkeypatch):
    monkeypatch.setattr(settings, "org_request_to", "ops@example.com")
    for body in ({**BODY, "email": ""}, {**BODY, "email": "nope"}, {**BODY, "name": " "},
                 {**BODY, "extra": 1}, {**BODY, "note": "x" * 2001}):
        assert _post(client, auth, body).status_code == 422, body
