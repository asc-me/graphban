"""PRD-21 D3: the super galaxy — an edge between repos must name the file that proves it.

The assertions here are mostly about what must NOT happen: no edge without evidence, no
edge from a guess, no silent drop, and above all no protocol in which an old client
deletes a dependency graph by staying quiet.
"""
import pytest

SEED_PW = "graphban"


def _login(client, email="alex@ascme-labs.com", password=SEED_PW):
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def hosted(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "hosted_mode", True)
    return settings


@pytest.fixture()
def org(client, hosted):
    """An org with two projects: `web` depends on `core`, once a push says so."""
    auth = _login(client)
    org = client.post("/api/orgs", json={"name": "Acme"}, headers=auth).json()
    core = client.post("/api/projects", json={"name": "Core"}, headers=auth).json()
    web = client.post("/api/projects", json={"name": "Web"}, headers=auth).json()
    return {"auth": auth, "org": org, "core": core, "web": web}


def _sync_key(client, auth, project_id):
    r = client.post("/api/api-keys", headers=auth,
                    json={"name": "pusher", "scopes": ["read", "sync"], "project_id": project_id})
    assert r.status_code == 201, r.text
    return r.json()["plaintext"]


def _push(client, key, **body):
    return client.post("/api/sync/code-graph", json=body, headers={"X-API-Key": key})


