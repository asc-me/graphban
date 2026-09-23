"""SystemOne adapter — speaks the TypeSafe Jev /v1/systemone protocol (GRPH-895, PRD-45 S1).

Two endpoints share this wire format:

- **laya** (self-host) — ``POST {base_url}/v1/systemone``. The ``model`` field selects a
  **head** (e.g. ``english``, ``multilingual``, ``typed-decisions``); ``"laya"`` routes to
  ``english``. Response carries ``confidence`` on a noul, ``action.act_probability`` on
  every answer, ``routing``, and ``legend`` keyed by index.
- **TypeSafe cloud** — ``POST https://api.typesafe.ai/v1/systemone`` with bearer auth.
  Same wire format; the model field is ``jev-1.13.0`` (or whatever the account serves).

The adapter is additive — nothing points at it yet. S3 wires it into memory adjudication.
"""
from __future__ import annotations

import logging

import httpx

from app import errors
from app.config import settings
from app.providers.base import provider_errors
from app.providers.decide import Decision, Question

logger = logging.getLogger("graphban.providers.systemone")


def _timeout() -> httpx.Timeout:
    """Short — a decider answers in tens to hundreds of milliseconds (measured 70–270 ms).

    The LLM timeout is designed for generative models that think for seconds; a decider
    that takes that long is broken, not slow. Using the same connect timeout as the other
    adapters (5 s) but a tighter read timeout keeps a hung endpoint from blocking the
    review queue for the full LLM_TIMEOUT_SECONDS.
    """
    return httpx.Timeout(15.0, connect=5.0)


def _build_body(
    *,
    model: str,
    system: str,
    context: str,
    question: str,
    answer_space: list[Question],
) -> dict:
    """Assemble the /v1/systemone request body."""
    return {
        "model": model,
        "state": {
            "system": system,
            "context": context,
            "question": question,
            "answer_space": {q.key: q.to_dict() for q in answer_space},
        },
    }


def _parse_response(data: dict, answer_space: list[Question]) -> Decision:
    """Parse a /v1/systemone response into a Decision.

    The protocol returns probabilities indexed by the answer_space keys. The exact shape
    varies between laya and TypeSafe:

    - laya: ``action.act_probability`` for the primary answer, ``confidence`` on a noul,
      ``legend`` keyed by index with per-option probabilities.
    - TypeSafe: ``answers`` keyed by question key, each with ``probability`` or ``value``.

    We try both shapes and fall back gracefully. A response in neither shape is an
    ``Unavailable`` — the caller can degrade to similarity, not crash.
    """
    answers: dict[str, float | dict[str, float]] = {}

    # TypeSafe shape: explicit `answers` dict keyed by question key.
    raw_answers = data.get("answers")
    if isinstance(raw_answers, dict):
        for q in answer_space:
            entry = raw_answers.get(q.key)
            if entry is None:
                continue
            if isinstance(entry, dict):
                # choice: {option: probability}
                if q.type == "choice":
                    probs = {k: float(v) for k, v in entry.items()
                             if isinstance(v, (int, float))}
                    if probs:
                        answers[q.key] = probs
                else:
                    # noul/score: look for `probability` or `value`
                    val = entry.get("probability", entry.get("value"))
                    if isinstance(val, (int, float)):
                        answers[q.key] = float(val)
            elif isinstance(entry, (int, float)):
                answers[q.key] = float(entry)

    # laya shape: no `answers` key; extract from action/confidence/legend.
    if not answers:
        action = data.get("action") or {}
        if isinstance(action, dict):
            ap = action.get("act_probability")
            if isinstance(ap, (int, float)):
                # The first noul question gets the act_probability.
                for q in answer_space:
                    if q.type == "noul":
                        answers[q.key] = float(ap)
                        break

        # Confidence on a noul overrides act_probability when present (laya's own cal).
        conf = data.get("confidence")
        if isinstance(conf, (int, float)) and conf > 0:
            for q in answer_space:
                if q.type == "noul":
                    answers[q.key] = float(conf)
                    break

        # Score questions from the legend or answer dict.
        legend = data.get("legend") or {}
        answer_obj = data.get("answer") or {}
        for q in answer_space:
            if q.key in answers:
                continue
            if q.type == "score":
                # Try answer.quality, answer.score, then legend entries.
                for src in (answer_obj, data):
                    if isinstance(src, dict):
                        val = src.get(q.key)
                        if isinstance(val, (int, float)):
                            answers[q.key] = float(val)
                            break
                if q.key not in answers and isinstance(legend, dict):
                    for _idx, entry in legend.items():
                        if isinstance(entry, dict) and q.key in entry:
                            answers[q.key] = float(entry[q.key])
                            break
            elif q.type == "choice" and isinstance(legend, dict):
                probs = {}
                for idx, entry in legend.items():
                    if isinstance(entry, dict):
                        for opt in q.options:
                            if opt in entry:
                                probs[opt] = float(entry[opt])
                if probs:
                    answers[q.key] = probs

    # Top-level quality as a last resort for score questions.
    for q in answer_space:
        if q.key in answers or q.type != "score":
            continue
        val = data.get(q.key)
        if isinstance(val, (int, float)):
            answers[q.key] = float(val)

    # Confidence on the noul (protocol-level, not per-answer).
    confidence: float | None = None
    conf = data.get("confidence")
    if isinstance(conf, (int, float)):
        confidence = float(conf)

    return Decision(answers=answers, confidence=confidence, raw=data)


class SystemOneDecider:
    """Adapter for the TypeSafe Jev /v1/systemone protocol.

    Works with both laya (self-host) and TypeSafe's cloud endpoint. The ``model`` field
    selects a head on laya (e.g. ``english``, ``multilingual``) or a model on TypeSafe
    (e.g. ``jev-1.13.0``).
    """

    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def decide(
        self,
        *,
        system: str,
        context: str,
        question: str,
        answer_space: list[Question],
    ) -> Decision:
        body = _build_body(
            model=self.model,
            system=system,
            context=context,
            question=question,
            answer_space=answer_space,
        )
        endpoint = f"{self.base_url}/v1/systemone"
        with provider_errors("systemone", model=self.model, endpoint=endpoint):
            r = httpx.post(
                endpoint,
                json=body,
                headers=self._headers(),
                timeout=_timeout(),
            )
            r.raise_for_status()
            data = r.json()

        if not isinstance(data, dict):
            raise errors.Unavailable(
                f"systemone ({endpoint}) returned a non-object response",
                hint="the endpoint may not speak the /v1/systemone protocol; "
                     "check the base URL in Settings → AI providers",
            )

        decision = _parse_response(data, answer_space)

        # A response with zero answers parsed is a shape mismatch — the endpoint answered
        # but not in a way we can use. This is different from a transport error: the
        # connection succeeded, the protocol was wrong.
        if not decision.answers:
            raise errors.Unavailable(
                f"systemone ({endpoint}) returned a well-formed response with no "
                f"parseable answers for {len(answer_space)} question(s)",
                hint="the response shape may not match the /v1/systemone protocol; "
                     "check the endpoint and model configuration",
            )

        # Record usage for the span table. The protocol returns input_tokens; output_tokens
        # is always 0 (a decider produces no text).
        from app.providers import llm_meter
        usage = data.get("usage") or {}
        if isinstance(usage, dict):
            llm_meter.record_usage(
                input=usage.get("input_tokens"),
                output=usage.get("output_tokens", 0),
            )

        return decision


def decider(*, base_url: str, api_key: str, model: str) -> SystemOneDecider:
    """Construct a SystemOne decider adapter."""
    return SystemOneDecider(base_url=base_url, api_key=api_key, model=model)
