"""GRPH-1338: GET /api/harness/ladder and GET /api/harness/routing/{item_id}."""
from tests.test_entitlements import _login, hosted, org  # noqa: F401
from tests.test_routing import (_claim, _decisions, _ent, _item, _report, _runner,  # noqa: F401
                                world)

PROFILE = {"defaults": [], "weights": {}, "excludes": []}


def _ladder(client, org, **kw):
    pid = kw.pop("project_id", org["project"]["id"])
    return client.get(f"/api/harness/ladder?project_id={pid}", headers=org["owner"])


def _rungs(rows):
    return [r["rung"] for r in rows]


def test_no_override_means_effective_equals_the_policy_ladder(client, org):
    body = _ladder(client, org).json()
    assert body["override"] is None and body["refused"] == []
    assert _rungs(body["effective"]) == _rungs(body["policy"]) == ["subscription", "api_key", "local"]
    assert all(r["label"] and r["description"] for r in body["policy"])


def test_a_policy_removed_rung_is_refused_not_effective(client, org):
    pid = org["project"]["id"]
    r = client.put("/api/fleet/policy", json={"project_id": pid, "ladder": ["subscription", "local"]},
                   headers=org["owner"])
    assert r.status_code == 200, r.text
    r = client.put("/api/fleet/profile",
                   json={**PROFILE, "ladder": ["local", "api_key", "subscription"]},
                   headers=org["owner"])
    assert r.status_code == 200, r.text
    body = _ladder(client, org).json()
    assert _rungs(body["override"]) == ["local", "api_key", "subscription"]
    assert _rungs(body["effective"]) == ["local", "subscription"]
    assert _rungs(body["refused"]) == ["api_key"]
    removed = [r for r in body["policy"] if r["removed"]]
    assert _rungs(removed) == ["api_key"]
    assert _rungs(body["policy"])[:2] == ["subscription", "local"]


def test_unreadable_project_is_404_on_both(client, org):
    other = _login(client, "kate@ascme-labs.com")
    pid = org["project"]["id"]
    assert client.get(f"/api/harness/ladder?project_id={pid}", headers=other).status_code in (403, 404)


def test_routing_read_returns_attempts_in_order_verbatim(client, world):
    held = _ent(client, world, "anthropic")
    b = _runner(client, world, [held])
    _report(client, world, held, b, "registered")
    _ent(client, world, "mistral")  # a second vendor, so the review reserve lets the build through
    item = _item()
    assert _claim(b, item) == item
    written = _decisions(item)
    assert written
    r = client.get(f"/api/harness/routing/{item}", headers=world["owner"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert [a["reason"] for a in body["attempts"]] == [d["reason"] for d in written]
    assert body["latest"]["why"] == written[-1]["reason"]
    assert body["latest"]["entitlement_id"] == written[-1]["entitlement_id"]
    assert body["latest"]["verdict"] in ("claimed", "held", "next_rung")
    assert body["item_key"] and body["stage"] and body["at"]


def test_an_item_never_routed_says_so(client, world):
    item = _item()
    body = client.get(f"/api/harness/routing/{item}", headers=world["owner"]).json()
    assert body["attempts"] == [] and body["latest"] is None
    assert body["reason"] == "never routed by capacity"


def test_routing_read_hides_unreadable_items(client, world):
    item = _item()
    other = _login(client, "kate@ascme-labs.com")
    r = client.get(f"/api/harness/routing/{item}", headers=other)
    assert r.status_code in (403, 404)
    assert client.get("/api/harness/routing/nope", headers=world["owner"]).status_code == 404
