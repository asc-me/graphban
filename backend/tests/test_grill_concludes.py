"""The grill can end (AL-298 / PRD-15 D2).

`GRILL_CHAT_SYSTEM` told the model to "keep grilling" with no terminal state, so "all
questions answered" could never become true and approval-by-grilling was unreachable by
construction. Two paths make it terminable: a real model classifies each dimension, and
the offline stub gets a deterministic bar.

The stub half matters more than it looks. `CHAT_PROVIDER=stub` is the shipped default, so
without it no PRD could be approved on a fresh install — which would break the
zero-browser flow AL-283 delivered.
"""
import json

import pytest

from app.services import prds as prd_svc
from app.services.platform import Resolved


@pytest.fixture()
def prd(client, auth):
    r = client.post("/api/prds", json={"title": "Concludes", "project_id": "core"}, headers=auth)
    return r.json()["id"]


def _apply(client, auth, prd_id, history):
    r = client.post(f"/api/prds/{prd_id}/grill/apply", json={"history": history}, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _state(client, auth, prd_id):
    return client.get(f"/api/prds/{prd_id}/grill", headers=auth).json()


def _qa(n: int) -> list[dict]:
    out = []
    for i in range(n):
        out += [{"role": "agent", "text": f"Question {i}?"},
                {"role": "user", "text": f"A substantive answer about topic {i}."}]
    return out


# ---- the offline stub can conclude ----------------------------------------------------
def test_the_stub_asks_one_question_per_dimension(client):
    """The offline grill must ask exactly what the standard grades. Previously a fixed
    list that only overlapped the dimensions by coincidence."""
    from app.models import Prd

    text = prd_svc._stub_command("grill", Prd(id="x", title="t", body="## A\n\nshort"))
    for question in prd_svc.DIMENSIONS.values():
        assert question in text, question


def test_a_stub_grill_reaches_complete(client, auth, prd):
    """The shipped default must be able to approve something, or the zero-browser install
    dead-ends at the first PRD."""
    assert _state(client, auth, prd)["complete"] is False
    _apply(client, auth, prd, _qa(len(prd_svc.DIMENSIONS)))
    state = _state(client, auth, prd)
    assert state["complete"] is True, state["outstanding"]
    assert state["outstanding"] == []


def test_the_stub_resolves_only_as_many_dimensions_as_answers(client, auth, prd):
    """The bar is mechanical but not free — two answers cannot clear four dimensions."""
    _apply(client, auth, prd, _qa(2))
    state = _state(client, auth, prd)
    assert state["complete"] is False
    assert len(state["outstanding"]) == 2


def test_the_stub_says_it_did_not_assess_substance(client, auth, prd):
    """A stub cannot judge substance, and pretending otherwise would be worse than
    admitting it. The note is what tells a reader which bar was actually applied."""
    _apply(client, auth, prd, _qa(1))
    note = _state(client, auth, prd)["dimensions"]["scope_edges"]["note"]
    assert "substance not assessed" in note, note


# ---- a real model classifies ----------------------------------------------------------
def _fake_model(monkeypatch, payload: str):
    """Stand in for a configured chat provider; `_classify_dimensions` bails on stub."""
    class _Chat:
        def chat(self, **kw):
            return payload

    monkeypatch.setattr(prd_svc.platform_svc, "resolve_chat",
                        lambda db, pid: Resolved("anthropic", _Chat()))


def test_a_real_model_verdict_is_recorded(client, auth, prd, monkeypatch):
    # Citations are mandatory: each verdict names the answer that settled it.
    _fake_model(monkeypatch, json.dumps({
        "scope_edges": {"outcome": "resolved", "note": "local only",
                        "answered_by": 1},
        "failure_modes": {"outcome": "resolved", "note": "retries once",
                          "answered_by": 2},
        "contracts": {"outcome": "deferred", "note": "after the prototype",
                      "answered_by": 1},
        "open_decisions": {"outcome": "unanswered", "note": "not discussed"},
    }))
    _apply(client, auth, prd, _qa(2))

    state = _state(client, auth, prd)
    assert state["dimensions"]["contracts"]["outcome"] == "deferred"
    assert state["outstanding"] == ["open_decisions"]
    assert state["complete"] is False


def test_an_evasive_answer_is_not_completion(client, auth, prd, monkeypatch):
    """The distinction the whole standard rests on: 'we'll figure it out later' without
    an explicit decision to defer is `unanswered`, not `resolved`."""
    _fake_model(monkeypatch, json.dumps(
        {name: {"outcome": "unanswered", "note": "hand-waved"} for name in prd_svc.DIMENSIONS}
    ))
    _apply(client, auth, prd, _qa(4))
    assert _state(client, auth, prd)["complete"] is False


@pytest.mark.parametrize("payload", ["not json at all", "{}", '{"scope_edges": "resolved"}',
                                     '{"scope_edges": {"outcome": "vibes"}}'])
def test_an_unparseable_verdict_records_nothing_rather_than_erroring(client, auth, prd, monkeypatch, payload):
    """A malformed reply must not break the grill and must not invent outcomes.

    It also must not fall back to the OFFLINE bar. That is what this originally asserted,
    and it was wrong: applying the stub's mechanical rule after a real model failed grades
    with a weaker standard than the one that just refused to answer, on a project that
    pays for a model. Nothing is recorded; the next round tries again."""
    _fake_model(monkeypatch, payload)
    _apply(client, auth, prd, _qa(1))
    state = _state(client, auth, prd)
    assert state["dimensions"]["scope_edges"]["outcome"] == "unanswered"
    assert state["complete"] is False


def test_a_model_outage_does_not_break_classification(client, auth, prd, monkeypatch):
    """Scoped to `classify_grill`. `grill_apply`'s own body-synthesis call is separately
    unguarded and surfaces a provider outage as an error — pre-existing, deliberately
    unchanged, since swallowing it would return an unedited PRD as though the rewrite had
    succeeded.

    An outage leaves the dimensions alone rather than grading them offline: a provider
    being down is not evidence about what the author said."""
    from app.db import SessionLocal

    class _Boom:
        def chat(self, **kw):
            raise RuntimeError("provider down")

    monkeypatch.setattr(prd_svc.platform_svc, "resolve_chat", lambda db, pid: Resolved("anthropic", _Boom()))
    db = SessionLocal()
    try:
        prd_svc.record_grill_turns(db, prd, _qa(1))
        done = prd_svc.classify_grill(db, prd_svc.get_prd(db, prd))  # must not raise
        assert done["dimensions"]["scope_edges"]["outcome"] == "unanswered"
        assert done["answers"] == 1, "the answer itself is still on record"
    finally:
        db.close()


# ---- deferral is the author's, and it sticks ------------------------------------------
def test_the_author_can_defer_explicitly(client, auth, prd):
    """Deferring is a decision, not something a model infers — and on a stub instance
    this route is the only way to record one."""
    r = client.post(f"/api/prds/{prd}/grill/defer",
                    json={"dimension": "contracts", "reason": "wire format after the spike"},
                    headers=auth)
    assert r.status_code == 200, r.text
    d = r.json()["dimensions"]["contracts"]
    assert d["outcome"] == "deferred" and "spike" in d["note"]


def test_a_later_round_never_downgrades_a_deferral(client, auth, prd, monkeypatch):
    """An author's decision to leave something open is theirs. A later classification
    that didn't see the deferral restated must not quietly reopen it — that would make
    deferral useless in any conversation that continues."""
    client.post(f"/api/prds/{prd}/grill/defer",
                json={"dimension": "contracts", "reason": "after the spike"}, headers=auth)
    _fake_model(monkeypatch, json.dumps(
        {name: {"outcome": "unanswered", "note": "reopened"} for name in prd_svc.DIMENSIONS}
    ))
    _apply(client, auth, prd, _qa(1))
    assert _state(client, auth, prd)["dimensions"]["contracts"]["outcome"] == "deferred"


def test_a_deferred_dimension_lets_the_grill_complete(client, auth, prd):
    """End to end on the stub: three answers plus one explicit deferral is a finished
    grill. This is the AL-303 case that must work on the shipped default."""
    _apply(client, auth, prd, _qa(3))
    client.post(f"/api/prds/{prd}/grill/defer",
                json={"dimension": "open_decisions", "reason": "pricing after beta"},
                headers=auth)
    state = _state(client, auth, prd)
    assert state["complete"] is True, state["outstanding"]
    assert state["deferred"] == ["open_decisions"]


def test_an_unknown_dimension_is_refused(client, auth, prd):
    r = client.post(f"/api/prds/{prd}/grill/defer",
                    json={"dimension": "vibes", "reason": "x"}, headers=auth)
    assert r.status_code == 422, r.text
