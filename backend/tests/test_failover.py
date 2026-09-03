"""Failover, and saying which credential answered (PRD-25 S3, GRPH-509).

Four claims, and the ones that are easy to ship broken are the refusals.

**A 401 must NOT fail over.** A version that falls over on everything passes every
happy-path test here — it only diverges on the errors it should have left alone, and each
divergence is a second call to be told the same thing, billed.

**Unknown statuses are terminal.** Asserted with a made-up vendor code, because the default is
the decision: an unclassified error failing over is how one bug doubles every bill.

**Both failing must not flatten.** The primary occupies `message` so existing log and alert
paths see the failure the operator must act on; the fallback sits under its own key so nothing
downstream merges two failures into one string and loses which was which.

**`source`/`answered_by` must name the credential that answered.** A response that quietly came
from the fallback is a cost and quality change nobody consented to.
"""
from __future__ import annotations

import httpx
import pytest

from app.errors import Unavailable
from app.models import Credential, DeploymentConfig, Project
from app.providers.base import RETRYABLE_STATUSES, provider_errors
from app.security import secrets
from app.services import platform as platform_svc
from app.services.failover import BothFailed, FailoverChat, is_retryable


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def project(db):
    p = Project(id="p1", name="P1", tag="P1")
    db.add(p)
    db.commit()
    return p


def _cred(db, cid, *, state="valid", model="m", kind="anthropic"):
    c = Credential(id=cid, kind=kind, model=model, label=cid, state=state,
                   api_key=secrets.encrypt("sk"))
    db.add(c)
    db.commit()
    return c


def _raising(exc):
    class Boom:
        def chat(self, **kw):
            raise exc

    return Boom()


def _answering(text="ok"):
    class Fine:
        def chat(self, **kw):
            return text

    return Fine()


def _http_error(status: int):
    """A real provider error, produced the way production produces one — through
    `provider_errors`, so the status tagging under test is the tagging that ships."""
    request = httpx.Request("POST", "http://provider.test/v1/chat")
    response = httpx.Response(status, request=request, text="upstream said no")
    try:
        with provider_errors("anthropic", endpoint="http://provider.test"):
            raise httpx.HTTPStatusError("boom", request=request, response=response)
    except Unavailable as e:
        return e
    raise AssertionError("provider_errors did not convert the error")


# ---- the taxonomy -------------------------------------------------------------------------


#: Named explicitly, NOT derived from `RETRYABLE_STATUSES`. The first version of this test
#: parametrized over the constant it was testing and asserted each member was retryable —
#: which `_tagged` guarantees by construction, so it could not fail. Widening the set just
#: produced more passing cases; the test count going UP under a sabotage is what exposed it.
MUST_FAIL_OVER = (429, 500, 502, 503, 504)


@pytest.mark.parametrize("status", MUST_FAIL_OVER)
def test_these_statuses_are_worth_a_second_credential(status):
    """Throttling and upstream faults are about the provider's moment, not the credential."""
    assert is_retryable(_http_error(status)) is True


def test_the_retryable_set_still_contains_what_this_file_requires():
    """Pins the constant against the list above, so narrowing the set fails HERE rather than
    silently reducing what the parametrized test covers."""
    missing = [s for s in MUST_FAIL_OVER if s not in RETRYABLE_STATUSES]
    assert not missing, f"{missing} dropped out of RETRYABLE_STATUSES"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_credential_and_request_failures_are_terminal(status):
    """"A bad credential does not become true by waiting" — and it does not become true by
    being asked somewhere else either."""
    assert is_retryable(_http_error(status)) is False


def test_an_unknown_vendor_status_is_terminal(db):
    """THE DEFAULT, asserted with a status nobody has classified. Failing over on an
    unclassified error is how one bug becomes a doubled bill on every call."""
    assert is_retryable(_http_error(418)) is False


def test_an_error_carrying_no_verdict_at_all_is_terminal():
    """An exception from somewhere that never went through `provider_errors` has no opinion
    about retrying, and absence of a verdict is not permission to spend money."""
    assert is_retryable(RuntimeError("something else entirely")) is False


def test_a_timeout_is_retryable():
    """Not a status at all. `provider_errors` tags it directly, and its own hint already says
    "this one IS worth retrying"."""
    request = httpx.Request("POST", "http://provider.test/v1/chat")
    try:
        with provider_errors("anthropic", endpoint="http://provider.test"):
            raise httpx.TimeoutException("slow", request=request)
    except Unavailable as e:
        assert is_retryable(e) is True


# ---- the failover itself -------------------------------------------------------------------


def test_a_429_fails_over_and_names_the_fallback():
    chat = FailoverChat(primary=_raising(_http_error(429)), primary_id="cred_a",
                        fallback=_answering("from the fallback"), fallback_id="cred_b")

    answer = chat.chat(system="s", context="c", question="q")

    assert answer == "from the fallback"
    assert chat.answered_by == "cred_b" and chat.failed_over is True


def test_a_401_does_not_fail_over_and_the_error_survives_intact(db):
    """The original exception is re-raised, not wrapped and not reworded: a caller that
    already handles `Unavailable` must keep handling it the same way."""
    original = _http_error(401)
    fallback = _answering("should never be reached")
    chat = FailoverChat(primary=_raising(original), primary_id="cred_a",
                        fallback=fallback, fallback_id="cred_b")

    with pytest.raises(Unavailable) as caught:
        chat.chat(system="s", context="c", question="q")

    assert caught.value is original, "the error was wrapped or replaced"
    assert chat.failed_over is False and chat.answered_by == ""


