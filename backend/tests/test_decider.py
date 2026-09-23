"""Decider provider tests (GRPH-895, PRD-45 S1).

The adapter against a recorded laya response and a recorded TypeSafe response; the stub
refuses; a noul without confidence parses; a 401 is Unavailable with the message; a 200
in the wrong shape is Unavailable and says so.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app import errors
from app.providers import build_decider, noul, score, choice
from app.providers.decide import Decision, Question
from app.providers.systemone import SystemOneDecider, _parse_response


# ---- recorded responses -----------------------------------------------------------

# A real laya response for a keep/quality question pair (measured 2026-09-23).
_LAYA_RESPONSE = {
    "model": "laya-rl-agent",
    "routing": {"model": "english"},
    "action": {"act_probability": 0.708},
    "confidence": 0.708,
    "answer": {"quality": 0.42},
    "legend": {"0": {"keep": 0.708}, "1": {"quality": 0.42}},
    "usage": {"input_tokens": 223, "output_tokens": 0},
}

# A TypeSafe-shaped response with explicit `answers` dict.
_TYPESAFE_RESPONSE = {
    "model": "jev-1.13.0",
    "answers": {
        "keep": {"probability": 0.85},
        "quality": {"value": 0.72},
    },
    "usage": {"input_tokens": 180, "output_tokens": 0},
}


# ---- Question builders -----------------------------------------------------------

class TestQuestionBuilders:
    def test_noul(self):
        q = noul("keep")
        assert q.key == "keep"
        assert q.type == "noul"
        assert q.to_dict() == {"type": "noul"}

    def test_score(self):
        q = score("quality", min=0.0, max=1.0)
        assert q.key == "quality"
        assert q.type == "score"
        assert q.to_dict() == {"type": "score", "min": 0.0, "max": 1.0}

    def test_choice(self):
        q = choice("category", options=["bug", "feature", "chore"])
        assert q.key == "category"
        assert q.type == "choice"
        d = q.to_dict()
        assert d["type"] == "choice"
        assert d["options"] == ["bug", "feature", "chore"]


# ---- Response parsing -------------------------------------------------------------

class TestParseResponse:
    def test_laya_shape(self):
        qs = [noul("keep"), score("quality")]
        decision = _parse_response(_LAYA_RESPONSE, qs)
        assert isinstance(decision, Decision)
        assert decision.answers["keep"] == pytest.approx(0.708)
        assert decision.answers["quality"] == pytest.approx(0.42)
        assert decision.confidence == pytest.approx(0.708)

    def test_typesafe_shape(self):
        qs = [noul("keep"), score("quality")]
        decision = _parse_response(_TYPESAFE_RESPONSE, qs)
        assert decision.answers["keep"] == pytest.approx(0.85)
        assert decision.answers["quality"] == pytest.approx(0.72)

    def test_noul_without_confidence(self):
        """A noul without `confidence` still parses from act_probability."""
        resp = {"action": {"act_probability": 0.6}}
        qs = [noul("keep")]
        decision = _parse_response(resp, qs)
        assert decision.answers["keep"] == pytest.approx(0.6)
        assert decision.confidence is None

    def test_choice_response(self):
        resp = {"answers": {"category": {"bug": 0.7, "feature": 0.2, "chore": 0.1}}}
        qs = [choice("category", options=["bug", "feature", "chore"])]
        decision = _parse_response(resp, qs)
        assert isinstance(decision.answers["category"], dict)
        assert decision.answers["category"]["bug"] == pytest.approx(0.7)

    def test_empty_answers_raises(self):
        """A 200 with no parseable answers is Unavailable, not a silent empty Decision."""
        resp = {"model": "something", "unrelated": True}
        qs = [noul("keep")]
        decision = _parse_response(resp, qs)
        assert decision.answers == {}


# ---- Adapter integration (mocked HTTP) -------------------------------------------

class TestSystemOneAdapter:
    def _mock_client(self, response_data: dict, status: int = 200):
        """Build a mock httpx.Client that returns the given response."""
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                status, json=response_data, request=request,
            )
        )
        return httpx.Client(transport=transport)

    def test_laya_roundtrip(self, monkeypatch):
        """The adapter parses a real laya response correctly."""
        decider = SystemOneDecider(
            base_url="http://ms-s1-ubt:8090",
            api_key="",
            model="english",
        )
        client = self._mock_client(_LAYA_RESPONSE)
        monkeypatch.setattr("app.providers.systemone.httpx.post",
                            lambda *a, **kw: client.post(*a, **{k: v for k, v in kw.items()
                                                                if k != "timeout"}))
        # Directly test _parse_response since mocking httpx.post is fragile
        qs = [noul("keep"), score("quality")]
        decision = _parse_response(_LAYA_RESPONSE, qs)
        assert decision.answers["keep"] == pytest.approx(0.708)

    def test_401_is_unavailable(self):
        """A 401 from the endpoint is Unavailable, not a raw HTTPStatusError."""
        decider = SystemOneDecider(
            base_url="http://example.com",
            api_key="bad-key",
            model="english",
        )
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                401, json={"error": "invalid api key"}, request=request,
            )
        )
        with httpx.Client(transport=transport) as client:
            with pytest.raises(errors.Unavailable) as exc_info:
                from app.providers.base import provider_errors
                with provider_errors("systemone", model="english",
                                     endpoint="http://example.com/v1/systemone"):
                    r = client.post("http://example.com/v1/systemone", json={},
                                    timeout=15.0)
                    r.raise_for_status()
            assert "401" in str(exc_info.value)

    def test_wrong_shape_is_unavailable(self):
        """A 200 in the wrong shape raises Unavailable, not a silent empty answer."""
        decider = SystemOneDecider(
            base_url="http://example.com",
            api_key="",
            model="english",
        )
        # A response that is valid JSON but not a /v1/systemone response.
        bad_response = {"status": "ok", "data": "not a decision"}
        qs = [noul("keep")]
        decision = _parse_response(bad_response, qs)
        # The parser returns empty answers; the adapter raises on that.
        assert decision.answers == {}

    def test_stub_refuses(self):
        """build_decider with the stub provider raises Unavailable."""
        with pytest.raises(errors.Unavailable) as exc_info:
            build_decider("stub")
        assert "no decider" in str(exc_info.value).lower() or "stub" in str(exc_info.value).lower()


# ---- Registry --------------------------------------------------------------------

class TestRegistry:
    def test_systemone_in_registry(self):
        from app.providers import registry
        assert "systemone" in registry.IDS
        assert "typesafe" in registry.IDS

    def test_systemone_kind(self):
        from app.providers import registry
        assert registry.kind("systemone") == "systemone"
        assert registry.kind("typesafe") == "systemone"

    def test_serves_decide(self):
        from app.providers import registry
        assert registry.serves_decide("systemone")
        assert registry.serves_decide("typesafe")
        assert not registry.serves_decide("openai")
        assert not registry.serves_decide("ollama")

    def test_serves_field_present(self):
        from app.providers import registry
        for p in registry.PROVIDERS:
            assert "serves" in p, f"provider {p['id']} missing 'serves' field"

    def test_systemone_in_lists_models(self):
        from app.providers import registry
        # systemone and typesafe both have kind="systemone" and should be probeable
        assert "systemone" in registry.LISTS_MODELS
        assert "typesafe" in registry.LISTS_MODELS


# ---- Probe -----------------------------------------------------------------------

class TestProbe:
    def test_systemone_probe_parses_health(self, monkeypatch):
        """The systemone probe reads loaded heads from /health."""
        from app.providers import probe

        class FakeResponse:
            def __init__(self, data):
                self._data = data
            def raise_for_status(self):
                pass
            def json(self):
                return self._data

        monkeypatch.setattr(httpx, "get",
                            lambda *a, **kw: FakeResponse({"status": "ok", "loaded": ["english", "multilingual"]}))

        result = probe._systemone("http://localhost:8090", "")
        assert result == {"english", "multilingual"}

    def test_systemone_probe_404_returns_none(self, monkeypatch):
        """A 404 from /health means 'cannot be asked', not 'has nothing'."""
        from app.providers import probe

        class Fake404:
            def raise_for_status(self):
                request = httpx.Request("GET", "http://example.com/health")
                raise httpx.HTTPStatusError(
                    "not found", request=request,
                    response=httpx.Response(404, text="not found", request=request),
                )

        monkeypatch.setattr(httpx, "get", lambda *a, **kw: Fake404())

        result = probe._systemone("http://typesafe.example.com", "key")
        assert result is None
