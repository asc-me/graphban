"""AI provider registry — list, per-provider config (write-only keys), and live resolution.
No network: we assert on the constructed adapter, never call .chat()."""
import app.providers as providers
from app.providers.openai_compat import OpenAICompatChat
from app.services.platform import Resolved


def test_provider_registry_endpoint(client, auth):
    r = client.get("/api/platform/providers", headers=auth)
    assert r.status_code == 200
    ids = {p["id"] for p in r.json()["providers"]}
    assert {"stub", "anthropic", "openai", "ollama", "groq", "deepseek", "mistral", "xai", "gemini"} <= ids


def test_set_active_provider_config_redacted_and_live(client, auth):
    r = client.patch(
        "/api/platform",
        json={
            "active_chat_provider": "openai",
            "providers": {"openai": {"api_key": "sk-test-123", "chat_model": "gpt-4o-mini",
                                     "base_url": "https://api.openai.com/v1"}},
        },
        headers=auth,
    )
    assert r.status_code == 200
    cfg = r.json()
    assert cfg["active_chat_provider"] == "openai"
    pc = cfg["provider_config"]["openai"]
    assert pc["key_set"] is True and pc["chat_model"] == "gpt-4o-mini"
    assert "api_key" not in pc  # redacted — never returned raw
    # drives the live provider layer
    assert providers._active["provider"] == "openai"
    assert providers._active["api_key"] == "sk-test-123"


def test_provider_key_is_write_only(client, auth):
    client.patch("/api/platform", json={
        "active_chat_provider": "openai",
        "providers": {"openai": {"api_key": "sk-keep", "chat_model": "gpt-4o-mini"}},
    }, headers=auth)
    # change the model with a blank key → the stored key survives
    r = client.patch("/api/platform", json={"providers": {"openai": {"chat_model": "gpt-4o", "api_key": ""}}}, headers=auth)
    pc = r.json()["provider_config"]["openai"]
    assert pc["key_set"] is True and pc["chat_model"] == "gpt-4o"
    assert providers._active["api_key"] == "sk-keep"


def test_ollama_rich_config_drives_provider(client, auth):
    r = client.patch("/api/platform", json={
        "active_chat_provider": "ollama",
        "providers": {"ollama": {"base_url": "https://ollama.example.ts.net", "chat_model": "qwen2.5",
                                 "embed_model": "nomic-embed-text", "api_key": "caddy-bearer"}},
    }, headers=auth)
    assert r.status_code == 200
    cm = providers.get_chat_model()
    assert cm.base_url == "https://ollama.example.ts.net"
    assert cm.model == "qwen2.5"
    assert cm.auth_key == "caddy-bearer"


def test_openai_compat_provider_uses_registry_default_base(client, auth):
    client.patch("/api/platform", json={
        "active_chat_provider": "groq",
        "providers": {"groq": {"api_key": "gk", "chat_model": "llama-3.3-70b-versatile"}},
    }, headers=auth)
    cm = providers.get_chat_model()
    assert isinstance(cm, OpenAICompatChat)
    assert cm.base_url == "https://api.groq.com/openai/v1"  # default from the registry
    assert cm.model == "llama-3.3-70b-versatile" and cm.api_key == "gk"


def test_unknown_provider_rejected(client, auth):
    assert client.patch("/api/platform", json={"active_chat_provider": "nope"}, headers=auth).status_code == 422


def test_configured_provider_reaches_prd_ai(client, auth, monkeypatch):
    """Regression (AL-148): when a real provider resolves for a PRD's project, the AI
    command uses the model — not the offline stub. The gate used to key off a stale
    app_settings.chat_provider flag the registry path never updated."""
    class FakeChat:
        def chat(self, *, system, context, question, temperature=None):
            return "REAL-MODEL-OUTPUT"

    monkeypatch.setattr("app.services.platform.resolve_chat",
                        lambda db, pid: Resolved("openai", FakeChat()))
    r = client.post("/api/prds/PRD-1/ai", json={"command": "risks"}, headers=auth).json()
    assert r["text"] == "REAL-MODEL-OUTPUT"  # real model, not _stub_command()


def test_chat_provider_resolves_per_project(client, auth):
    """Regression (AL-148 multi-project): configuring one project's provider must not
    change another's. The old process-global providers._active held a single provider, so
    the last project saved (or the alphabetically-first applied at startup) leaked into
    every project's AI calls."""
    client.post("/api/projects", json={"name": "Glyph"}, headers=auth)  # -> id "glyph"
    client.patch("/api/platform?project_id=glyph", json={
        "active_chat_provider": "openai",
        "providers": {"openai": {"api_key": "sk-x", "chat_model": "gpt-4o-mini"}},
    }, headers=auth)

    # glyph was configured last (old global would now point everything at openai), but
    # core (seeded stub) must still resolve to the stub — per project, not per process.
    assert client.get("/api/platform?project_id=core", headers=auth).json()["effective_chat_provider"] == "stub"
    assert client.get("/api/platform?project_id=glyph", headers=auth).json()["effective_chat_provider"] == "openai"

    # End-to-end: a core PRD command still uses the offline stub despite glyph's openai.
    r = client.post("/api/prds/PRD-1/ai", json={"command": "risks"}, headers=auth).json()
    assert "local stub" in r["text"].lower()


