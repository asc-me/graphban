"""`GBAGENT_API_KEY` reaches the model endpoint as a bearer, and only from the environment.

The seat file carries the Graphban server's key; the model endpoint had none, so gbagent could only
speak to an unauthenticated endpoint — a local Ollama. A cloud OpenAI-compatible host wants a token.
The fleet's rule (fleet-adapters.md) is that nothing carrying a credential goes on argv, so the key is
an environment variable the supervisor's child inherits, never a flag.
"""
from __future__ import annotations

import httpx

from gbagent import cli
from gbagent.llm import OllamaSession


def test_the_key_is_read_from_the_environment_and_nowhere_else(monkeypatch):
    monkeypatch.delenv(cli.API_KEY_ENV, raising=False)
    assert cli.endpoint_key() == ""

    monkeypatch.setenv(cli.API_KEY_ENV, "sk-test")
    assert cli.endpoint_key() == "sk-test"

    # No flag: a credential on argv would show in `ps`.
    parser = cli.build_parser()
    flags = {a for action in parser._subparsers._group_actions for sub in action.choices.values()
             for a in sub._option_string_actions}
    assert not any("key" in f for f in flags), flags


def test_models_sends_the_bearer_when_the_key_is_set(monkeypatch):
    seen: dict[str, str] = {}

    class Recording(OllamaSession):
        def __init__(self, base_url, model, **kw):
            seen["api_key"] = kw.get("api_key", "<missing>")
            super().__init__(base_url, model, **kw)

        def list_models(self):
            return ["m"]

    monkeypatch.setattr(cli, "OllamaSession", Recording)
    monkeypatch.setenv(cli.API_KEY_ENV, "sk-models")

    assert cli._models("http://endpoint/v1") == ["m"]
    assert seen["api_key"] == "sk-models"


def test_the_session_puts_the_key_in_the_authorization_header():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"data": [{"id": "qwen3.8-max"}]})

    session = OllamaSession("http://endpoint/v1", "", system="", task="", api_key="sk-header",
                            transport=httpx.MockTransport(handler))
    try:
        session.list_models()
    finally:
        session.close()
    assert captured["auth"] == "Bearer sk-header"


def test_no_key_means_no_authorization_header():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"models": [{"name": "local"}]})

    session = OllamaSession("http://localhost:11434", "", system="", task="",
                            transport=httpx.MockTransport(handler))
    try:
        session.list_models()
    finally:
        session.close()
    assert captured["auth"] == ""