def _galaxy(client, auth, org_id):
    r = client.get(f"/api/orgs/{org_id}/galaxy", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


# ---- resolution ----------------------------------------------------------------
def test_a_manifest_name_resolves_to_the_sibling_that_publishes_it(client, org):
    core_key = _sync_key(client, org["auth"], org["core"]["id"])
    web_key = _sync_key(client, org["auth"], org["web"]["id"])

    _push(client, core_key, provides=["@acme/core"], manifests=[])
    r = _push(client, web_key, provides=["@acme/web"], manifests=[
        {"name": "@acme/core",
         "evidence": [{"file": "web/package.json", "fact": "@acme/core ^2.1"}]},
    ])
    assert r.status_code == 200, r.text
    assert r.json()["galaxy"]["resolved"] == 1

    g = _galaxy(client, org["auth"], org["org"]["id"])
    edge = next(e for e in g["edges"])
    assert edge["src"] == org["web"]["id"] and edge["dst"] == org["core"]["id"]
    assert edge["kind"] == "depends_on"
    assert edge["evidence"] == [{"file": "web/package.json", "fact": "@acme/core ^2.1"}]
    assert edge["fresh"] is True


def test_an_edge_without_evidence_is_refused(client, org):
    """The whole difference between this graph and a guess. 422, not a silent drop."""
    key = _sync_key(client, org["auth"], org["web"]["id"])
    r = _push(client, key, manifests=[{"name": "@acme/core", "evidence": []}])
    assert r.status_code == 422
    assert "evidence" in r.json()["detail"]


def test_an_external_package_is_dropped_but_counted(client, org):
    """A silent drop with no count is the failure this codebase keeps finding."""
    key = _sync_key(client, org["auth"], org["web"]["id"])
    r = _push(client, key, manifests=[
        {"name": "react", "evidence": [{"file": "web/package.json", "fact": "react ^19"}]},
        {"name": "lodash", "evidence": [{"file": "web/package.json", "fact": "lodash ^4"}]},
    ])
    body = r.json()["galaxy"]
    assert body["resolved"] == 0
    assert body["external"] == 2
    assert set(body["external_names"]) == {"react", "lodash"}
    assert _galaxy(client, org["auth"], org["org"]["id"])["edges"] == []


def test_a_name_two_projects_claim_draws_nothing_and_is_reported(client, org):
    """An ambiguous name is a coin flip, and this graph does not guess. It must also not
    quietly resolve to whichever row came back first."""
    core_key = _sync_key(client, org["auth"], org["core"]["id"])
    web_key = _sync_key(client, org["auth"], org["web"]["id"])
    _push(client, core_key, provides=["@acme/shared"], manifests=[])
    # Web publishes the same name AND declares a dependency on it. The collision is what
    # drops the edge — resolution never gets as far as picking one.
    r = _push(client, web_key, provides=["@acme/shared"], manifests=[
        {"name": "@acme/shared", "evidence": [{"file": "web/package.json", "fact": "@acme/shared ^1"}]},
    ])
    assert r.json()["galaxy"]["resolved"] == 0
    assert r.json()["galaxy"]["external"] == 1

    g = _galaxy(client, org["auth"], org["org"]["id"])
    assert g["edges"] == []
    collision = next(c for c in g["collisions"] if c["name"] == "@acme/shared")
    assert set(collision["project_ids"]) == {org["core"]["id"], org["web"]["id"]}


def test_a_project_depending_on_a_name_it_publishes_makes_no_self_edge(client, org):
    """A monorepo's internal package relationships belong in its code graph. The galaxy's
    resolution is the checkout, not the package."""
    key = _sync_key(client, org["auth"], org["core"]["id"])
    r = _push(client, key, provides=["@acme/core"], manifests=[
        {"name": "@acme/core", "evidence": [{"file": "pkg/a/package.json", "fact": "@acme/core"}]},
    ])
    assert r.json()["galaxy"]["resolved"] == 0
    assert _galaxy(client, org["auth"], org["org"]["id"])["edges"] == []


# ---- the distinction that would be permanent if wrong ---------------------------
def test_an_omitted_manifests_block_stales_nothing(client, org):
    """An older client that did not look must not delete the graph by staying quiet.

    Collapsing omitted and empty writes absence-reads-as-clean into a wire format, where
    it is far harder to dig out than a bad test: EVERY old client would silently drop the
    dependencies of every project it pushed.
    """
    core_key = _sync_key(client, org["auth"], org["core"]["id"])
    web_key = _sync_key(client, org["auth"], org["web"]["id"])
    _push(client, core_key, provides=["@acme/core"], manifests=[])
    _push(client, web_key, manifests=[
        {"name": "@acme/core", "evidence": [{"file": "web/package.json", "fact": "^2.1"}]},
    ])

    r = _push(client, web_key, nodes=[], edges=[])  # no `manifests` key at all
    assert r.json()["galaxy"]["looked"] is False
    assert r.json()["galaxy"]["edges_marked_stale"] == 0
    assert _galaxy(client, org["auth"], org["org"]["id"])["edges"][0]["fresh"] is True


def test_an_empty_manifests_block_stales_the_edges(client, org):
    """Looked, found none — a fact, and a different one from not having looked."""
    core_key = _sync_key(client, org["auth"], org["core"]["id"])
    web_key = _sync_key(client, org["auth"], org["web"]["id"])
    _push(client, core_key, provides=["@acme/core"], manifests=[])
    _push(client, web_key, manifests=[
        {"name": "@acme/core", "evidence": [{"file": "web/package.json", "fact": "^2.1"}]},
    ])

    r = _push(client, web_key, manifests=[])
    assert r.json()["galaxy"]["looked"] is True
    assert r.json()["galaxy"]["edges_marked_stale"] == 1

    edge = _galaxy(client, org["auth"], org["org"]["id"])["edges"][0]
    assert edge["fresh"] is False
    # Stale, not deleted, and its evidence is NOT trimmed — a relationship with no
    # explanation is worse than a deleted one.
    assert edge["evidence"] == [{"file": "web/package.json", "fact": "^2.1"}]


def test_a_stale_edge_comes_back_fresh_when_redeclared(client, org):
    core_key = _sync_key(client, org["auth"], org["core"]["id"])
    web_key = _sync_key(client, org["auth"], org["web"]["id"])
    _push(client, core_key, provides=["@acme/core"], manifests=[])
    dep = [{"name": "@acme/core", "evidence": [{"file": "web/package.json", "fact": "^2.1"}]}]
    _push(client, web_key, manifests=dep)
    _push(client, web_key, manifests=[])
    _push(client, web_key, manifests=dep)

    edges = _galaxy(client, org["auth"], org["org"]["id"])["edges"]
    assert len(edges) == 1 and edges[0]["fresh"] is True


# ---- the read ------------------------------------------------------------------
def test_nodes_separate_never_pushed_from_pushed_but_unconnected(client, org):
    """Two of the three empty states. An org with projects that have pushed nothing is not
    an empty org — the EDGES are empty, and the nodes must still be drawn."""
    g = _galaxy(client, org["auth"], org["org"]["id"])
    assert len(g["nodes"]) == 2
    assert all(n["pushed"] is False for n in g["nodes"])
    assert g["edges"] == []


def test_the_galaxy_is_org_scoped(client, org):
    """Another tenant's projects are not in this graph, whatever they publish."""
    dana = _login(client, "dana@ascme-labs.com")
    client.post("/api/orgs", json={"name": "Dana Co"}, headers=dana)
    client.post("/api/projects", json={"name": "Theirs"}, headers=dana)

    names = {n["name"] for n in _galaxy(client, org["auth"], org["org"]["id"])["nodes"]}
    assert names == {"Core", "Web"}
    # 404, not 403 — the org gate hides existence rather than confirming it.
    assert client.get(f"/api/orgs/{org['org']['id']}/galaxy", headers=dana).status_code == 404


# ---- D4: the arrows out ---------------------------------------------------------
def _map(client, auth, project_id):
    r = client.get(f"/api/agent/code/map?project_id={project_id}", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def test_the_arrow_out_anchors_on_the_file_that_declares_it(client, org):
    """The payoff for D3 being strict: because the edge had to name a file, the project
    view can attach the arrow to the REAL node for that file rather than float it."""
    core_key = _sync_key(client, org["auth"], org["core"]["id"])
    web_key = _sync_key(client, org["auth"], org["web"]["id"])
    _push(client, core_key, provides=["@acme/core"], manifests=[])
    _push(client, web_key, manifests=[
        {"name": "@acme/core", "evidence": [{"file": "web/package.json", "fact": "^2.1"}]},
    ], nodes=[
        {"path": "web/package.json", "kind": "config", "name": "manifest", "summary": "deps"},
    ])

    stubs = _map(client, org["auth"], org["web"]["id"])["outbound"]
    assert len(stubs) == 1
    assert stubs[0]["tag"] == "CORE"
    assert stubs[0]["anchor_paths"] == ["web/package.json"]
    assert stubs[0]["unanchored"] is False
    assert stubs[0]["evidence"] == [{"file": "web/package.json", "fact": "^2.1"}]


def test_an_arrow_whose_file_was_never_described_says_so(client, org):
    """The manifest can name a file the code graph has never described. The arrow is
    real; its anchor is missing. Hiding it would lose a dependency, and drawing it from
    nowhere with no explanation is what D3 exists to prevent — so it renders and says why."""
    core_key = _sync_key(client, org["auth"], org["core"]["id"])
    web_key = _sync_key(client, org["auth"], org["web"]["id"])
    _push(client, core_key, provides=["@acme/core"], manifests=[])
    _push(client, web_key, manifests=[
        {"name": "@acme/core", "evidence": [{"file": "web/package.json", "fact": "^2.1"}]},
    ])  # no nodes pushed, so nothing describes web/package.json

    stubs = _map(client, org["auth"], org["web"]["id"])["outbound"]
    assert len(stubs) == 1
    assert stubs[0]["anchor_paths"] == []
    assert stubs[0]["unanchored"] is True


def test_a_stale_edge_still_draws_an_arrow_marked_stale(client, org):
    core_key = _sync_key(client, org["auth"], org["core"]["id"])
    web_key = _sync_key(client, org["auth"], org["web"]["id"])
    _push(client, core_key, provides=["@acme/core"], manifests=[])
    _push(client, web_key, manifests=[
        {"name": "@acme/core", "evidence": [{"file": "web/package.json", "fact": "^2.1"}]},
    ])
    _push(client, web_key, manifests=[])

    stubs = _map(client, org["auth"], org["web"]["id"])["outbound"]
    assert len(stubs) == 1 and stubs[0]["fresh"] is False


def test_a_project_outside_an_org_has_no_arrows_rather_than_an_error(client, auth):
    """Self-host, and any project with no org: there are no siblings to depend on. That
    is different from having none, and it is an empty list, never a failure."""
    assert _map(client, auth, "core")["outbound"] == []


# ---- D6: linked deployments -----------------------------------------------------
def _deployments(client, auth, org_id):
    r = client.get(f"/api/orgs/{org_id}/deployments", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_deployment_is_named_by_its_credential(client, org):
    """The cloud stores no other deployment identity, which is what makes naming the key
    at mint time load-bearing — the name IS the label everywhere in the console."""
    key = _sync_key(client, org["auth"], org["core"]["id"])
    _push(client, key, base_url="http://ubuntu-srv:8080", nodes=[
        {"path": "a.py", "kind": "file", "name": "a", "summary": "x"},
    ])

    rows = _deployments(client, org["auth"], org["org"]["id"])
    row = next(r for r in rows if r["project_tag"] == "CORE")
    assert row["label"] == "pusher"
    assert row["base_url"] == "http://ubuntu-srv:8080"
    assert row["node_count"] == 1
    assert row["freshness"] == "in_sync"


def test_never_pushed_is_not_the_same_as_stale(client, org):
    """A credential that never pushed is a link somebody set up and did not finish; one
    that pushed a month ago is a box that stopped. Different actions, different words."""
    _sync_key(client, org["auth"], org["core"]["id"])
    row = next(r for r in _deployments(client, org["auth"], org["org"]["id"])
               if r["project_tag"] == "CORE")
    assert row["freshness"] == "never"
    assert row["last_push_at"] is None
    assert row["node_count"] == 0


def test_an_unreported_address_is_empty_rather_than_invented(client, org):
    """The cloud cannot know where a box lives until the box says so."""
    key = _sync_key(client, org["auth"], org["core"]["id"])
    _push(client, key, nodes=[])  # no base_url in the payload
    row = next(r for r in _deployments(client, org["auth"], org["org"]["id"])
               if r["project_tag"] == "CORE")
    assert row["base_url"] == ""


def test_only_sync_credentials_are_deployments(client, org):
    """An agent key is a client, not a box. Listing it here would invent a deployment."""
    client.post("/api/api-keys", headers=org["auth"],
                json={"name": "an agent", "scopes": ["read", "write"],
                      "project_id": org["core"]["id"]})
    labels = {r["label"] for r in _deployments(client, org["auth"], org["org"]["id"])}
    assert "an agent" not in labels


def test_deployments_are_org_scoped(client, org, hosted):
    dana = _login(client, "dana@ascme-labs.com")
    client.post("/api/orgs", json={"name": "Dana Co"}, headers=dana)
    assert client.get(f"/api/orgs/{org['org']['id']}/deployments",
                      headers=dana).status_code == 404


def test_a_revoked_credential_stays_visible(client, org):
    """A retired deployment is history, not noise. Dropping the row would make a box that
    was deliberately unlinked indistinguishable from one that never existed.

    Revoked here means the SOFT kill switch — what the fleet sweeps set (`end_wave`,
    `revoke-expired`). Note the gap: `DELETE /api/api-keys/{id}` hard-deletes the row, so a
    credential retired that way does vanish from this list. The design asks for retired
    deployments to stay visible; only the soft path currently delivers that.
    """
    from app.db import SessionLocal
    from app.models import ApiKey
    from sqlalchemy import select

    key = _sync_key(client, org["auth"], org["core"]["id"])
    _push(client, key, nodes=[])

    db = SessionLocal()
    try:
        row = db.scalar(select(ApiKey).where(ApiKey.name == "pusher"))
        row.revoked = True
        kid = row.id
        db.commit()
    finally:
        db.close()

    row = next((r for r in _deployments(client, org["auth"], org["org"]["id"])
                if r["credential_id"] == kid), None)
    assert row is not None and row["revoked"] is True
