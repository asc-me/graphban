"""A misconfigured AI provider says so (follow-up to two lost debugging sessions).

Every provider failure used to reach the agent as::

    internal: internal error executing 'grill_prd'
    hint:     safe to retry once; if it persists, report it

which is worse than useless for a misconfiguration. Retrying a refused connection
never helps, and the hint sends the agent to file a bug rather than open Settings. It
cost two separate sessions before anyone saw the cause — first a model name that did
not exist on the endpoint, then, on a different project, a base URL of
`http://localhost:11434`, which inside the API container is the container itself.

The bar these tests set: the error must name what is wrong, and must NOT tell the
caller to retry something that cannot succeed.
"""
import httpx
import pytest

from app import errors
from app.providers.base import provider_errors
from app.services.platform import Resolved


def _raise(exc):
    with provider_errors("ollama", model="mistral-small3.1:24b", endpoint="http://ms-s1-ubt:11434"):
        raise exc


def _response(status: int, body: str = "") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://ms-s1-ubt:11434/api/chat")
    return httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(status, text=body, request=request)
    )


# ---- the two failures that actually happened ----------------------------------------
def test_an_unreachable_endpoint_names_the_endpoint_and_the_container_trap(client):
    """The super-arc case. `localhost` inside the container is the container — the one
    piece of knowledge that turns a 20-minute hunt into a 20-second fix."""
    with pytest.raises(errors.Unavailable) as e:
        _raise(httpx.ConnectError("Connection refused"))

    assert "ms-s1-ubt" in str(e.value)
    assert "localhost" in e.value.hint and "CONTAINER" in e.value.hint
    assert "will not help" in e.value.hint


def test_a_missing_model_names_the_model_and_how_to_get_it(client):
    """The first case: `qwen3-coder` configured, `qwen3-coder:30b` installed. Ollama
    answers 404 with the model name in the body."""
    with pytest.raises(errors.Unavailable) as e:
        _raise(_response(404, '{"error":"model mistral-small3.1:24b not found"}'))

    assert "mistral-small3.1:24b" in str(e.value)
    assert "ollama pull" in e.value.hint
    assert "will not help" in e.value.hint


# ---- the distinction the old message erased ------------------------------------------
def test_a_timeout_is_the_one_case_worth_retrying(client):
    """Not everything is a misconfiguration. A cold model genuinely is worth another go,
    and the hint has to distinguish that from the cases that never will be."""
    with pytest.raises(errors.Unavailable) as e:
        _raise(httpx.ReadTimeout("too slow"))

    assert "timed out" in str(e.value)
    assert "worth retrying" in e.value.hint


def test_other_http_statuses_point_at_credentials(client):
    with pytest.raises(errors.Unavailable) as e:
        _raise(_response(401, "invalid api key"))

    assert "401" in str(e.value)
    assert "credentials" in e.value.hint


def test_a_404_that_is_not_about_the_model_is_not_mislabelled(client):
    """A wrong path should not be reported as a missing model — that would send someone
    pulling a model they already have."""
    with pytest.raises(errors.Unavailable) as e:
        _raise(_response(404, "404 page not found"))

    assert "ollama pull" not in (e.value.hint or "")


def test_success_passes_through_untouched(client):
    with provider_errors("ollama", model="m", endpoint="http://x"):
        result = "fine"
    assert result == "fine"


# ---- and the agent-facing surface ----------------------------------------------------
def test_the_mcp_error_is_unavailable_not_internal(client, auth, monkeypatch):
    """What the agent actually sees. `unavailable` tells it to stop and look at config;
    `internal` + "safe to retry once" told it to bang on the door again."""
    import json

    from app.services import prds as prd_svc

    prd = client.post("/api/prds", json={"title": "P", "project_id": "core"},
                      headers=auth).json()["id"]

    class _Dead:
        model = "mistral-small3.1:24b"
        base_url = "http://ms-s1-ubt:11434"

        def chat(self, **kw):
            with provider_errors("ollama", model=self.model, endpoint=self.base_url):
                raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr(prd_svc.platform_svc, "resolve_chat", lambda db, pid: Resolved("ollama", _Dead()))
    key = client.post("/api/api-keys", json={"name": "a", "scopes": ["read"]},
                      headers=auth).json()["plaintext"]

    r = client.post("/api/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "grill_prd", "arguments": {"prd_id": prd}}},
                    headers={"X-API-Key": key})
    err = r.json()["result"]["structuredContent"]["error"]
    assert err["code"] == "unavailable", err
    assert "ms-s1-ubt" in err["message"], err
    assert "safe to retry once" not in json.dumps(err)
