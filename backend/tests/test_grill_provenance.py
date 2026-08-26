"""Who answered, and which provider set the bar (AL-299 / PRD-15 D3).

Approval is derived from the grill, so a baseline is only as trustworthy as what stands
behind it. Two facts a reader needs and previously could not recover: where each answer
came from, and what graded the dimensions.

The second carries the weight. On the shipped default (`CHAT_PROVIDER=stub`) the bar is
mechanical — `approved` means "four answers were recorded", not "four questions were
answered well". Without `graded_by` a stub-graded baseline and a model-graded one are
indistinguishable, which is the failure this item exists to prevent.

This item also adds `answer_grill`. Without an MCP path to record an answer, "agent-
relayed" was a label for a state that could not occur, while PRD-15's acceptance requires
an agent to relay answers with nobody visiting a settings screen.
"""
import json

import pytest

from app.services import prds as prd_svc
from app.services.platform import Resolved


@pytest.fixture()
def prd(client, auth):
    r = client.post("/api/prds", json={"title": "Provenance", "project_id": "core"}, headers=auth)
    return r.json()["id"]


@pytest.fixture()
def agent_key(client, auth):
    r = client.post("/api/api-keys", json={"name": "relay", "scopes": ["read", "write"]},
                    headers=auth)
    return r.json()["plaintext"]


def _mcp(client, api_key, tool, args):
    r = client.post("/api/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": tool, "arguments": args}},
                    headers={"X-API-Key": api_key})
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    if result.get("isError"):
        return {"__ERR__": result["structuredContent"]["error"]}
    return json.loads(result["content"][0]["text"])


def _state(client, auth, prd_id):
    return client.get(f"/api/prds/{prd_id}/grill", headers=auth).json()


# ---- the agent path this item had to add ---------------------------------------------
def test_an_agent_can_relay_an_answer(client, auth, agent_key, prd):
    """PRD-15's acceptance requires an agent to record answers with nobody visiting a
    settings screen. Before this there was no MCP path to do it at all — `grill_prd` is
    read-only — so the whole relayed flow was unreachable."""
    out = _mcp(client, agent_key, "answer_grill",
               {"prd_id": prd, "answer": "Scope stops at the local instance."})
    assert out["answers"] == 1
    assert out["complete"] is False
    assert len(out["outstanding"]) == len(prd_svc.DIMENSIONS) - 1


def test_a_relayed_answer_is_labelled_as_relayed(client, auth, agent_key, prd):
    """The point of the label: a reviewer can tell an answer a person typed from one an
    agent reported. Neither is blocked; they are simply not the same evidence."""
    _mcp(client, agent_key, "answer_grill", {"prd_id": prd, "answer": "Postgres only."})
    turn = [t for t in _state(client, auth, prd)["turns"] if t["role"] == "user"][0]
    assert turn["via"] == "agent"
    assert turn["actor"].startswith("agent:")


def test_an_answer_typed_in_a_session_is_labelled_human(client, auth, prd):
    r = client.post(f"/api/prds/{prd}/grill/apply",
                    json={"history": [{"role": "user", "text": "We only support Postgres."}]},
                    headers=auth)
    assert r.status_code == 200, r.text
    turn = [t for t in _state(client, auth, prd)["turns"] if t["role"] == "user"][0]
    assert turn["via"] == "human"
    assert turn["actor"] and not turn["actor"].startswith("agent:")


def test_a_question_has_no_supplier(client, auth, prd):
    """Only an answer has someone behind it; the questions come from the grill."""
    client.post(f"/api/prds/{prd}/grill/apply",
                json={"history": [{"role": "agent", "text": "What is out of scope?"}]},
                headers=auth)
    question = [t for t in _state(client, auth, prd)["turns"] if t["role"] == "agent"][0]
    assert question["via"] == "" and question["actor"] == ""


def test_an_empty_relayed_answer_is_refused(client, auth, agent_key, prd):
    """An agent must relay what the author said, not manufacture engagement out of
    whitespace — a blank answer would still tick a dimension under the stub rule."""
    err = _mcp(client, agent_key, "answer_grill", {"prd_id": prd, "answer": "   "})
    assert err["__ERR__"]["code"] == "validation", err


def test_relaying_into_another_projects_prd_is_refused(client, auth, prd):
    r = client.post("/api/projects", json={"name": "Elsewhere", "tag": "ELS"}, headers=auth)
    other = r.json()["id"]
    key = client.post("/api/api-keys",
                      json={"name": "pinned", "project_id": other, "scopes": ["read", "write"]},
                      headers=auth).json()["plaintext"]
    err = _mcp(client, key, "answer_grill", {"prd_id": prd, "answer": "Not mine to answer."})
    assert err["__ERR__"]["code"] == "unauthorized", err


