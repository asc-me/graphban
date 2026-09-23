"""Decider protocol — the third model type beside `ChatModel` and `Embedder` (PRD-45 S1).

A **System One model** returns decisions and nothing else. The caller declares the answer
space up front — a yes/no (`noul`), an ordered scale (`score`), a set of named options
(`choice`) — and the model returns a calibrated probability over exactly that space in one
forward pass. It cannot produce an invalid answer and cannot produce text.

The wire is TypeSafe AI's `/v1/systemone` (PRD-45 D2): a request is `{model, state,
questions}` where every question carries `type`, `instructions` and `criteria`; a reply is
`answers` keyed by question, each carrying the typed answer (`noul` / `score` / `choice`),
`probabilities` and `confidence`. **This module is the only place that shape is spelled
out.** The first version of S1 invented a `state.answer_space` shape from no protocol and
shipped a "recorded laya reply" nobody had recorded; the first real call was a 400. Every
question builder below produces exactly what the protocol takes, and a test pins it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

NOUL = "noul"
SCORE = "score"
CHOICE = "choice"
TYPES = (NOUL, SCORE, CHOICE)

#: TypeSafe's bounds: a score has 2–10 levels, a choice up to 255 options.
SCORE_LEVELS = (2, 10)
CHOICE_MAX = 255


@dataclass(frozen=True)
class Question:
    """One question in the answer space, in the protocol's own terms.

    `criteria` is what makes the answer space explicit: a noul's `true`/`false` readings, a
    score's ordered level names, a choice's `{option: description}`. A question without
    criteria is legal for a noul (the instructions alone), never for a score or a choice.
    """

    key: str
    type: str
    instructions: str
    criteria: Any = None

    def to_dict(self) -> dict:
        d: dict = {"type": self.type, "instructions": self.instructions}
        if self.criteria is not None:
            d["criteria"] = (dict(self.criteria) if isinstance(self.criteria, dict)
                             else list(self.criteria))
        return d


def noul(key: str, instructions: str, *, true: str = "", false: str = "") -> Question:
    """A yes/no question. The model returns P(true) as `noul`."""
    criteria = {"true": true, "false": false} if (true or false) else None
    return Question(key=key, type=NOUL, instructions=instructions, criteria=criteria)


def score(key: str, instructions: str, levels: list[str]) -> Question:
    """An ordered-scale question over named `levels`. The model returns a position on the
    scale as `score` (0 is the first level, fractional between levels)."""
    n = len(levels)
    if not SCORE_LEVELS[0] <= n <= SCORE_LEVELS[1]:
        raise ValueError(f"a score needs {SCORE_LEVELS[0]}–{SCORE_LEVELS[1]} levels, got {n}")
    return Question(key=key, type=SCORE, instructions=instructions, criteria=list(levels))


def choice(key: str, instructions: str, options: dict[str, str | None] | list[str]) -> Question:
    """A named-options question. The model returns the chosen option as `choice` and a
    probability per option."""
    opts = {o: None for o in options} if isinstance(options, list) else dict(options)
    if not 1 <= len(opts) <= CHOICE_MAX:
        raise ValueError(f"a choice needs 1–{CHOICE_MAX} options, got {len(opts)}")
    return Question(key=key, type=CHOICE, instructions=instructions, criteria=opts)


@dataclass(frozen=True)
class Answer:
    """The model's answer to one question.

    `value` is typed by the question: a noul's P(true), a score's position on its levels,
    a choice's option key. `probabilities` is the full distribution when the endpoint sent
    one (a choice's per option; a score's per level, keyed by level index); `confidence`
    when it sent that. **Never a string of prose.**
    """

    value: float | str
    probabilities: dict[str, float] | None = None
    confidence: float | None = None


@dataclass(frozen=True)
class Decision:
    """One reply: an `Answer` per question asked, and which model answered.

    `model` is what the endpoint says answered — laya reports `routing.model` (the head),
    TypeSafe the pinned model name — so a verdict can be traced to the head that gave it
    (PRD-45 D2), and a name the endpoint silently routed elsewhere is visible.
    """

    answers: dict[str, Answer]
    model: str = ""
    raw: dict = field(default_factory=dict, repr=False)


@runtime_checkable
class Decider(Protocol):
    """Every adapter implements this. `state` is the text (or structured state) being judged;
    `questions` is the declared answer space. The reply has one `Answer` per question or
    raises `errors.Unavailable` — an answer space with holes is not a decision."""

    def decide(self, *, state: str | dict | list, questions: list[Question]) -> Decision: ...
