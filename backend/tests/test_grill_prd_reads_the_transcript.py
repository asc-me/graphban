"""`grill_prd` asks from the transcript it already stored (GRPH-1123).

The tool reported `dimensions_outstanding` and then asked a fresh list, because
the call was `ai_command_detail(..., "grill")`: context is the PRD body, and
`_COMMANDS["grill"]` says "ask 5-8 questions" with no way to stop. The stream
already had the other prompt. On GRPH-P58 three dimensions were closed and
`grill_prd` still asked about all four.

The question text is not recorded. `records_answers` stays false, and a call
must not append a turn — that is the GRPH-513 split, and recording the question
would make `turns_recorded` lie.
"""
from __future__ import annotations

import json

import pytest

from app.services.platform import Resolved


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _call(client, key, tool, args):
    result = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args}},
        headers={"X-API-Key": key},
    ).json()["result"]
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


def _prd(client, auth):
    return client.post("/api/prds", json={
        "title": "A spec to grill", "project_id": "core",
        "body": "# A spec\n\n## D1 — the thing\n\nbuild the thing.\n",
    }, headers=auth).json()


def _key(client, auth):
    return client.post("/api/api-keys", json={"name": "griller"}, headers=auth).json()["plaintext"]


def _close(db, prd_id, *names):
    from app.services import prds as prd_svc

    for name in names:
        prd_svc.set_dimension(db, prd_id, name, "resolved", note="settled", graded_by="test")


def test_a_settled_dimension_is_not_asked_again(client, auth):
    """GRPH-P58 before the defer: three closed, open_decisions still open.

    The old call asked every dimension every time. Omitting the closed question
    text is the thing a body-only stub cannot do.
    """
    from app.db import SessionLocal
    from app.services import prds as prd_svc

    prd = _prd(client, auth)
    key = _key(client, auth)
    db = SessionLocal()
    try:
        prd_svc.record_grill_turns(
            db, prd["id"],
            [{"role": "user", "text": "Cells come from the posted topology."}],
        )
        _close(db, prd["id"], "scope_edges", "failure_modes", "contracts")
        before = len(prd_svc.grill_turns(db, prd["id"]))
    finally:
        db.close()

    out = _call(client, key, "grill_prd", {"prd_id": prd["id"]})

    assert prd_svc.DIMENSIONS["open_decisions"] in out["questions"]
    for name in ("scope_edges", "failure_modes", "contracts"):
        assert prd_svc.DIMENSIONS[name] not in out["questions"], name
    assert out["questions"].lstrip().startswith("-")
    assert out["records_answers"] is False
    assert out["turns_recorded"] == before
    db = SessionLocal()
    try:
        assert len(prd_svc.grill_turns(db, prd["id"])) == before
    finally:
        db.close()


def test_a_finished_grill_stops_instead_of_asking(client, auth):
    """Once nothing is outstanding the stub says stop. A fresh bullet list here
    is the loop the stall flag was detecting and could not end."""
    from app.db import SessionLocal
    from app.services import prds as prd_svc

    prd = _prd(client, auth)
    key = _key(client, auth)
    db = SessionLocal()
    try:
        prd_svc.record_grill_turns(db, prd["id"], [{"role": "user", "text": "Defer the last one."}])
        _close(db, prd["id"], "scope_edges", "failure_modes", "contracts")
        prd_svc.set_dimension(
            db, prd["id"], "open_decisions", "deferred",
            note="author deferred", graded_by="test",
        )
        before = len(prd_svc.grill_turns(db, prd["id"]))
    finally:
        db.close()

    out = _call(client, key, "grill_prd", {"prd_id": prd["id"]})

    assert not out["questions"].lstrip().startswith("-")
    assert "Stop asking" in out["questions"]
    for question in prd_svc.DIMENSIONS.values():
        assert question not in out["questions"]
    assert out["turns_recorded"] == before
    assert out["records_answers"] is False


def test_the_question_call_sends_this_interrogation_only(db, monkeypatch):
    """Sabotage point is the call, not `grill_context` itself.

    `context=prd.body` drops "Conversation so far". Forgetting `since=grill_window`
    pulls an answer from the previous interrogation into this one.
    """
    from app.services import prds as prd_svc

    prd = prd_svc.create_prd(
        db, title="Spec", project_id="core",
        body="# Spec\n\n## Problem\n\nAgents need the git model named.\n",
    )
    prd_svc.record_grill_turns(
        db, prd.id, [{"role": "user", "text": "OLD-ANSWER-OUTSIDE-THE-WINDOW"}],
    )
    prd.grill_from_seq = 1
    db.commit()
    prd_svc.record_grill_turns(
        db, prd.id, [{"role": "user", "text": "Gitflow is out of scope for v1."}],
    )
    before = len(prd_svc.grill_turns(db, prd.id))
    captured: dict = {}

    class Chat:
        model = "fake"

        def chat(self, **kwargs):
            captured.update(kwargs)
            return "- What happens on the failure path?"

    monkeypatch.setattr(
        prd_svc.platform_svc, "resolve_role",
        lambda _db, _pid, _role: Resolved("anthropic", Chat(), model="fake"),
    )

    text, retried = prd_svc.grill_questions(db, prd.id)

    assert retried is False
    assert text.startswith("-")
    assert captured["system"] is prd_svc.GRILL_CHAT_SYSTEM
    assert "Conversation so far" in captured["context"]
    assert "Gitflow is out of scope for v1." in captured["context"]
    assert "OLD-ANSWER-OUTSIDE-THE-WINDOW" not in captured["context"]
    assert captured["question"] == "Gitflow is out of scope for v1."
    assert len(prd_svc.grill_turns(db, prd.id)) == before


def test_the_editor_command_still_asks_every_dimension(db):
    """The one-shot command has no transcript. Stopping it would look like it had
    read the answers it is not given."""
    from app.services import prds as prd_svc

    prd = prd_svc.create_prd(
        db, title="Editor", project_id="core",
        body="# Editor\n\n## Problem\n\nEnough prose that the section is not thin at all.\n",
    )
    prd_svc.set_dimension(db, prd.id, "contracts", "resolved", note="settled", graded_by="test")

    text = prd_svc.ai_command(db, prd.id, "grill")

    for question in prd_svc.DIMENSIONS.values():
        assert question in text
