"""A decider that cannot answer must say so (GRPH-995, PRD-45 §9).

Found on the GRPH-899 demo walk. An empty-model TypeSafe row probed `valid` — the probe asks
about health, never about `decide()` — so it became the deployment default, and from then on
every memory adjudication on the box failed at runtime while the console showed a green row
and `falling_back` stayed empty. The only symptom anywhere was one shard coming back
ungraded: an absence that read as a clean result on every surface that mattered.

Two holes, and each has its own assertion here:

1. nothing RECORDED the runtime failure, so a `valid` row was indistinguishable from a
   working judge;
2. even recorded, `falling_back` was built only from explicit `decider_credential_id`
   pointers, so the projects that INHERITED the broken default — all of them, in the
   incident — were invisible.

**These tests sabotage the CALL.** A real credential row, the real `resolve_decider`, the real
listing. `test_memory_decider.py` patches `resolve_decider` wholesale, which is right for its
verdict-shape questions and wrong here: that `Resolved` carries no `credential_id`, so the
recording step would have no row to write to and this file would pass with the fix deleted.
`_build` replaces only `providers.build_decider`.
"""
from __future__ import annotations

import pytest

from app.models import Credential
from app.providers.decide import Answer, Decision
from app.security import secrets
from app.services import memory as mem_svc
from app.services import platform as platform_svc


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _proj(client, auth, name):
    return client.post("/api/projects", json={"name": name}, headers=auth).json()["id"]


def _key(client, auth, **body):
    return client.post("/api/api-keys", json={"name": "mem", **body},
                       headers=auth).json()["plaintext"]


def _mcp(client, key, tool, args):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args}},
        headers={"X-API-Key": key},
    ).json()["result"]["structuredContent"]


def _judge_on(client, auth, pid):
    """Only the toggle. `memory_auto_reject` stays at its default so the write path reaches
    the judge at all — with mode `review` and auto-reject off, `auto_triage` returns before
    scoring anything."""
    r = client.patch(f"/api/projects/{pid}", json={"memory_llm_judge": True}, headers=auth)
    assert r.status_code == 200, r.text


def _decider_row(db, cid="cred_mute", *, state="valid", model="", label=""):
    """The row from the incident: `valid`, because the probe only asked about health, and an
    EMPTY model, because nothing ever asked whether that model could decide."""
    c = Credential(id=cid, kind="systemone", org_id=None, model=model, label=label or cid,
                   api_key=secrets.encrypt("sk-live"), state=state)
    db.add(c)
    db.commit()
    return c


class MuteDecider:
    """`decide()` raises. A health probe passes against this; the call does not."""

    model = ""

    def __init__(self):
        self.calls = 0

    def decide(self, *, state, questions):
        self.calls += 1
        raise RuntimeError("model '' is not served by this endpoint")


class AnsweringDecider:
    model = "laya-1"

    def __init__(self, *, keep=0.9, quality=4.0):
        self.keep, self.quality, self.calls = keep, quality, 0

    def decide(self, *, state, questions):
        self.calls += 1
        answers = {}
        for q in questions:
            key = q.key if hasattr(q, "key") else q
            if key == "contradicts":
                answers[key] = Answer(value="none", confidence=1.0)
            elif key == "keep":
                answers[key] = Answer(value=float(self.keep), confidence=1.0)
            elif key == "quality":
                answers[key] = Answer(value=float(self.quality), confidence=1.0)
            else:
                answers[key] = Answer(value=0.9, confidence=1.0)
        return Decision(answers=answers, model=self.model)


def _build(monkeypatch, decider):
    from app import providers

    monkeypatch.setattr(providers, "build_decider", lambda *a, **k: decider)
    return decider