# ---- GRPH-625: the catalogue grows past the first five labs ------------------------------


def test_the_compat_family_and_the_custom_shape_are_shipped(client, auth):
    """The CN labs, the hosted open-weights providers, and one generic shape.

    **Kind membership is the contract, not a label** — `openai` kind is what makes an entry
    probeable (LISTS_MODELS), what the Settings form reads the endpoint default from, and
    what reuses the compat adapter with zero new transport code. An entry with the wrong
    kind would render in the picker and then answer "cannot be asked" to every save.
    """
    from app.providers import registry

    ids = {p["id"]: p for p in client.get("/api/platform/providers", headers=auth).json()["providers"]}
    for pid in ("qwen", "kimi", "glm", "minimax", "openrouter", "together",
                "fireworks", "perplexity", "cohere", "custom"):
        assert pid in ids, f"{pid} is not in the shipped catalog"
        assert ids[pid]["kind"] == "openai", f"{pid} is not the compat kind"
    # `custom` IS the generic shape: empty URL and no default model are what make the form
    # ask for them. A catalogue default here would pre-fill a lie about someone's gateway.
    assert ids["custom"]["base_url"] == "" and ids["custom"]["chat_model"] == ""
    assert registry.is_openai_compat("custom") and "custom" in registry.LISTS_MODELS


def test_a_compat_credential_is_probed_at_the_endpoint_it_names(client, auth, monkeypatch):
    """Saving `qwen` must probe QWEN's url, not api.openai.com. The registry default is
    carried by the form, but the probe is what proves it arrived on the credential."""
    from app.services import platform as platform_svc

    seen: list[tuple] = []

    def fake(pid, base_url, api_key=""):
        seen.append((pid, base_url))
        return {"qwen-plus"} if pid == "qwen" else None

    monkeypatch.setattr(platform_svc.probe, "known_models", fake)
    r = client.post("/api/platform/credentials?project_id=core",
                    json={"kind": "qwen", "label": "DashScope",
                          "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                          "api_key": "sk-x", "model": "qwen-plus"}, headers=auth)
    assert r.status_code == 201, r.text
    assert r.json()["state"] == "valid"
    assert seen and seen[0][0] == "qwen" and "dashscope" in seen[0][1]


def test_a_wrong_model_guess_surfaces_as_a_list_not_a_silence(client, auth, monkeypatch):
    """The registry defaults are best-known-at-filing (GRPH-625 says so in the file). The
    contract that makes a wrong guess survivable is this: the provider answers, the save is
    refused, and the refusal names what IS offered. If this ever returns pending_validation
    instead, the probe stopped seeing the model check and every wrong default below turns
    into a runtime failure at the call site — the GRPH-485 incident, un-fixed."""
    from app.services import platform as platform_svc

    monkeypatch.setattr(platform_svc.probe, "known_models",
                        lambda *a, **k: frozenset({"kimi-latest", "kimi-k3"}))
    r = client.post("/api/platform/credentials?project_id=core",
                    json={"kind": "kimi", "base_url": "https://api.moonshot.ai/v1",
                          "api_key": "sk-x", "model": "kimi-nope-2027"}, headers=auth)
    assert r.status_code == 422
    assert "kimi-latest" in r.json()["detail"]


def test_an_unreachable_custom_endpoint_saves_pending_not_refused(client, auth, monkeypatch):
    """A vLLM on a laptop during a VPN blip must still be a saveable credential —
    `pending_validation` is the honest state, and refusing the save would be refusing the
    network rather than the config (the contract probe.py documents)."""
    from app.services import platform as platform_svc

    monkeypatch.setattr(platform_svc.probe, "known_models", lambda *a, **k: None)
    r = client.post("/api/platform/credentials?project_id=core",
                    json={"kind": "custom", "base_url": "http://localhost:1234/v1",
                          "api_key": "none", "model": "qwen2.5"}, headers=auth)
    assert r.status_code == 201
    assert r.json()["state"] == "pending_validation"


def test_the_custom_probe_lands_on_the_url_the_form_named(client, auth):
    """THE CALL (GRPH-625). Monkeypatching `known_models` stays green if create_credential
    never asks the host — the helper is correct and nobody calls it with the form URL.
    A local server that records GET /v1/models is the request, not the helper."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            body = json.dumps({"data": [{"id": "qwen2.5"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/v1"
        r = client.post(
            "/api/platform/credentials?project_id=core",
            json={"kind": "custom", "label": "laptop vLLM",
                  "base_url": url, "api_key": "none", "model": "qwen2.5"},
            headers=auth,
        )
        assert r.status_code == 201, r.text
        assert r.json()["state"] == "valid"
        assert "/v1/models" in hits, f"probe never reached the named host; hits={hits}"
    finally:
        httpd.shutdown()
        httpd.server_close()
