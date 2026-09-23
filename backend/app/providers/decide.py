"""Decider protocol — the third model type beside ChatModel and Embedder (GRPH-895, PRD-45 S1).

A **System One model** returns decisions and nothing else. The caller declares the answer
space up front — a yes/no (`noul`), an ordered scale (`score`), a set of named options
(`choice`) — and the model returns a calibrated probability over exactly that space in one
forward pass. It cannot produce an invalid answer, cannot produce text, and answers in tens
to hundreds of milliseconds.

This module defines:

- **Question builders** — `noul`, `score`, `choice` — that construct the `answer_space`
  payload the protocol expects.
- **Answers** — `Decision`, the parsed response from a decider call.
- **Decider** — the Protocol every adapter implements.

The protocol is TypeSafe AI's Jev `/v1/systemone`; the first adapter is `systemone.py`,
which speaks to laya (self-host) and TypeSafe's cloud endpoint.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Question:
    """One question in the answer space.

    `type` is one of:
    - ``"noul"``  — yes/no (binary). The model returns P(yes).
    - ``"score"`` — ordered scale from `min` to `max`. The model returns a value in range.
    - ``"choice"``— named options. The model returns a probability distribution over them.

    `key` is the name the caller uses to read the answer back (e.g. "keep", "quality").
    """
    key: str
    type: str  # "noul" | "score" | "choice"
    min: float = 0.0
    max: float = 1.0
    options: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d: dict = {"type": self.type}
        if self.type == "score":
            d["min"] = self.min
            d["max"] = self.max
        elif self.type == "choice":
            d["options"] = list(self.options)
        return d


def noul(key: str) -> Question:
    """A yes/no question. The model returns P(yes) as `confidence` on the noul."""
    return Question(key=key, type="noul")


def score(key: str, *, min: float = 0.0, max: float = 1.0) -> Question:
    """An ordered scale question. The model returns a value in [min, max]."""
    return Question(key=key, type="score", min=min, max=max)


def choice(key: str, options: list[str]) -> Question:
    """A named-options question. The model returns a probability distribution."""
    return Question(key=key, type="choice", options=options)


@dataclass(frozen=True)
class Decision:
    """The parsed response from a decider call.

    `answers` is keyed by question key. Each value is the model's answer for that question:
    - noul: a float in [0, 1] — P(yes).
    - score: a float in [min, max].
    - choice: a dict mapping option name → probability.

    `confidence` is the protocol's own confidence on a noul (optional, present when the
    endpoint provides it — laya does, TypeSafe may not).

    `raw` is the unmodified response body, for diagnostics and span recording.
    """
    answers: dict[str, float | dict[str, float]]
    confidence: float | None = None
    raw: dict = field(default_factory=dict, repr=False)


@runtime_checkable
class Decider(Protocol):
    """The Decider protocol. Every adapter implements this.

    `decide` takes a system prompt, context text, question text, and a list of Question
    objects defining the answer space. Returns a Decision with one answer per question.
    """

    def decide(
        self,
        *,
        system: str,
        context: str,
        question: str,
        answer_space: list[Question],
    ) -> Decision: ...
