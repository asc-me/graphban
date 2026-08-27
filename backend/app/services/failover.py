"""Failing over to a second credential, and saying which one answered (PRD-25 S3, D-h).

**Failover is about the response in hand, never about a provider's future.** There is no attempt
to tell "permanently broken" from "temporarily throttled" — that is a judgement nobody can make
from one HTTP status, and pretending to make it is how a design acquires a state machine it
cannot keep accurate.

**Unknown statuses are TERMINAL** (`providers.base.RETRYABLE_STATUSES`). Failing over on an
unclassified error is how one bug becomes a doubled bill on every call, and the two
misclassifications are not symmetric: calling something terminal that was retryable surfaces
immediately as an error a human reads, while the reverse hides inside a response that looks fine.

**Which credential answered is part of the answer.** A response that quietly came from the
fallback is a cost and a quality change nobody consented to — a different model, possibly a
different vendor, with no trace in the thing the user is reading. `answered_by` and `failed_over`
are set on every call so the caller can report it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.providers.base import ChatModel

logger = logging.getLogger("graphban.failover")


def is_retryable(exc: BaseException) -> bool:
    """Whether a DIFFERENT credential is worth trying for this failure.

    Reads the verdict `providers.base` attached at the raise site rather than parsing a
    message. An error carrying no verdict at all is terminal — an unclassified failure is
    exactly the case the default exists for.
    """
    return bool(getattr(exc, "retryable", False))


class BothFailed(Exception):
    """Primary and fallback both failed.

    **The primary's failure leads and the fallback's sits under its own key.** The operator
    configured the default; that is the failure they must act on, and it belongs in the
    ordinary message field every existing log and alert path already reads.

    They are NOT concatenated. A single string is what a downstream logger would flatten them
    into, and then nobody can tell which credential produced which half — the shape is the
    guarantee, not the wording.
    """

    def __init__(self, primary: BaseException, primary_id: str,
                 fallback: BaseException, fallback_id: str) -> None:
        self.primary = primary
        self.primary_id = primary_id
        self.fallback = fallback
        self.fallback_id = fallback_id
        super().__init__(str(primary))

    @property
    def code(self) -> str:
        return "provider_failed"

    def as_error(self) -> dict:
        return {
            "code": "provider_failed",
            "message": str(self.primary),
            "credential_id": self.primary_id,
            "also_failed": {
                "credential_id": self.fallback_id,
                "message": str(self.fallback),
            },
        }


@dataclass
class FailoverChat:
    """A `ChatModel` that tries the fallback when the primary fails retryably.

    Wrapping the model rather than the resolution is deliberate: failover is a property of the
    CALL, and resolution has already finished by the time a 429 exists. It also means every
    existing call site gets failover without knowing about it.
    """

    primary: ChatModel
    primary_id: str
    fallback: ChatModel | None = None
    fallback_id: str = ""
    #: The credential that produced the last answer. Empty until a call has been made.
    answered_by: str = ""
    #: Whether the last answer came from the fallback.
    failed_over: bool = False
    #: Every failure seen, for a caller that wants to report a degraded-but-working state.
    failures: list = field(default_factory=list)

    def chat(self, *, system: str, context: str, question: str,
             temperature: float | None = None) -> str:
        kwargs = {"system": system, "context": context, "question": question}
        if temperature is not None:
            kwargs["temperature"] = temperature

        self.failed_over = False
        try:
            answer = self.primary.chat(**kwargs)
        except Exception as exc:  # noqa: BLE001 — classified immediately below
            self.failures.append((self.primary_id, exc))
            if self.fallback is None or not is_retryable(exc):
                # Terminal, or nothing to fall over to. The original error survives INTACT —
                # not wrapped, not reworded — because a caller that already handles it must
                # keep handling it the same way.
                self.answered_by = ""
                raise
            logger.warning(
                "credential %s failed retryably (%s); trying fallback %s",
                self.primary_id, exc, self.fallback_id,
            )
            try:
                answer = self.fallback.chat(**kwargs)
            except Exception as second:  # noqa: BLE001
                self.failures.append((self.fallback_id, second))
                self.answered_by = ""
                raise BothFailed(exc, self.primary_id, second, self.fallback_id) from second
            self.answered_by = self.fallback_id
            self.failed_over = True
            return answer

        self.answered_by = self.primary_id
        return answer