def test_a_successful_primary_names_itself():
    """The counterpart. If `answered_by` were only set on failover it would carry no
    information on the ordinary path."""
    chat = FailoverChat(primary=_answering(), primary_id="cred_a",
                        fallback=_answering(), fallback_id="cred_b")

    chat.chat(system="s", context="c", question="q")

    assert chat.answered_by == "cred_a" and chat.failed_over is False


def test_with_no_fallback_configured_the_error_surfaces_unchanged():
    """Failover is opt-in. A deployment that has not configured one must behave exactly as it
    did before this slice existed."""
    original = _http_error(429)
    chat = FailoverChat(primary=_raising(original), primary_id="cred_a")

    with pytest.raises(Unavailable) as caught:
        chat.chat(system="s", context="c", question="q")

    assert caught.value is original


# ---- both failing --------------------------------------------------------------------------


def test_both_failing_leads_with_the_primary_and_does_not_flatten():
    """The shape IS the guarantee. Concatenating into one string is what a downstream logger
    would do, and then nobody can tell which credential produced which half."""
    primary, fallback = _http_error(429), _http_error(503)
    chat = FailoverChat(primary=_raising(primary), primary_id="cred_a",
                        fallback=_raising(fallback), fallback_id="cred_b")

    with pytest.raises(BothFailed) as caught:
        chat.chat(system="s", context="c", question="q")

    err = caught.value.as_error()
    assert err["code"] == "provider_failed"
    assert err["credential_id"] == "cred_a"
    assert err["message"] == str(primary), "the primary must occupy the ordinary message field"
    assert err["also_failed"] == {"credential_id": "cred_b", "message": str(fallback)}
    assert str(fallback) not in err["message"], (
        "the two failures were concatenated — a downstream logger can no longer tell them apart"
    )


# ---- resolution wires it up -----------------------------------------------------------------


def test_resolution_wraps_the_chat_when_a_fallback_is_configured(db, project):
    a, b = _cred(db, "cred_a"), _cred(db, "cred_b")
    db.add(DeploymentConfig(scope="", default_credential_id=a.id, fallback_credential_id=b.id))
    db.commit()

    resolved = platform_svc.resolve_chat(db, "p1")

    assert isinstance(resolved.chat, FailoverChat)
    assert resolved.chat.primary_id == "cred_a" and resolved.chat.fallback_id == "cred_b"


def test_resolution_does_not_wrap_when_no_fallback_is_set(db, project):
    """A deployment without a fallback gets exactly the object it got before S3."""
    a = _cred(db, "cred_a")
    db.add(DeploymentConfig(scope="", default_credential_id=a.id))
    db.commit()

    assert not isinstance(platform_svc.resolve_chat(db, "p1").chat, FailoverChat)


def test_the_fallback_is_never_the_credential_that_just_resolved(db, project):
    """Falling over to the credential that just failed spends a second call to be told the
    same thing."""
    a = _cred(db, "cred_a")
    db.add(DeploymentConfig(scope="", default_credential_id=a.id, fallback_credential_id=a.id))
    db.commit()

    assert not isinstance(platform_svc.resolve_chat(db, "p1").chat, FailoverChat)


def test_an_unreachable_fallback_is_skipped_not_probed(db, project):
    """Grill decision: no synchronous probe on the request path. An unbounded network call
    inside a user-facing request, to rescue a configuration the operator already broke, turns
    one slow upstream into two."""
    a = _cred(db, "cred_a")
    b = _cred(db, "cred_b", state="unreachable")
    db.add(DeploymentConfig(scope="", default_credential_id=a.id, fallback_credential_id=b.id))
    db.commit()

    assert not isinstance(platform_svc.resolve_chat(db, "p1").chat, FailoverChat)


def test_a_project_pointer_also_gets_the_deployment_fallback(db, project):
    """"Applies to whatever resolved, project pointer or default alike" — a project that
    overrode the default still deserves the deployment's safety net."""
    own, b = _cred(db, "cred_own"), _cred(db, "cred_b")
    db.add(DeploymentConfig(scope="", fallback_credential_id=b.id))
    project.credential_id = own.id
    db.commit()

    resolved = platform_svc.resolve_chat(db, "p1")

    assert isinstance(resolved.chat, FailoverChat)
    assert resolved.chat.primary_id == "cred_own"


# ---- an empty answer fails over ---------------------------------------------------------


def test_an_empty_answer_from_the_primary_fails_over(db, project):
    """`require_answer` tags `EmptyAnswer` retryable, and the failover reads that tag —
    so a provider that answers nothing hands the question to the second credential
    instead of handing "" to the caller. The live instance did the latter for a grill."""
    from app.providers.base import require_answer

    try:
        require_answer("   ", "openai-compat", model="grok-4.5", endpoint="https://api.x.ai/v1")
    except Unavailable as e:
        empty = e
    else:
        raise AssertionError("require_answer accepted a blank answer")

    assert is_retryable(empty) is True
    chat = FailoverChat(primary=_raising(empty), primary_id="cred_primary",
                        fallback=_answering("from the fallback"), fallback_id="cred_fallback")
    assert chat.chat(system="s", context="c", question="q") == "from the fallback"
    assert chat.answered_by == "cred_fallback"
    assert chat.failed_over is True
