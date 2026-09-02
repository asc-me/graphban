"""Self-serve Stripe billing (GRPH-82).

Unset keys keep the manual plan path. The webhook's CALL is `apply_event`.
A missing signature is 401, not a quiet apply.
"""
import pytest

from app.models import Organization
from app.services import billing as billing_svc


def _login(client, email, password="graphban"):
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _make_org(client, headers, name="Acme"):
    r = client.post("/api/orgs", json={"name": name}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture()
def hosted(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "hosted_mode", True)
    return settings


@pytest.fixture()
def stripe_on(hosted, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "stripe_api_key", "sk_test_x")
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_x")
    monkeypatch.setattr(settings, "stripe_price_pro", "price_pro")
    monkeypatch.setattr(settings, "stripe_price_team", "price_team")
    return settings


def test_unset_stripe_hides_checkout_and_the_webhook(client, hosted):
    auth = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, auth)
    assert client.post(
        f"/api/orgs/{org['id']}/billing/checkout",
        json={"plan": "pro", "success_url": "https://x/ok", "cancel_url": "https://x/no"},
        headers=auth,
    ).status_code == 404
    assert client.post("/api/public/stripe/webhook", content=b"{}",
                       headers={"stripe-signature": "t=1,v1=x"}).status_code == 404
    body = client.get(f"/api/orgs/{org['id']}/billing", headers=auth).json()
    assert body["self_serve"] is False
    assert body["has_customer"] is False
    assert body["plan"] == "free"


def test_manual_plan_path_still_works_when_stripe_is_on(client, stripe_on, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "platform_admin_emails", "alex@ascme-labs.com")
    auth = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, auth)
    r = client.put(f"/api/orgs/{org['id']}/plan", json={"plan": "team"}, headers=auth)
    assert r.status_code == 200
    assert r.json()["plan"] == "team"


def test_checkout_mints_a_url(client, stripe_on, monkeypatch):
    auth = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, auth)
    monkeypatch.setattr(
        billing_svc, "create_checkout_url",
        lambda *a, **k: "https://checkout.stripe.com/c/session_test",
    )
    r = client.post(
        f"/api/orgs/{org['id']}/billing/checkout",
        json={"plan": "pro", "success_url": "https://x/ok", "cancel_url": "https://x/no"},
        headers=auth,
    )
    assert r.status_code == 200
    assert r.json()["url"].startswith("https://checkout.stripe.com/")


def test_checkout_refuses_enterprise(client, stripe_on):
    auth = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, auth)
    r = client.post(
        f"/api/orgs/{org['id']}/billing/checkout",
        json={"plan": "enterprise", "success_url": "https://x/ok", "cancel_url": "https://x/no"},
        headers=auth,
    )
    assert r.status_code == 422


def test_portal_needs_a_customer(client, stripe_on):
    auth = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, auth)
    r = client.post(
        f"/api/orgs/{org['id']}/billing/portal",
        json={"return_url": "https://x/back"},
        headers=auth,
    )
    assert r.status_code == 404


def test_webhook_without_a_signature_is_401(client, stripe_on, monkeypatch):
    def boom(*a, **k):
        raise ValueError("webhook signature verification failed")
    monkeypatch.setattr(billing_svc, "construct_event", boom)
    r = client.post("/api/public/stripe/webhook", content=b"{}",
                    headers={"stripe-signature": "nope"})
    assert r.status_code == 401


def test_apply_event_sets_plan_and_customer(client, hosted):
    from app.db import SessionLocal
    auth = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, auth)
    db = SessionLocal()
    try:
        billing_svc.apply_event(db, {
            "type": "checkout.session.completed",
            "data": {"object": {
                "metadata": {"org_id": org["id"], "plan": "pro"},
                "client_reference_id": org["id"],
                "customer": "cus_abc",
            }},
        })
    finally:
        db.close()
    body = client.get(f"/api/orgs/{org['id']}/billing", headers=auth).json()
    assert body["plan"] == "pro"
    db = SessionLocal()
    try:
        row = db.get(Organization, org["id"])
        assert row.stripe_customer_id == "cus_abc"
    finally:
        db.close()


def test_subscription_deleted_returns_to_free(client, hosted):
    from app.db import SessionLocal
    auth = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, auth)
    db = SessionLocal()
    try:
        row = db.get(Organization, org["id"])
        row.plan = "pro"
        row.stripe_customer_id = "cus_abc"
        db.commit()
        billing_svc.apply_event(db, {
            "type": "customer.subscription.deleted",
            "data": {"object": {"customer": "cus_abc"}},
        })
    finally:
        db.close()
    assert client.get(f"/api/orgs/{org['id']}/billing", headers=auth).json()["plan"] == "free"


def test_configured_stripe_reports_self_serve(client, stripe_on):
    auth = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, auth)
    body = client.get(f"/api/orgs/{org['id']}/billing", headers=auth).json()
    assert body["self_serve"] is True
    assert body["has_customer"] is False


def test_has_customer_is_the_org_column(client, stripe_on):
    """THE CALL. A hardcoded false would hide Manage billing after checkout."""
    from app.db import SessionLocal
    auth = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, auth)
    db = SessionLocal()
    try:
        row = db.get(Organization, org["id"])
        row.stripe_customer_id = "cus_abc"
        db.commit()
    finally:
        db.close()
    body = client.get(f"/api/orgs/{org['id']}/billing", headers=auth).json()
    assert body["has_customer"] is True
    assert body["self_serve"] is True


def test_subscription_updated_maps_price_to_plan(client, stripe_on):
    from app.db import SessionLocal
    auth = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, auth)
    db = SessionLocal()
    try:
        row = db.get(Organization, org["id"])
        row.stripe_customer_id = "cus_abc"
        db.commit()
        billing_svc.apply_event(db, {
            "type": "customer.subscription.updated",
            "data": {"object": {
                "customer": "cus_abc",
                "items": {"data": [{"price": {"id": "price_team"}}]},
            }},
        })
    finally:
        db.close()
    assert client.get(f"/api/orgs/{org['id']}/billing", headers=auth).json()["plan"] == "team"


def test_unknown_event_does_not_change_plan(client, hosted):
    from app.db import SessionLocal
    auth = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, auth)
    db = SessionLocal()
    try:
        billing_svc.apply_event(db, {
            "type": "invoice.paid",
            "data": {"object": {"customer": "cus_abc"}},
        })
    finally:
        db.close()
    assert client.get(f"/api/orgs/{org['id']}/billing", headers=auth).json()["plan"] == "free"


def test_webhook_calls_apply_event(client, stripe_on, monkeypatch):
    """Sabotage the CALL: verifying a signature without applying would look healthy."""
    called = {"n": 0}

    def fake_construct(payload, sig):
        return {"type": "checkout.session.completed", "data": {"object": {}}}

    def fake_apply(db, event):
        called["n"] += 1

    monkeypatch.setattr(billing_svc, "construct_event", fake_construct)
    monkeypatch.setattr(billing_svc, "apply_event", fake_apply)
    r = client.post("/api/public/stripe/webhook", content=b"{}",
                    headers={"stripe-signature": "t=1,v1=ok"})
    assert r.status_code == 200
    assert called["n"] == 1
