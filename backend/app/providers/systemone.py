"""System One adapter — the TypeSafe `/v1/systemone` wire (PRD-45 S1, D2).

Two endpoints share this wire:

- **laya** (self-host) — `POST {base_url}/v1/systemone`. `model` selects a **head**
  (`english`, `multilingual`, `typed-decisions`); an unknown name such as `laya` is routed
  to `english` and the reply says so in `routing.model`. The reply's `model` is always
  `laya-rl-agent`. Measured on ms-s1-ubt 2026-09-23: 70–270 ms, deterministic.
- **TypeSafe cloud** — `POST https://api.typesafe.ai/v1/systemone`, bearer auth, `model`
  a pinned name such as `jev-1.13.0`.

The adapter depends on exactly the published contract: `{model, state, questions}` out,
`answers[key].{noul|score|choice, probabilities, confidence}` and `usage` back. Fields laya
adds (`routing`, `action`, `legend` keyed by index) are read where useful and never required.
A reply that is a 200 and not that shape is `errors.Unavailable` — "answered, but not in
the System One shape" (D10) — never a Decision with a made-up 0.5 in it.
"""
from __future__ import annotations

import logging

import httpx

from app import errors
from app.providers.base import provider_errors
from app.providers.decide import CHOICE, NOUL, SCORE, Answer, Decision, Question

logger = logging.getLogger("graphban.providers.systemone")

PATH = "/v1/systemone"


def _timeout() -> httpx.Timeout:
    """Short. A decider answers in tens to hundreds of milliseconds; one that takes the
    chat timeout is broken, not slow, and must not hold the review queue for 90 s."""
    return httpx.Timeout(15.0, connect=5.0)


def _build_body(model: str, state: str | dict | list, questions: list[Question]) -> dict:
    """The request, in the protocol's own words. A function so a test pins the shape."""
    return {"model": model, "state": state, "questions": {q.key: q.to_dict() for q in questions}}


def _shape_error(endpoint: str, what: str) -> errors.Unavailable:
    return errors.Unavailable(
        f"systemone ({endpoint}) answered, but not in the System One shape: {what}",
        hint="the endpoint may not speak /v1/systemone, or the model name is wrong; "
             "check the base URL and model in Settings -> AI providers",
    )


def _parse_response(data: dict, questions: list[Question], *, endpoint: str = "") -> Decision:
    """`answers[key]` typed by the question asked. Every question must be answered; a
    hole is a shape error, not a default."""
    answers = data.get("answers") if isinstance(data, dict) else None
    if not isinstance(answers, dict):
        raise _shape_error(endpoint, "no `answers` object")
    out: dict[str, Answer] = {}
    for q in questions:
        entry = answers.get(q.key)
        if not isinstance(entry, dict):
            raise _shape_error(endpoint, f"no answer for {q.key!r}")
        value = entry.get(q.type)
        if q.type in (NOUL, SCORE):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise _shape_error(endpoint, f"{q.key!r} has no numeric `{q.type}`")
            value = float(value)
        elif q.type == CHOICE:
            if not isinstance(value, str) or not value:
                raise _shape_error(endpoint, f"{q.key!r} has no `choice`")
        else:  # pragma: no cover — the builders only make the three
            raise _shape_error(endpoint, f"unknown question type {q.type!r}")
        probs_raw = entry.get("probabilities")
        probabilities = ({str(k): float(v) for k, v in probs_raw.items()
                          if isinstance(v, (int, float))}
                         if isinstance(probs_raw, dict) else None)
        conf = entry.get("confidence")
        confidence = float(conf) if isinstance(conf, (int, float)) and not isinstance(conf, bool) else None
        out[q.key] = Answer(value=value, probabilities=probabilities, confidence=confidence)
    routing = data.get("routing")
    routed = routing.get("model") if isinstance(routing, dict) else None
    model = str(routed or data.get("model") or "")
    return Decision(answers=out, model=model, raw=data)


class SystemOneDecider:
    """One credential's decider. `model` is the head (laya) or the pinned model (TypeSafe)."""

    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def decide(self, *, state: str | dict | list, questions: list[Question]) -> Decision:
        if not questions:
            raise ValueError("a decision needs at least one question")
        endpoint = f"{self.base_url}{PATH}"
        body = _build_body(self.model, state, questions)
        with provider_errors("systemone", model=self.model, endpoint=endpoint):
            r = httpx.post(endpoint, json=body, headers=self._headers(), timeout=_timeout())
            r.raise_for_status()
            data = r.json()
        if not isinstance(data, dict):
            raise _shape_error(endpoint, "the body is not an object")
        decision = _parse_response(data, questions, endpoint=endpoint)
        if isinstance(data.get("routing"), dict) and decision.model and decision.model != self.model:
            # laya routes a name it does not serve to a default head and says so. A verdict
            # attributed to a head that never answered is the substitution PRD-45 §12 names.
            logger.warning("systemone: model %r was answered by %r (endpoint routed the name)",
                           self.model, decision.model)
        from app.providers import llm_meter

        usage = data.get("usage") or {}
        if isinstance(usage, dict):
            llm_meter.record_usage(input=usage.get("input_tokens"),
                                   output=usage.get("output_tokens", 0))
        return decision


def decider(*, base_url: str, api_key: str, model: str) -> SystemOneDecider:
    return SystemOneDecider(base_url=base_url, api_key=api_key, model=model)
