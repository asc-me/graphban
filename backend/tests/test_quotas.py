"""Plan quotas removed from the product (GBC-35).

Hosted installs no longer return HTTP 402 for plan limits; caps and Stripe billing
live on the control plane. These tests keep the self-host regression and the
acceptance criterion from GBC-P5.
"""
import pytest

from app.services import quotas


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


def _set_plan(monkeypatch, tier="free", **limits):
    base = quotas.PLANS[tier]
    monkeypatch.setitem(
        quotas.PLANS,
        tier,
        quotas.Plan(
            max_projects=limits.get("max_projects", base.max_projects),
            max_seats=limits.get("max_seats", base.max_seats),
            max_shards=limits.get("max_shards", base.max_shards),
            max_calls_per_month=limits.get("max_calls_per_month", base.max_calls_per_month),
        ),
    )


def test_self_host_has_no_quotas(client, monkeypatch):
    _set_plan(monkeypatch, max_projects=1)  # would bite if product still enforced caps
    auth = _login(client, "alex@ascme-labs.com")
    for i in range(3):
        assert client.post("/api/projects", json={"name": f"P{i}"}, headers=auth).status_code == 201


def test_control_plane_product_install_without_stripe_serves_tracker_no_billing_surface(
    client, hosted, monkeypatch,
):
    """A product install with no Stripe settings serves the tracker and does not expose
    Checkout, the portal, the webhook, or a plan quota."""
    _set_plan(monkeypatch, max_projects=1, max_seats=1, max_shards=1, max_calls_per_month=1)
    auth = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, auth)
    oid = org["id"]

    assert client.get("/api/projects", headers=auth).status_code == 200
    assert client.post("/api/projects", json={"name": "One"}, headers=auth).status_code == 201
    assert client.post("/api/projects", json={"name": "Two"}, headers=auth).status_code == 201

    assert client.get(f"/api/orgs/{oid}/billing", headers=auth).status_code == 404
    assert client.post(
        f"/api/orgs/{oid}/billing/checkout",
        json={"plan": "pro", "success_url": "https://x/s", "cancel_url": "https://x/c"},
        headers=auth,
    ).status_code == 404
    assert client.post(
        f"/api/orgs/{oid}/billing/portal",
        json={"return_url": "https://x"},
        headers=auth,
    ).status_code == 404
    assert client.post(
        "/api/public/stripe/webhook",
        content=b"{}",
        headers={"stripe-signature": "t=1,v1=x"},
    ).status_code == 404

    key = client.post("/api/api-keys", json={"name": "agent"}, headers=auth).json()["plaintext"]
    for _ in range(3):
        r = client.post(
            "/api/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "get_backlog", "arguments": {}},
            },
            headers={"X-API-Key": key},
        ).json()
        result = r["result"]
        assert result.get("isError") is not True