def _status(client, auth, pid):
    r = client.get(f"/api/memory/judge-status?project_id={pid}", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _rows(db):
    """The listing, read fresh.

    `db.rollback()` + `expire_all()` are not ceremony: the failure is recorded by the APP's
    session (a different one) and committed there, while this fixture session is still inside
    the read transaction its first listing started — so it would hand back identity-mapped
    rows from a snapshot that predates the recording, and the assertion would test nothing.
    """
    db.rollback()
    db.expire_all()
    return {r["id"]: r for r in platform_svc.list_credentials(db, "")}


# ---- hole 1: nothing recorded the runtime failure ---------------------------------------


def test_a_decide_failure_moves_the_row_the_probe_called_valid(client, auth, db, monkeypatch):
    """THE ENABLING FACT. `state=valid` was a true answer to a question nobody needed asked,
    and every reader of `state` — `usable`, `resolve_decider`, the console chip,
    `falling_back` — took it for "this judge works".

    Sabotage: delete the `note_decide_failure` call from `_decider_judge`. This fails, and
    every verdict-shape test in `test_memory_decider.py` still passes.
    """
    pid = _proj(client, auth, "MuteDecider")
    _judge_on(client, auth, pid)
    key = _key(client, auth, project_id=pid)
    cred = _decider_row(db)
    platform_svc.set_scope_defaults(db, "", decider_credential_id=cred.id)
    boom = _build(monkeypatch, MuteDecider())

    s = _mcp(client, key, "add_memory", {"text": "always pin the pgvector image to pg16 in CI"})

    assert boom.calls == 1, "the decider was never asked, so this proves nothing"
    assert s["status"] == "candidate", "a shard the judge could not grade must not move"
    assert s["scoring_source"] != "decider"
    db.refresh(cred)
    assert cred.state == "unreachable", (
        "the row still says valid — nothing downstream can tell this decider from a working one")
    assert "decide() failed at runtime" in cred.last_error
    assert "not served by this endpoint" in cred.last_error, "the reason must name the miss"


def test_the_review_queue_alone_records_the_failure(client, auth, db, monkeypatch):
    """The SECOND call site. `_decider_judge` (write) and `_decider_review_judge` (queue)
    catch their own exceptions separately, so a fix in only one leaves the other mute — and
    the queue is the path the Memory review page actually runs.

    The shard is written with the judge OFF, so nothing has recorded a failure yet.
    """
    pid = _proj(client, auth, "QueueOnly")
    key = _key(client, auth, project_id=pid)
    cred = _decider_row(db, "cred_queue")
    platform_svc.set_scope_defaults(db, "", decider_credential_id=cred.id)
    s = _mcp(client, key, "add_memory", {"text": "a novel convention about retry budgets"})
    assert s["status"] == "candidate"
    assert cred.state == "valid"

    _judge_on(client, auth, pid)
    boom = _build(monkeypatch, MuteDecider())
    scored = client.get(f"/api/memory/candidates/scored?project_id={pid}", headers=auth).json()

    row = next(r for r in scored if r["shard"]["id"] == s["id"])
    assert boom.calls >= 1
    assert row["judged"] is False
    assert row["judge_source"] != "decider"
    assert row["ungraded_reason"] == mem_svc.JUDGE_CAUSES["error"], (
        "the row must name the miss, not render as an unasked question")
    db.refresh(cred)
    assert cred.state == "unreachable", "the queue path caught the failure and recorded nothing"


def test_recording_a_failure_with_no_row_is_a_no_op(client, db):
    """A decider that did not come from a credential row has nothing to record against —
    which is how `test_memory_decider.py` builds one. An exception here would turn "the
    judge is down" into a 500 on the memory write path, the exact thing the `except` around
    `decide()` exists to prevent."""
    assert platform_svc.note_decide_failure(db, "") is False
    assert platform_svc.note_decide_failure(db, "cred_never_existed") is False
    assert platform_svc.note_decide_success(db, "") is False
    assert platform_svc.note_decide_success(db, "cred_never_existed") is False


def test_a_blip_then_recovers_clears_falling_back(client, auth, db, monkeypatch):
    """THE BOUNCE. Recording is one-way without this: one timeout becomes a permanent
    banner. The Memory review page then says the judge is down while it is grading —
    the inverse of the absence this file exists to catch.

    Measured on the deployment default because an unreachable default is still
    resolved and called (S2). A project pointer is routed off after one timeout
    (`usable` is False) and recovered by the retry sweep, not this path.

    Sabotage the CALL: delete `note_decide_success` from `_decider_judge`. This fails,
    and every one-way recording test above stays green.
    """
    pid = _proj(client, auth, "BlipThenRecovers")
    _judge_on(client, auth, pid)
    key = _key(client, auth, project_id=pid)
    cred = _decider_row(db, "cred_blip", model="laya-1")
    # Deployment default, not a project pointer: an unreachable default is still
    # resolved and called (S2 asymmetry), so the later answer can restore the row.
    # A project pointer would be routed off after one timeout and never asked again.
    platform_svc.set_scope_defaults(db, "", decider_credential_id=cred.id)
    boom = _build(monkeypatch, MuteDecider())

    first = _mcp(client, key, "add_memory",
                 {"text": "always pin the pgvector image to pg16 in CI"})
    assert boom.calls == 1
    db.refresh(cred)
    assert cred.state == "unreachable"
    assert first["scoring_source"] != "decider"
    assert _status(client, auth, pid)["falling_back"] is True

    good = _build(monkeypatch, AnsweringDecider())
    second = _mcp(client, key, "add_memory",
                  {"text": "HNSW not ivfflat — ivfflat built empty silently loses recall"})
    assert good.calls == 1, (
        "the unreachable default was not asked again — S2 still returns it")
    # Assert on the WRITE path before GET scored: that path runs `_decider_review_judge`,
    # which has its own `note_decide_success`, and would mask a deleted write-path call.
    db.refresh(cred)
    assert cred.state == "valid", "a later answer must undo the runtime mark"
    assert not (cred.last_error or "").startswith("decide() failed at runtime")
    status = _status(client, auth, pid)
    assert status["falling_back"] is False and status["judge"] == "decider"


def test_a_probe_unreachable_row_is_not_restored_by_a_live_answer(client, db):
    """`note_decide_success` only clears a mark THIS path wrote. A probe-unreachable
    row is a different fact — restoring it because one call succeeded would hide a
    host that still cannot be listed."""
    cred = _decider_row(db, "cred_probe_down", state="unreachable")
    cred.last_error = "connection refused"
    db.commit()

    assert platform_svc.note_decide_success(db, cred.id) is False
    db.refresh(cred)
    assert cred.state == "unreachable"
    assert cred.last_error == "connection refused"


def test_a_decider_that_cannot_even_be_built_still_reports(client, auth, db, monkeypatch):
    """`build_decider` raises for a kind it cannot serve, and a hand-edited or half-migrated
    row can name one. The page whose whole job is to report a broken judge must not break
    itself: a 500 here renders as NO banner, which is the absence reading as a clean result
    one level up. So the status still answers, on the next rung down."""
    pid = _proj(client, auth, "Unbuildable")
    _judge_on(client, auth, pid)
    cred = _decider_row(db, "cred_unbuildable")
    platform_svc.set_scope_defaults(db, "", decider_credential_id=cred.id)

    def unbuildable(*_a, **_k):
        raise ValueError("unknown decider kind")

    from app import providers
    monkeypatch.setattr(providers, "build_decider", unbuildable)

    status = _status(client, auth, pid)  # a 500 here fails the request, not just an assert
    assert status["decider_configured"] is False
    assert status["falling_back"] is True and status["judge"] == "similarity"
    assert status["reason"] == mem_svc.JUDGE_CAUSES["no_provider"]


# ---- hole 2: falling_back could not see an inherited pointer ----------------------------


def test_falling_back_names_the_projects_that_inherited_the_broken_default(
        client, auth, db, monkeypatch):
    """THE POINT for the console. In the incident this list was empty while every project on
    the deployment was scoring memory on similarity alone: the row was `valid`, and even once
    it was not, a project with no pointer of its own was invisible to a field built only from
    explicit pointers.

    Sabotage: drop the inheritor scan in `list_credentials` and the second assertion fails
    while `test_listing_falling_back_includes_decider` (explicit pointer) still passes.
    """
    pid = _proj(client, auth, "InheritsMute")
    sibling = _proj(client, auth, "AlsoInherits")
    _judge_on(client, auth, pid)
    key = _key(client, auth, project_id=pid)
    cred = _decider_row(db)
    platform_svc.set_scope_defaults(db, "", decider_credential_id=cred.id)
    _build(monkeypatch, MuteDecider())

    assert _rows(db)[cred.id]["falling_back"] == [], (
        "control: nothing has asked this decider yet, so nobody is falling back")

    _mcp(client, key, "add_memory", {"text": "always pin the pgvector image to pg16 in CI"})

    row = _rows(db)[cred.id]
    assert row["is_decider"] is True
    assert pid in row["falling_back"], f"the project that asked is not named: {row['falling_back']}"
    assert sibling in row["falling_back"], (
        f"a project that INHERITED the default is not named: {row['falling_back']} — this is "
        "the case the incident hit, where no project had a pointer of its own")


def test_an_unreachable_default_names_its_inheritors_without_a_runtime_failure(client, auth, db):
    """Criterion 3's first half: an already-`unreachable` default cannot look like a clean
    green judge either. No call is made here — the state alone is enough, and S2's asymmetry
    (the default is still RETURNED) is exactly why resolution cannot be the signal."""
    inheritor = _proj(client, auth, "Inherits")
    cred = _decider_row(db, "cred_broken_default", state="unreachable", model="laya-1")
    platform_svc.set_scope_defaults(db, "", decider_credential_id=cred.id)

    row = _rows(db)[cred.id]
    assert inheritor in row["falling_back"]
    assert platform_svc.resolve_decider(db, inheritor).credential_id == cred.id, (
        "the asymmetry must survive: the default is still returned, so `falling_back` is the "
        "only thing standing between this and a clean-looking deployment")
    assert platform_svc.decider_health(db, inheritor) == {
        "configured": True, "usable": False, "credential_id": cred.id, "label": cred.id,
        "state": "unreachable", "last_error": "", "source": "deployment", "fell_back_from": "",
    }


def test_a_project_that_turned_the_decider_off_is_not_named(client, auth, db):
    """`memory.decide: none` is a choice, not a fallback. Naming it would make the field
    noise an operator learns to scroll past — the fate of every warning that cries wolf."""
    off = _proj(client, auth, "DeciderOff")
    inheritor = _proj(client, auth, "DeciderInherits")
    cred = _decider_row(db, "cred_off_default", state="unreachable")
    platform_svc.set_scope_defaults(db, "", decider_credential_id=cred.id)
    platform_svc.set_project_roles(
        db, off, {"memory.decide": {"credential_id": platform_svc.DECIDER_ROLE_NONE}})

    fallen = _rows(db)[cred.id]["falling_back"]
    assert inheritor in fallen
    assert off not in fallen, "a project that asked for no decider is not being denied one"


def test_a_project_with_its_own_working_decider_is_not_named_on_the_broken_default(
        client, auth, db, monkeypatch):
    """The counterpart to the inheritor scan: a project that IS getting adjudication must not
    be listed as falling back, or the field says nothing."""
    own = _proj(client, auth, "HasItsOwn")
    inheritor = _proj(client, auth, "Inherits")
    broken = _decider_row(db, "cred_broken_scope", state="unreachable")
    good = _decider_row(db, "cred_own_good", model="laya-1")
    platform_svc.set_scope_defaults(db, "", decider_credential_id=broken.id)
    platform_svc.set_project_decider(db, own, decider_credential_id=good.id)
    _build(monkeypatch, AnsweringDecider())

    rows = _rows(db)
    assert inheritor in rows[broken.id]["falling_back"]
    assert own not in rows[broken.id]["falling_back"]
    assert rows[good.id]["falling_back"] == []
    assert platform_svc.decider_health(db, own)["usable"] is True
    assert _status(client, auth, own) == {
        "judge_on": False, "judge": "off", "decider_configured": False, "credential_label": "",
        "falling_back": False, "reason": mem_svc.JUDGE_CAUSES["off"],
    }


def test_a_project_pointer_that_fails_at_runtime_is_named_and_fallen_past(
        client, auth, db, monkeypatch):
    """A project's OWN decider, asked and mute. Recorded on the row, named on the row, and
    then fallen past — which is the GRPH-525 behaviour for a project pointer, and the reason
    the page still says falling back: the next rung down is the chat judge, and there is
    none configured here either."""
    pid = _proj(client, auth, "OwnMutePointer")
    _judge_on(client, auth, pid)
    key = _key(client, auth, project_id=pid)
    cred = _decider_row(db, "cred_own_mute", model="laya-1")
    platform_svc.set_project_decider(db, pid, decider_credential_id=cred.id)
    _build(monkeypatch, MuteDecider())

    _mcp(client, key, "add_memory", {"text": "always pin the pgvector image to pg16 in CI"})

    db.refresh(cred)
    assert cred.state == "unreachable"
    assert pid in _rows(db)[cred.id]["falling_back"]
    assert platform_svc.resolve_decider(db, pid).credential_id != cred.id, (
        "a project pointer that cannot answer is fallen past, not retried on every write")
    status = _status(client, auth, pid)
    assert status["falling_back"] is True and status["judge"] == "similarity"


def test_a_project_pointer_blip_recovers_via_the_retry_sweep(
        client, auth, db, monkeypatch):
    """THE BOUNCE (project half). After one failed `decide()`, `usable()` is False
    so `resolve_decider` never calls this pointer again and `note_decide_success`
    cannot fire. The sweep must reclaim a runtime-fail `unreachable` row;
    `ping_decide` restoring it is what lets the next write grade on the PROJECT
    decider.

    Sabotage the CALL: drop `_runtime_fail_retryable` from `due` (or from
    `claim`'s WHERE). This fails, and the deployment-default blip test stays
    green — S2 still calls an unreachable default.
    """
    from app.services import credential_retry as cr

    pid = _proj(client, auth, "OwnPointerBlip")
    _judge_on(client, auth, pid)
    key = _key(client, auth, project_id=pid)
    cred = _decider_row(db, "cred_own_blip", model="laya-1")
    platform_svc.set_project_decider(db, pid, decider_credential_id=cred.id)
    boom = _build(monkeypatch, MuteDecider())

    first = _mcp(client, key, "add_memory",
                 {"text": "always pin the pgvector image to pg16 in CI"})
    assert boom.calls == 1
    db.refresh(cred)
    assert cred.state == "unreachable"
    assert first["scoring_source"] != "decider"
    assert platform_svc.resolve_decider(db, pid).credential_id != cred.id
    assert _status(client, auth, pid)["falling_back"] is True

    monkeypatch.setattr(cr.probe, "known_models",
                        lambda *a, **k: frozenset({"laya-1"}))
    monkeypatch.setattr(cr.systemone, "ping_decide", lambda *a, **k: None)
    assert cr.run_once(db) == 1, (
        "the sweep did not pick up the runtime-fail row — due()/claim() still "
        "filter state==pending only")
    db.refresh(cred)
    assert cred.state == "valid"
    assert not (cred.last_error or "").startswith("decide() failed at runtime")

    good = _build(monkeypatch, AnsweringDecider())
    _mcp(client, key, "add_memory",
         {"text": "HNSW not ivfflat — ivfflat built empty silently loses recall"})
    assert good.calls == 1, (
        "the project pointer was not asked after the sweep restored it")
    db.refresh(cred)
    assert cred.state == "valid"
    status = _status(client, auth, pid)
    assert status["falling_back"] is False and status["judge"] == "decider"


# ---- the page ---------------------------------------------------------------------------


def test_the_review_page_is_told_the_project_is_falling_back(client, auth, db, monkeypatch):
    """Criterion 1's first half. Before this, the page's only project-level fact was the
    `memory_llm_judge` toggle — what was ASKED for, not what happened."""
    pid = _proj(client, auth, "SaysFallingBack")
    _judge_on(client, auth, pid)
    key = _key(client, auth, project_id=pid)
    cred = _decider_row(db, label="Laya (empty model)")
    platform_svc.set_scope_defaults(db, "", decider_credential_id=cred.id)
    _build(monkeypatch, MuteDecider())

    before = _status(client, auth, pid)
    assert before == {
        "judge_on": True, "judge": "decider", "decider_configured": True,
        "credential_label": "Laya (empty model)", "falling_back": False, "reason": "",
    }, "control: a decider nobody has asked yet is not a fallback"

    _mcp(client, key, "add_memory", {"text": "always pin the pgvector image to pg16 in CI"})

    after = _status(client, auth, pid)
    assert after["falling_back"] is True
    assert after["judge"] == "similarity"
    assert "Laya (empty model)" in after["reason"], "the copy must name the credential"
    assert "could not be reached" in after["reason"]
    assert "decide() failed at runtime" in after["reason"]


def test_a_decider_that_answers_reports_no_fallback(client, auth, db, monkeypatch):
    """The counterpart, and the reason `judge` is a positive answer rather than an absence.
    If `falling_back` were true whenever a decider was configured, the banner would be noise
    and this file would still be green with the recording deleted."""
    pid = _proj(client, auth, "WorkingDecider")
    _judge_on(client, auth, pid)
    key = _key(client, auth, project_id=pid)
    cred = _decider_row(db, "cred_working", model="laya-1", label="Laya")
    platform_svc.set_scope_defaults(db, "", decider_credential_id=cred.id)
    good = _build(monkeypatch, AnsweringDecider())

    s = _mcp(client, key, "add_memory", {"text": "always pin the pgvector image to pg16 in CI"})
    scored = client.get(f"/api/memory/candidates/scored?project_id={pid}", headers=auth).json()

    assert good.calls >= 1
    row = next(r for r in scored if r["shard"]["id"] == s["id"])
    assert row["judged"] is True and row["judge_source"] == "decider"
    assert _status(client, auth, pid)["falling_back"] is False
    db.refresh(cred)
    assert cred.state == "valid", "a decider that answered must not be marked unreachable"
    assert _rows(db)[cred.id]["falling_back"] == []


# ---- what must not change ---------------------------------------------------------------


def test_vetoes_still_win_when_the_decider_cannot_answer(client, auth, db, monkeypatch):
    """Criterion 3's second half, and the invariant D8 states: a structural veto is not the
    judge's to overturn, so a mute judge must not loosen one — or `disable auth in dev`
    becomes a candidate again the moment the decider goes down."""
    pid = _proj(client, auth, "VetoWins")
    _judge_on(client, auth, pid)
    key = _key(client, auth, project_id=pid)
    bad = _mcp(client, key, "add_memory", {"text": "disable auth in dev to move faster"})
    client.post(f"/api/memory/shards/{bad['id']}/reject", headers=auth)

    cred = _decider_row(db, "cred_veto")
    platform_svc.set_scope_defaults(db, "", decider_credential_id=cred.id)
    boom = _build(monkeypatch, MuteDecider())

    again = _mcp(client, key, "add_memory", {"text": "disable auth in dev to move faster"})

    assert again["status"] == "rejected"
    assert again["scoring_source"] == "similarity"
    assert boom.calls == 0, "a vetoed shard must not spend a judge call"


def test_judge_status_needs_a_readable_project(client, auth, db):
    """Scoped like the queue it describes: a project the caller cannot read is not a free
    read of somebody else's decider configuration."""
    cred = _decider_row(db, "cred_scope_check", state="unreachable")
    platform_svc.set_scope_defaults(db, "", decider_credential_id=cred.id)

    r = client.get("/api/memory/judge-status?project_id=does-not-exist", headers=auth)
    assert r.status_code in (403, 404), r.text