# ---- which provider set the bar -------------------------------------------------------
def test_a_stub_graded_dimension_says_stub(client, auth, agent_key, prd):
    """The one that matters most. On the shipped default `approved` means "answers were
    recorded", not "answers were good" — and a reader has to be able to see that."""
    _mcp(client, agent_key, "answer_grill", {"prd_id": prd, "answer": "Local only."})
    assert _state(client, auth, prd)["dimensions"]["scope_edges"]["graded_by"] == "stub"


def test_a_model_graded_dimension_names_the_provider(client, auth, agent_key, prd, monkeypatch):
    class _Chat:
        def chat(self, **kw):
            # Must name a real answer — the classifier rejects an uncited verdict.
            return json.dumps({n: {"outcome": "resolved", "note": "ok",
                                   "answered_by": 1}
                               for n in prd_svc.DIMENSIONS})

    monkeypatch.setattr(prd_svc.platform_svc, "resolve_chat", lambda db, pid: Resolved("anthropic", _Chat()))
    _mcp(client, agent_key, "answer_grill", {"prd_id": prd, "answer": "A real answer."})
    dims = _state(client, auth, prd)["dimensions"]
    assert dims["scope_edges"]["graded_by"] == "anthropic"


def test_a_failed_model_verdict_is_credited_to_nothing(
    client, auth, agent_key, prd, monkeypatch
):
    """A real model IS configured but its reply is unparseable. Nothing may be recorded:
    not under the model's name, and not under the stub's either.

    (Found by sabotage: replacing the grader with a bare provider lookup left every test
    green, because on a stub instance "what is configured" and "what graded" coincide.)"""
    class _Garbage:
        def chat(self, **kw):
            return "I think it's probably fine?"

    monkeypatch.setattr(prd_svc.platform_svc, "resolve_chat", lambda db, pid: Resolved("anthropic", _Garbage()))
    _mcp(client, agent_key, "answer_grill", {"prd_id": prd, "answer": "An answer."})

    d = _state(client, auth, prd)["dimensions"]["scope_edges"]
    # Originally this asserted the stub rule decided and was labelled `stub`. Labelling
    # made the weak grading visible, but it still HAPPENED — a real model failing should
    # not silently hand grading to a weaker offline rule. Now nothing is recorded, which
    # is both honest and unmistakable.
    assert d["outcome"] == "unanswered"
    assert d["graded_by"] == "", "a failed model reply must not be credited to anything"


def test_an_explicit_deferral_is_graded_by_the_author(client, auth, prd):
    """A deferral is the author's decision, so attributing it to a model would be wrong
    — and on a stub instance would read as though the stub had assessed it."""
    client.post(f"/api/prds/{prd}/grill/defer",
                json={"dimension": "contracts", "reason": "after the spike"}, headers=auth)
    assert _state(client, auth, prd)["dimensions"]["contracts"]["graded_by"] == "author"


def test_a_stub_and_a_model_baseline_are_distinguishable(client, auth, agent_key, prd, monkeypatch):
    """Stated as its own test because it is the whole justification for the column: two
    PRDs can both be `complete` and mean very different things."""
    for _ in range(len(prd_svc.DIMENSIONS)):
        _mcp(client, agent_key, "answer_grill", {"prd_id": prd, "answer": "An answer."})
    stub_state = _state(client, auth, prd)
    assert stub_state["complete"] is True
    assert {d["graded_by"] for d in stub_state["dimensions"].values()} == {"stub"}

    r = client.post("/api/prds", json={"title": "Judged", "project_id": "core"}, headers=auth)
    judged = r.json()["id"]

    class _Chat:
        def chat(self, **kw):
            return json.dumps({n: {"outcome": "resolved", "note": "substantive",
                                   "answered_by": 1}
                               for n in prd_svc.DIMENSIONS})

    monkeypatch.setattr(prd_svc.platform_svc, "resolve_chat", lambda db, pid: Resolved("ollama", _Chat()))
    _mcp(client, agent_key, "answer_grill", {"prd_id": judged, "answer": "A real answer."})
    judged_state = _state(client, auth, judged)
    assert judged_state["complete"] is True
    assert {d["graded_by"] for d in judged_state["dimensions"].values()} == {"ollama"}
