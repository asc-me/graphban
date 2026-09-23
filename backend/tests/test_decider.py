"""Decider provider tests (GRPH-895, PRD-45 S1).

The adapter against a laya reply that was actually recorded (ms-s1-ubt, 2026-09-23) and a
TypeSafe-shaped reply; the request is the protocol's shape; a hole in the answers is
`Unavailable`, never a default; a 401 is `Unavailable` with the status; the stub refuses.

The first version of this file carried a "real laya response" with `action` / `legend` and
no `answers`, which no endpoint has ever sent, and passed 19 tests against it. The reply
below was captured from the box with curl, and the body test is the one that fails if
anyone reintroduces `state.answer_space`.
"""
from __future__ import annotations

import httpx
import pytest

from app import errors
from app.providers import build_decider, choice, noul, score
from app.providers.decide import Answer, Decision, Question
from app.providers.systemone import SystemOneDecider, _build_body, _parse_response

# ---- recorded replies ------------------------------------------------------------------

#: Verbatim from `POST http://ms-s1-ubt:8090/v1/systemone` on 2026-09-23, three questions.
LAYA_REPLY = {
    "model": "laya-rl-agent",
    "answers": {
        "keep": {"type": "noul", "noul": 0.4361, "confidence": 0.5639,
                 "action": {"act_probability": 1.0}},
        "quality": {"type": "score", "score": 2.1258,
                    "legend": {"0": "useless", "1": "weak", "2": "fair", "3": "good", "4": "excellent"},
                    "probabilities": {"0": 0.0283, "1": 0.2371, "2": 0.3983, "3": 0.2533, "4": 0.0831},
                    "confidence": 0.153, "action": {"act_probability": 1.0}},
        "kind": {"type": "choice", "choice": "convention",
                 "probabilities": {"convention": 0.5183, "gotcha": 0.4539, "status": 0.0278},
                 "confidence": 0.273, "action": {"act_probability": 1.0}},
    },
    "usage": {"input_tokens": 223, "output_tokens": 0},
    "routing": {"model": "english", "repo": "convaiinnovations/laya",
                "reason": "explicit model='english'", "detection": None, "workflow": None},
}

#: TypeSafe's documented shape: a noul carries no confidence; a score's legend is by index.
TYPESAFE_REPLY = {
    "model": "jev-1.13.0",
    "answers": {
        "keep": {"type": "noul", "noul": 0.85},
        "quality": {"type": "score", "score": 3.1,
                    "legend": {"0": "useless", "1": "weak", "2": "fair", "3": "good", "4": "excellent"},
                    "probabilities": {"0": 0.01, "1": 0.04, "2": 0.15, "3": 0.5, "4": 0.3},
                    "confidence": 0.62},
    },
    "usage": {"input_tokens": 180, "output_tokens": 0},
}

LEVELS = ["useless", "weak", "fair", "good", "excellent"]
KEEP = noul("keep", "The note is durable, specific and actionable",
            true="a rule, gotcha or decision with enough detail to act on",
            false="vague, transient, obvious, redundant, or a status update")
QUALITY = score("quality", "How useful this note is to a future agent", LEVELS)
KIND = choice("kind", "What kind of note this is",
              {"convention": "a rule the project follows", "gotcha": "a trap and its fix",
               "status": "a transient status update"})


# ---- the request is the protocol's shape -----------------------------------------------

class TestRequest:
    def test_the_body_is_model_state_questions(self):
        """Sabotage: put `answer_space` back under `state`; this fails on the missing key."""
        body = _build_body("multilingual", "Always run pnpm install --frozen-lockfile", [KEEP, QUALITY, KIND])
        assert set(body) == {"model", "state", "questions"}
        assert body["model"] == "multilingual"
        assert body["state"] == "Always run pnpm install --frozen-lockfile"
        q = body["questions"]
        assert q["keep"] == {"type": "noul", "instructions": KEEP.instructions,
                             "criteria": {"true": KEEP.criteria["true"], "false": KEEP.criteria["false"]}}
        assert q["quality"] == {"type": "score", "instructions": QUALITY.instructions, "criteria": LEVELS}
        assert q["kind"]["type"] == "choice" and set(q["kind"]["criteria"]) == {"convention", "gotcha", "status"}

    def test_a_noul_without_criteria_carries_only_instructions(self):
        assert noul("k", "is it?").to_dict() == {"type": "noul", "instructions": "is it?"}

    def test_the_protocol_bounds_are_enforced_before_the_wire(self):
        with pytest.raises(ValueError):
            score("q", "one level is not a scale", ["only"])
        with pytest.raises(ValueError):
            score("q", "too many", [str(i) for i in range(11)])
        with pytest.raises(ValueError):
            choice("c", "none", [])
        assert choice("c", "list form", ["a", "b"]).criteria == {"a": None, "b": None}


# ---- the reply is read from `answers`, typed by the question -------------------------

