"""GRPH-1343 / PRD-P62 D8: a self-hosted box reads its capacity plan from a signed grant."""
import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.services import plan_grant as G

KEY = Ed25519PrivateKey.generate()
PUB = KEY.public_key().public_bytes(serialization.Encoding.Raw,
                                    serialization.PublicFormat.Raw).hex()
OTHER = Ed25519PrivateKey.generate()


def _login(client):
    r = client.post("/api/auth/login", json={"email": "alex@ascme-labs.com",
                                             "password": "graphban"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _grant(org_id, plan="team", issued=None, ttl=3600, key=KEY):
    now = int(time.time())
    doc = json.dumps({"org_id": org_id, "deployment_id": "dep-1", "plan": plan,
                      "issued_at": issued or now - 10, "expires_at": (issued or now) + ttl},
                     sort_keys=True, separators=(",", ":")).encode()
    return doc, key.sign(doc)


def _body(doc, sig):
    return {"grant_b64": base64.b64encode(doc).decode(),
            "signature_b64": base64.b64encode(sig).decode()}


@pytest.fixture()
def box(client, monkeypatch):
    from app.config import settings
    from app.db import SessionLocal
    from app.models import InstalledPlanGrant, Organization, Project, RuleDecisionPull

    monkeypatch.setattr(settings, "hosted_mode", True)   # only to create the org
    monkeypatch.setattr(G, "TRUSTED_PLAN_GRANT_KEYS", (PUB,))
    G._state.update(reason="missing", plan=None, issued_at=None, expires_at=None)
    owner = _login(client)
    org = client.post("/api/orgs", json={"name": "Acme"}, headers=owner).json()
    proj = client.post("/api/projects", json={"name": "Rocket"}, headers=owner).json()
    monkeypatch.setattr(settings, "hosted_mode", False)
    db = SessionLocal()
    try:
        db.get(Project, proj["id"]).org_id = org["id"]
        db.get(Organization, org["id"]).plan = "team"   # the column an operator can edit
        db.query(InstalledPlanGrant).delete()
        db.merge(RuleDecisionPull(id="instance", org_id=org["id"]))
        db.commit()
    finally:
        db.close()
    yield {"owner": owner, "org": org["id"], "project": proj["id"]}
    G._state.update(reason="missing", plan=None, issued_at=None, expires_at=None)


def _list(client, box):
    return client.get(f"/api/harness/capacity?project_id={box['project']}",
                      headers=box["owner"])


def _install(client, box, doc, sig):
    return client.post("/api/platform/plan-grant", json=_body(doc, sig), headers=box["owner"])


def test_no_grant_is_402_naming_missing_whatever_org_plan_says(client, box):
    r = _list(client, box)
    assert r.status_code == 402, r.text
    assert "missing" in r.text and "install a license" in r.text


def test_a_grant_that_expires_later_is_402_naming_expired(client, box):
    _install(client, box, *_grant(box["org"], ttl=3600))
    G._state["expires_at"] = int(time.time()) - 1   # the clock passes expires_at
    r = _list(client, box)
    assert r.status_code == 402 and "expired" in r.text


def test_valid_grant_unlocks(client, box):
    assert _install(client, box, *_grant(box["org"])).json()["ok"] is True
    assert _list(client, box).status_code == 200
    assert client.get("/api/platform/plan-grant", headers=box["owner"]).json()["plan"] == "team"


def test_expired_grant_names_expired(client, box):
    r = _install(client, box, *_grant(box["org"], issued=int(time.time()) - 100, ttl=10))
    assert r.json()["reason"] == "expired"
    assert _list(client, box).status_code == 402


def test_unknown_key(client, box, monkeypatch):
    monkeypatch.setattr(G, "TRUSTED_PLAN_GRANT_KEYS", ())
    assert _install(client, box, *_grant(box["org"])).json()["reason"] == "unknown_key"
    r = _list(client, box)
    assert r.status_code == 402 and "missing" in r.text


def test_signed_by_another_key_is_bad_signature(client, box):
    assert _install(client, box, *_grant(box["org"], key=OTHER)).json()["reason"] == \
        "bad_signature"


def test_one_flipped_byte(client, box):
    doc, sig = _grant(box["org"])
    bad = bytes([doc[0] ^ 1]) + doc[1:]
    assert _install(client, box, bad, sig).json()["reason"] == "bad_signature"
    assert _list(client, box).status_code == 402


def test_another_org(client, box):
    assert _install(client, box, *_grant("someone-else")).json()["reason"] == "wrong_org"
    assert _list(client, box).status_code == 402


def test_older_grant_refused_later_replaces(client, box):
    now = int(time.time())
    assert _install(client, box, *_grant(box["org"], issued=now - 100, ttl=9999)).json()["ok"]
    assert _install(client, box, *_grant(box["org"], issued=now - 200, ttl=9999)
                    ).json()["reason"] == "older_than_installed"
    assert _install(client, box, *_grant(box["org"], plan="enterprise", issued=now - 50,
                                         ttl=9999)).json()["plan"] == "enterprise"
    assert client.get("/api/platform/plan-grant", headers=box["owner"]).json()["plan"] == \
        "enterprise"


def test_signature_is_verified_at_startup_and_install_only(client, box, monkeypatch):
    from app.db import SessionLocal

    _install(client, box, *_grant(box["org"]))
    calls = []
    real = G.verify
    monkeypatch.setattr(G, "verify", lambda *a, **k: calls.append(1) or real(*a, **k))
    for _ in range(10):
        assert _list(client, box).status_code == 200
    assert calls == []
    db = SessionLocal()
    try:
        G.load(db)   # startup
    finally:
        db.close()
    assert len(calls) == 1


def test_startup_reloads_the_persisted_grant(client, box):
    from app.db import SessionLocal

    _install(client, box, *_grant(box["org"]))
    G._state.update(reason="missing", plan=None, issued_at=None, expires_at=None)
    db = SessionLocal()
    try:
        assert G.load(db)["ok"] is True
    finally:
        db.close()


def test_expiry_ends_live_reservations_and_deletes_nothing(client, box):
    from app.db import SessionLocal
    from app.models import Entitlement, Item, RoutingDecision

    _install(client, box, *_grant(box["org"], ttl=3600))
    db = SessionLocal()
    try:
        item = db.query(Item).first()
        db.add(RoutingDecision(item_id=item.id, kind="reservation", reason="r"))
        db.commit()
        ents = db.query(Entitlement).count()
        total = db.query(RoutingDecision).count()
        G.current(db, now=time.time() + 7200)
        rows = db.query(RoutingDecision).filter(RoutingDecision.kind == "reservation").all()
        assert rows and all(r.ended_reason == "plan grant expired" and r.ended_at for r in rows)
        assert db.query(RoutingDecision).count() == total
        assert db.query(Entitlement).count() == ents
        assert G.status()["reason"] == "expired"
    finally:
        db.close()


def test_hosted_still_reads_org_plan(client, box, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "hosted_mode", True)
    assert _list(client, box).status_code == 200   # org.plan == team, no grant installed


def test_api_key_gets_401_on_install(client, box):
    key = client.post("/api/api-keys", json={"name": "k"}, headers=box["owner"]).json()
    r = client.post("/api/platform/plan-grant", json=_body(*_grant(box["org"])),
                    headers={"X-API-Key": key["plaintext"]})
    assert r.status_code == 401
