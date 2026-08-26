"""GRPH-505 — a timeout that says it is one, and gets one more go.

**Found by using it, and the first diagnosis was falsified by measuring.** "The PRD is too
long" was a satisfying story built on two data points: `grill_prd` timed out twice on a 46k
PRD and succeeded twenty minutes later with nothing changed. Measured against the real host
and the configured model, 80k characters answer in 57s and the 46k case in 51s — comfortably
inside the 90s budget, and not monotonic in length at all.

So the fix is not a bigger budget. It is: try once more, and when both attempts fail, say
something a caller can act on.

The load-bearing test is not "a grill completes" — that passes today. It is a client that
times out ONCE and then succeeds.
"""
from __future__ import annotations

import pytest

from app import errors
from app.services import prds as prd_svc


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


class Flaky:
    """Times out `fail_times` times, then answers."""

    model = "qwen3.6:35b-a3b-coding-mtp-q4_K_M"

    def __init__(self, fail_times: int, answer: str = "- a question"):
        self.fail_times = fail_times
        self.answer = answer
        self.calls = 0

    def chat(self, **kwargs) -> str:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise TimeoutError("timed out")
        return self.answer


def test_one_timeout_becomes_a_slow_success():
    """THE CRITERION. This is exactly what happened manually — the retry that worked."""
    chat = Flaky(fail_times=1)

    answer, retried = prd_svc.chat_with_retry(chat, provider="ollama", model=chat.model,
                                              system="s", context="c", question="q")

    assert answer == "- a question"
    assert retried is True, "a caller cannot tell a slow success from a fast one otherwise"
    assert chat.calls == 2


def test_a_call_that_works_first_time_is_not_reported_as_retried():
    """The control. `retried` that was always True would carry no information."""
    chat = Flaky(fail_times=0)

    answer, retried = prd_svc.chat_with_retry(chat, provider="ollama", model=chat.model,
                                              system="s", context="c", question="q")

    assert retried is False and chat.calls == 1


def test_a_persistent_timeout_names_the_model_and_the_budget():
    """The other half of the defect: a bare timeout is indistinguishable from a dead server,
    a broken PRD, and a misconfigured model. The message has to separate them."""
    chat = Flaky(fail_times=99)

    with pytest.raises(errors.ModelTimedOut) as exc:
        prd_svc.chat_with_retry(chat, provider="ollama", model=chat.model,
                                system="s", context="c", question="q")

    message = str(exc.value)
    assert chat.model in message, "which model"
    assert "90" in message, "which budget"
    assert "transient" in message, "whether trying again is worth it"
    assert exc.value.hint, "a refusal must carry the machine-readable next step (AL-47)"


def test_it_stops_after_two_attempts_rather_than_looping():
    """A loop with backoff turns 'slow' into 'hangs for five minutes'. The caller has a budget
    too, and the observed failure resolved on ONE retry."""
    chat = Flaky(fail_times=99)

    with pytest.raises(errors.ModelTimedOut):
        prd_svc.chat_with_retry(chat, provider="ollama", model=chat.model,
                                system="s", context="c", question="q")

    assert chat.calls == prd_svc.CHAT_ATTEMPTS == 2


def test_the_timeout_code_is_its_own_so_a_caller_can_branch_on_it():
    """`unavailable` means an operator must change something before a retry can help, which is
    the opposite of what was measured. Collapsing them is the defect."""
    assert errors.ModelTimedOut.code == "model_timeout"
    assert errors.ModelTimedOut.code != errors.Unavailable.code


class Broken:
    """Fails for a reason that waiting cannot fix."""

    model = "m"

    def __init__(self):
        self.calls = 0

    def chat(self, **kwargs):
        self.calls += 1
        raise ValueError("401 Unauthorized: invalid api key")


def test_a_failure_that_is_not_a_timeout_is_not_retried():
    """A refused key does not become true by waiting, and retrying it doubles the delay before
    anybody sees the real problem."""
    chat = Broken()

    with pytest.raises(ValueError):
        prd_svc.chat_with_retry(chat, provider="openai", model="m",
                                system="s", context="c", question="q")

    assert chat.calls == 1
    

@pytest.mark.parametrize("exc", [
    TimeoutError("timed out"),
    Exception("ReadTimeout"),
    type("ReadTimeout", (Exception,), {})("connection lost"),
])
def test_timeouts_are_recognised_across_provider_shapes(exc):
    """The provider layer is plain httpx for some vendors and an SDK for others. Matching on
    the exception's NAME as well as its text is what keeps this from being tied to the set of
    providers that exist today."""
    assert prd_svc._is_timeout(exc) is True


def test_something_that_merely_mentions_time_is_not_a_timeout():
    """The control for the matcher. Over-matching would retry real failures."""
    assert prd_svc._is_timeout(ValueError("time to fix your config")) is False


# ---- through the MCP surface, which is where the caller actually sees it -------------------


def _rpc(client, key, tool, args=None):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args or {}}},
        headers={"X-API-Key": key},
    ).json()["result"]


def test_grill_prd_reports_whether_it_needed_a_second_attempt(client, auth, db):
    """Reported to the CALLER, not only to the server's log. Somebody whose grill was slow can
    tell contention from a hung server without shell access to the host.

    The field is also declared in the tool's outputSchema, so GRPH-495's conformance probe
    covers it — a manifest that promised `retried` and never emitted it would be the same
    class of defect one layer out.
    """
    key = client.post("/api/api-keys", json={"name": "grill"},
                      headers=auth).json()["plaintext"]
    prd = prd_svc.create_prd(db, title="Spec", project_id="core",
                             body="# Spec\n\n## 1. Overview\n\nSomething.\n")

    out = _rpc(client, key, "grill_prd", {"prd_id": prd.id})

    assert not out.get("isError"), out
    body = out["structuredContent"]
    assert "questions" in body
    assert body["retried"] is False, "the stub provider answers first time"
