"""GRPH-1344 / PRD-P62 D8: an attached box fetches and refreshes its plan grant."""
import base64
import json
import time

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.services import plan_grant as G

KEY = Ed25519PrivateKey.generate()
PUB = KEY.public_key().public_bytes(serialization.Encoding.Raw,
                                    serialization.PublicFormat.Raw).hex()
T0 = 1_800_000_000


def _grant(org_id, issued, ttl=1000, plan="team"):
    doc = json.dumps({"org_id": org_id, "deployment_id": "dep-1", "plan": plan,
                      "issued_at": issued, "expires_at": issued + ttl},
                     sort_keys=True, separators=(",", ":")).encode()
    return doc, KEY.sign(doc)


class FakeControlPlane:
    def __init__(self):
        self.calls = []
        self.grant = None
        self.status = 200
        self.down = False

    def post(self, url, headers=None, timeout=None, **kw):
        self.calls.append((url, headers))
        if self.down:
            raise httpx.ConnectError("down")
        req = httpx.Request("POST", url)
        if self.status != 200:
            return httpx.Response(self.status, request=req)
        doc, sig = self.grant
        return httpx.Response(200, request=req, json={
            "grant_b64": base64.b64encode(doc).decode(),
            "signature_b64": base64.b64encode(sig).decode()})


@pytest.fixture()
def box(client, monkeypatch):
    from app.config import settings
    from app.db import SessionLocal
    from app.models import InstalledPlanGrant, RuleDecisionPull

    monkeypatch.setattr(G, "TRUSTED_PLAN_GRANT_KEYS", (PUB,))
    monkeypatch.setattr(settings, "hosted_mode", True)   # only to create the org
    r = client.post("/api/auth/login", json={"email": "alex@ascme-labs.com",
                                             "password": "graphban"})
    owner = {"Authorization": f"Bearer {r.json()['access_token']}"}
    org_id = client.post("/api/orgs", json={"name": "Acme"}, headers=owner).json()["id"]
    monkeypatch.setattr(settings, "hosted_mode", False)
    monkeypatch.setattr(settings, "sync_cloud_url", "https://cp.example")
    monkeypatch.setattr(settings, "sync_api_key", "gb_sk_link")
    cp = FakeControlPlane()
    monkeypatch.setattr(G.httpx, "post", cp.post)
    reset = dict(reason="missing", plan=None, issued_at=None, expires_at=None)
    G._state.update(reset)
    G._last_fetch.update(at=None, ok=None, reason=G.NOT_ATTACHED)
    db = SessionLocal()
    try:
        db.query(InstalledPlanGrant).delete()
        db.merge(RuleDecisionPull(id="instance", org_id=org_id))
        db.commit()
        yield db, cp, org_id
    finally:
        db.close()
        G._state.update(reset)


def test_linked_box_installs_a_fetched_grant_at_startup(box):
    db, cp, org = box
    cp.grant = _grant(org, T0)
    assert G.refresh_if_due(db, now=T0 + 1, startup=True)
    st = G.status()
    assert st["ok"] and st["plan"] == "team"
    assert st["last_fetch"] == {"at": T0 + 1, "ok": True, "reason": None}
    assert cp.calls == [("https://cp.example/api/sync/plan-grant", {"X-API-Key": "gb_sk_link"})]


def test_refresh_fires_only_once_under_half_the_lifetime_remains(box):
    db, cp, org = box
    cp.grant = _grant(org, T0, ttl=1000)
    G.refresh_if_due(db, now=T0, startup=True)
    cp.calls.clear()
    assert not G.refresh_if_due(db, now=T0 + 500)      # exactly half left: not yet
    assert cp.calls == []
    cp.grant = _grant(org, T0 + 501, ttl=1000)
    assert G.refresh_if_due(db, now=T0 + 501)           # under half: fires
    assert len(cp.calls) == 1
    assert G.status()["issued_at"] == T0 + 501


@pytest.mark.parametrize("failure", ["401", "down", "500"])
def test_a_failed_fetch_keeps_the_installed_grant_and_records_why(box, failure):
    db, cp, org = box
    cp.grant = _grant(org, T0, ttl=1000)
    G.fetch_once(db, now=T0)
    if failure == "down":
        cp.down = True
    else:
        cp.status = int(failure)
    G.fetch_once(db, now=T0 + 600)
    st = G.status()
    assert st["ok"] and st["expires_at"] == T0 + 1000          # still in force
    assert G.current(db, now=T0 + 900)["ok"]
    lf = st["last_fetch"]
    assert lf["ok"] is False and lf["at"] == T0 + 600
    assert lf["reason"] == {"401": "unauthorized", "down": "unreachable",
                            "500": "http_500"}[failure]
    assert G.current(db, now=T0 + 1000)["reason"] == "expired"  # until its own expiry


def test_without_a_valid_grant_it_retries_hourly(box):
    db, cp, org = box
    cp.down = True
    assert G.refresh_if_due(db, now=T0, startup=True)
    assert not G.refresh_if_due(db, now=T0 + 3599)
    assert G.refresh_if_due(db, now=T0 + 3600)


def test_unlinked_box_makes_no_outbound_call(box, monkeypatch):
    from app.config import settings
    db, cp, org = box
    monkeypatch.setattr(settings, "sync_cloud_url", "")
    monkeypatch.setattr(settings, "sync_api_key", "")
    cp.grant = _grant(org, T0)
    G.refresh_if_due(db, now=T0, startup=True)
    assert cp.calls == []
    assert G.status()["last_fetch"]["reason"] == "not attached"
    assert not G.status()["ok"]