class TestReply:
    def test_the_recorded_laya_reply(self):
        d = _parse_response(LAYA_REPLY, [KEEP, QUALITY, KIND])
        assert isinstance(d, Decision)
        assert d.answers["keep"] == Answer(value=0.4361, probabilities=None, confidence=0.5639)
        assert d.answers["quality"].value == pytest.approx(2.1258)
        assert d.answers["quality"].probabilities["2"] == pytest.approx(0.3983)
        assert d.answers["kind"].value == "convention"
        assert d.answers["kind"].probabilities["gotcha"] == pytest.approx(0.4539)
        assert d.model == "english"  # the head that answered, not the reply's `model`

    def test_a_typesafe_reply_without_noul_confidence(self):
        d = _parse_response(TYPESAFE_REPLY, [KEEP, QUALITY])
        assert d.answers["keep"].value == pytest.approx(0.85) and d.answers["keep"].confidence is None
        assert d.answers["quality"].value == pytest.approx(3.1)
        assert d.model == "jev-1.13.0"

    def test_a_hole_in_the_answers_is_unavailable_not_a_default(self):
        """Sabotage: fill a missing answer with 0.5; every branch below passes and the
        review queue publishes on a number nobody computed."""
        with pytest.raises(errors.Unavailable, match="System One shape"):
            _parse_response({"answers": {"keep": {"type": "noul", "noul": 0.9}}}, [KEEP, QUALITY])
        with pytest.raises(errors.Unavailable, match="numeric"):
            _parse_response({"answers": {"keep": {"type": "noul", "noul": "yes"}}}, [KEEP])
        with pytest.raises(errors.Unavailable, match="no `choice`"):
            _parse_response({"answers": {"kind": {"type": "choice", "probabilities": {}}}}, [KIND])

    def test_the_shape_the_first_version_invented_is_refused(self):
        """What S1 v1 called a laya reply. No endpoint sends it; the adapter must not read it."""
        invented = {"model": "laya-rl-agent", "routing": {"model": "english"},
                    "action": {"act_probability": 0.708}, "confidence": 0.708,
                    "answer": {"quality": 0.42}, "legend": {"0": {"keep": 0.708}}}
        with pytest.raises(errors.Unavailable, match="no `answers`"):
            _parse_response(invented, [KEEP, QUALITY])


# ---- the adapter end to end, over a fake transport ----------------------------------------

def _serve(monkeypatch, status: int, payload: dict, seen: list):
    def fake_post(url, *, json, headers, timeout):
        seen.append((url, json, headers))
        req = httpx.Request("POST", url)
        return httpx.Response(status, json=payload, request=req)
    monkeypatch.setattr("app.providers.systemone.httpx.post", fake_post)


class TestAdapter:
    def test_round_trip_against_laya(self, monkeypatch):
        seen: list = []
        _serve(monkeypatch, 200, LAYA_REPLY, seen)
        d = SystemOneDecider("http://ms-s1-ubt:8090", "", "multilingual").decide(
            state="Always run pnpm install --frozen-lockfile", questions=[KEEP, QUALITY, KIND])
        url, body, headers = seen[0]
        assert url == "http://ms-s1-ubt:8090/v1/systemone"
        assert "questions" in body and "answer_space" not in str(body)
        assert "Authorization" not in headers  # no key, no header
        assert d.answers["keep"].value == pytest.approx(0.4361)

    def test_a_key_travels_as_a_bearer(self, monkeypatch):
        seen: list = []
        _serve(monkeypatch, 200, TYPESAFE_REPLY, seen)
        SystemOneDecider("https://api.typesafe.ai", "sk-x", "jev-1.13.0").decide(state="t", questions=[KEEP, QUALITY])
        assert seen[0][2]["Authorization"] == "Bearer sk-x"

    def test_a_401_is_unavailable_with_the_status(self, monkeypatch):
        _serve(monkeypatch, 401, {"detail": "invalid api key"}, [])
        with pytest.raises(errors.Unavailable) as e:
            SystemOneDecider("https://api.typesafe.ai", "bad", "jev-1.13.0").decide(state="t", questions=[KEEP])
        assert "401" in str(e.value)

    def test_a_200_in_the_wrong_shape_is_unavailable(self, monkeypatch):
        _serve(monkeypatch, 200, {"status": "ok", "data": "not a decision"}, [])
        with pytest.raises(errors.Unavailable, match="System One shape"):
            SystemOneDecider("http://box:8090", "", "english").decide(state="t", questions=[KEEP])

    def test_no_questions_is_a_caller_error(self):
        with pytest.raises(ValueError):
            SystemOneDecider("http://box:8090", "", "english").decide(state="t", questions=[])

    def test_the_stub_refuses(self):
        with pytest.raises(errors.Unavailable):
            build_decider("stub")

    def test_build_decider_meters_decide(self, monkeypatch):
        """`llm_meter.metered` wraps the protocol methods it knows; `decide` must be one of
        them or a decider call never becomes a span (PRD-45 D3)."""
        from app.providers import llm_meter
        wrapped: list[str] = []
        real = llm_meter._wrap

        def spy(fn, kind, meta, gen):
            wrapped.append(kind)
            return real(fn, kind, meta, gen)

        monkeypatch.setattr(llm_meter, "_wrap", spy)
        build_decider("systemone", base_url="http://box:8090", model="multilingual")
        assert "decide" in wrapped


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
