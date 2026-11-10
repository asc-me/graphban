"""GRPH-1456: org routes the console serves are gone from the product. The ones it does
not serve stay, so those screens still have an API."""
from app.main import app

_RETIRED = {
    "/api/orgs/{org_id}/rule-toggles",
    "/api/orgs/{org_id}/rule-toggles/{rule_id}",
    "/api/orgs/{org_id}/rules/profiles",
    "/api/orgs/{org_id}/rules/profiles/applied",
    "/api/orgs/{org_id}/rules/profiles/{profile_id}/apply",
    "/api/orgs/{org_id}/rules/sync",
}

# The console's feature flags mark these unserved. Removing them would leave the
# product screens with no API. Project-level /rules/* stay as well.
_KEPT = {
    "/api/orgs/{org_id}/rules/observations",
    "/api/orgs/{org_id}/rules/effect",
    "/api/orgs/{org_id}/rules/contribution",
    "/api/orgs/{org_id}/rules/platform-effect",
    "/api/orgs/{org_id}/rules/profiles/updates",
    "/api/orgs/{org_id}/rules/profiles/{profile_id}/updates/accept",
    "/api/orgs/{org_id}/rules/profiles/{profile_id}/drift",
    "/api/orgs/{org_id}/rules/recommended",
    "/api/orgs/{org_id}/rules/recommended/dismissed",
    "/api/orgs/{org_id}/rules/recommended/{suggestion_id}/dismiss",
    "/api/rules/toggles",
    "/api/rules/toggles/{rule_id}",
    "/api/rules/profiles/{profile_id}/apply",
}


def _paths():
    # Included routers stay collapsed on app.routes until OpenAPI flattens them.
    return set(app.openapi()["paths"])


def test_console_served_org_routes_are_gone():
    paths = _paths()
    assert _RETIRED.isdisjoint(paths)


def test_unserved_org_routes_stay():
    paths = _paths()
    missing = _KEPT - paths
    assert not missing, missing


def test_retired_paths_404(client):
    org = "org_retired"
    calls = [
        ("GET", f"/api/orgs/{org}/rule-toggles"),
        ("PUT", f"/api/orgs/{org}/rule-toggles/review.sabotage_required"),
        ("GET", f"/api/orgs/{org}/rules/profiles"),
        ("GET", f"/api/orgs/{org}/rules/profiles/applied"),
        ("POST", f"/api/orgs/{org}/rules/profiles/standard/apply"),
        ("GET", f"/api/orgs/{org}/rules/sync"),
    ]
    for method, path in calls:
        response = client.request(method, path, json={})
        assert response.status_code == 404, (method, path, response.status_code, response.text)
