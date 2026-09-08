"""A wave can be scoped to one PRD (GRPH-797).

Reported from a real run on super-arc: `gbfleet until` immediately delegated an epic and
three items from outside the PRD the operator was working on. There was no flag to prevent
it, and no arrangement of the backlog that would have — `backlog` is claimable BY DESIGN
(GRPH-397, so a crashed agent's work is offered again), which is exactly why "I'll leave it in
backlog" is not a defence and the lever had to go here.

`_is_claimable` is deliberately untouched. Narrowing it to fix this would have made abandoned
work invisible to the divvy all over again.
"""
import pytest

from app.services import collision as collision_svc


@pytest.fixture()
def db(client):
    """Its own session, opened after the app's lifespan has seeded — the reason every other
    suite here depends on `client` for a session too."""
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _item(client, auth, title, *, prd_id=None, touchpoints=None):
    body = {"title": title, "project_id": "core", "touchpoints": touchpoints or [title]}
    if prd_id:
        body["prd_id"] = prd_id
    r = client.post("/api/items", json=body, headers=auth)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def test_a_scoped_pool_carries_only_that_prds_work(client, auth, db):
    mine = _item(client, auth, "in the prd", prd_id="prd-a", touchpoints=["a.py"])
    _item(client, auth, "someone else's", prd_id="prd-b", touchpoints=["b.py"])
    _item(client, auth, "no prd at all", touchpoints=["c.py"])

    clusters = collision_svc.clusters_for_project(db, "core", prd_id="prd-a")

    got = {i for c in clusters for i in c.get("items", [])}
    assert mine in got
    assert len(got) == 1, "a scoped wave saw work from outside its PRD"


def test_unscoped_is_exactly_what_it_was(client, auth, db):
    """The regression that matters most: nobody passing --prd must see any change."""
    a = _item(client, auth, "one", prd_id="prd-a", touchpoints=["a.py"])
    b = _item(client, auth, "two", touchpoints=["b.py"])

    got = {i for c in collision_svc.clusters_for_project(db, "core") for i in c.get("items", [])}

    assert {a, b} <= got


def test_the_filter_runs_before_the_clusters_are_built(client, auth, db):
    """A cluster is a promise that its members do not collide. Filtering finished clusters
    would hand out a promise computed over items no longer in them."""
    _item(client, auth, "prd-a work", prd_id="prd-a", touchpoints=["shared.py"])
    _item(client, auth, "prd-b work", prd_id="prd-b", touchpoints=["shared.py"])

    clusters = collision_svc.clusters_for_project(db, "core", prd_id="prd-a")

    # Both items touch `shared.py`, so unfiltered they are ONE cluster. Scoped, the survivor
    # must be alone in its own — not a two-item cluster with one member removed.
    assert len(clusters) == 1
    assert len(clusters[0]["items"]) == 1


def test_an_empty_prd_scopes_to_nothing_rather_than_everything(client, auth, db):
    """The dangerous direction. A filter that fell back to "all" on a typo would drain the
    project while reporting that it was scoped."""
    _item(client, auth, "work", prd_id="prd-a", touchpoints=["a.py"])

    assert collision_svc.clusters_for_project(db, "core", prd_id="prd-typo") == []


# ---- who is told the filter exists ----------------------------------------------------------

def _manifest(client, key):
    r = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers={"X-API-Key": key})
    return {t["name"]: t for t in r.json()["result"]["tools"]}


def _props(manifest):
    return set(manifest["collision_clusters"]["inputSchema"].get("properties", {}))


def test_a_fleet_key_is_told_about_prd_id(client, auth):
    key = client.post("/api/api-keys", json={"name": "planner", "project_id": "core",
                                             "tool_tiers": ["fleet"]},
                      headers=auth).json()["plaintext"]

    assert "prd_id" in _props(_manifest(client, key))


def test_an_ordinary_agent_key_is_not(client, auth):
    """The whole reason it is gated: carried by every manifest it put the full surface 6
    tokens over the ceiling, and the pinned footprint test said so."""
    key = client.post("/api/api-keys", json={"name": "agent", "project_id": "core"},
                      headers=auth).json()["plaintext"]

    assert "prd_id" not in _props(_manifest(client, key))


def test_the_filter_still_works_for_a_key_that_was_never_told(client, auth):
    """ADVERTISEMENT, NEVER A BOUNDARY — the contract `_with_attestation` states and
    `tool_tiers` states. A manifest can only fail to mention a property; the dispatcher reads
    it from anyone who sends it. Getting this backwards would turn a token optimisation into
    an authorisation change, which is a far worse defect than the tokens it saved."""
    key = client.post("/api/api-keys", json={"name": "agent", "project_id": "core"},
                      headers=auth).json()["plaintext"]
    client.post("/api/items", json={"title": "scoped", "project_id": "core",
                                    "prd_id": "prd-a", "touchpoints": ["a.py"]}, headers=auth)
    client.post("/api/items", json={"title": "other", "project_id": "core",
                                    "touchpoints": ["b.py"]}, headers=auth)

    r = client.post("/api/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "collision_clusters", "arguments": {"prd_id": "prd-a"}}},
        headers={"X-API-Key": key})

    import json as _json
    got = _json.loads(r.json()["result"]["content"][0]["text"])
    assert got["total"] == 1, got


def test_the_manifest_does_not_leak_the_property_to_the_next_caller(client, auth):
    """`TOOLS` is module-level and shared. A widener that mutated it instead of deep-copying
    would put the property into every subsequent manifest — including the ordinary agents it
    exists to keep it away from — and the ceiling guard would only notice on a re-run."""
    fleet = client.post("/api/api-keys", json={"name": "planner", "project_id": "core",
                                               "tool_tiers": ["fleet"]},
                        headers=auth).json()["plaintext"]
    plain = client.post("/api/api-keys", json={"name": "agent", "project_id": "core"},
                        headers=auth).json()["plaintext"]

    assert "prd_id" in _props(_manifest(client, fleet))
    assert "prd_id" not in _props(_manifest(client, plain)), "the widener mutated TOOLS"
