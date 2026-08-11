"""AL-227: memory auto-triage. The AL-151 scorer ACTS on agent candidates on write —
auto-rejecting near-dups / resembles-rejected (on by default) and auto-publishing
strongly-corroborated lessons (off by default) — behind per-project toggles, with
every auto-action audited and undoable. Fresh projects give empty, deterministic
pools; the stub embedder yields identical vectors for identical text."""
import pytest

from app.services import memory as mem_svc


def _mcp(client, key, tool, args):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args}},
        headers={"X-API-Key": key},
    ).json()["result"]["structuredContent"]


def _key(client, auth, **body):
    return client.post("/api/api-keys", json={"name": "mem", **body}, headers=auth).json()["plaintext"]


def _proj(client, auth, name):
    return client.post("/api/projects", json={"name": name}, headers=auth).json()["id"]


def _login(client, email):
    r = client.post("/api/auth/login", json={"email": email, "password": "graphban"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# ---- defaults ----

def test_new_project_defaults(client, auth):
    """Reject on, review mode, LLM judge off — the safe posture (AL-227, AL-280)."""
    p = client.post("/api/projects", json={"name": "TriageDefaults"}, headers=auth).json()
    assert p["memory_auto_reject"] is True
    assert p["memory_write_mode"] == "review"
    assert p["memory_llm_judge"] is False


# ---- auto-reject (default on) ----

def test_auto_reject_drops_duplicate_of_published_on_write(client, auth):
    pid = _proj(client, auth, "AutoRejDup")
    key = _key(client, auth, project_id=pid)
    pub = _mcp(client, key, "add_memory", {"text": "prefer idempotency keys on writes"})
    client.post(f"/api/memory/shards/{pub['id']}/publish", headers=auth)  # now trusted
    # An identical candidate is a near-duplicate → auto-rejected in the same call.
    dup = _mcp(client, key, "add_memory", {"text": "prefer idempotency keys on writes"})
    assert dup["status"] == "rejected"
    assert dup["scoring_source"] == "similarity"
    assert dup["auto_confidence"] is not None and dup["auto_confidence"] >= 0.95
    # It never reaches the human review queue.
    queue = client.get(f"/api/memory/candidates?project_id={pid}", headers=auth).json()
    assert all(r["id"] != dup["id"] for r in queue)


def test_auto_reject_drops_resembles_rejected_on_write(client, auth):
    pid = _proj(client, auth, "AutoRejBad")
    key = _key(client, auth, project_id=pid)
    bad = _mcp(client, key, "add_memory", {"text": "disable auth in dev to move faster"})
    client.post(f"/api/memory/shards/{bad['id']}/reject", headers=auth)
    again = _mcp(client, key, "add_memory", {"text": "disable auth in dev to move faster"})
    assert again["status"] == "rejected"
    assert again["scoring_source"] == "similarity"


def test_auto_reject_off_keeps_candidate(client, auth):
    pid = _proj(client, auth, "AutoRejOff")
    client.patch(f"/api/projects/{pid}", json={"memory_auto_reject": False}, headers=auth)
    key = _key(client, auth, project_id=pid)
    pub = _mcp(client, key, "add_memory", {"text": "always paginate list endpoints"})
    client.post(f"/api/memory/shards/{pub['id']}/publish", headers=auth)
    dup = _mcp(client, key, "add_memory", {"text": "always paginate list endpoints"})
    # Advisory scorer still says reject, but with the toggle off nothing acts.
    assert dup["status"] == "candidate"
    assert dup["scoring_source"] == ""


def test_novel_candidate_is_left_for_review(client, auth):
    """A novel lesson has no strong signal → stays a candidate even with reject on."""
    pid = _proj(client, auth, "AutoNovel")
    key = _key(client, auth, project_id=pid)
    s = _mcp(client, key, "add_memory", {"text": "the flux capacitor prefers 1.21 gigawatts"})
    assert s["status"] == "candidate"


# ---- auto-accept (default off) ----

def test_auto_accept_publishes_high_confidence_recurrence(client, auth):
    pid = _proj(client, auth, "AutoAcc")
    client.patch(f"/api/projects/{pid}", json={"memory_write_mode": "auto"}, headers=auth)
    key = _key(client, auth, project_id=pid)
    text = "always set a timeout on outbound http"
    statuses = [_mcp(client, key, "add_memory", {"text": text})["status"] for _ in range(3)]
    # Recurrence lifts confidence: by the 3rd identical candidate it crosses the
    # auto-publish bar (>= 0.9) and publishes without a human.
    assert statuses[-1] == "published"


def test_auto_accept_off_by_default_keeps_recurring_candidate(client, auth):
    pid = _proj(client, auth, "AutoAccOff")
    key = _key(client, auth, project_id=pid)
    text = "always set a timeout on outbound http"
    statuses = [_mcp(client, key, "add_memory", {"text": text})["status"] for _ in range(3)]
    # No auto-accept → the recurring lesson waits in the queue for a human.
    assert statuses == ["candidate", "candidate", "candidate"]


# ---- audit + undo ----

def test_auto_action_is_audited(client, auth):
    pid = _proj(client, auth, "AutoAudit")
    key = _key(client, auth, project_id=pid)
    pub = _mcp(client, key, "add_memory", {"text": "cache invalidation needs a version tag"})
    client.post(f"/api/memory/shards/{pub['id']}/publish", headers=auth)
    _mcp(client, key, "add_memory", {"text": "cache invalidation needs a version tag"})
    actions = [e["action"] for e in client.get(f"/api/events?project_id={pid}", headers=auth).json()["results"]]
    assert "auto_reject_shard" in actions


def test_auto_actions_lane_and_undo(client, auth):
    pid = _proj(client, auth, "AutoUndo")
    key = _key(client, auth, project_id=pid)
    pub = _mcp(client, key, "add_memory", {"text": "retry only idempotent requests"})
    client.post(f"/api/memory/shards/{pub['id']}/publish", headers=auth)
    dup = _mcp(client, key, "add_memory", {"text": "retry only idempotent requests"})
    assert dup["status"] == "rejected"

    # The auto-actions lane surfaces it.
    lane = client.get(f"/api/memory/auto-actions?project_id={pid}", headers=auth).json()
    assert any(s["id"] == dup["id"] for s in lane)

    # Undo returns it to the candidate queue and clears the auto markers.
    r = client.post(f"/api/memory/shards/{dup['id']}/undo-auto", headers=auth)
    assert r.status_code == 200
    restored = r.json()
    assert restored["status"] == "candidate"
    assert restored["scoring_source"] == ""
    assert restored["auto_confidence"] is None
    # It's back in review and gone from the lane.
    queue = client.get(f"/api/memory/candidates?project_id={pid}", headers=auth).json()
    assert any(s["id"] == dup["id"] for s in queue)
    lane2 = client.get(f"/api/memory/auto-actions?project_id={pid}", headers=auth).json()
    assert all(s["id"] != dup["id"] for s in lane2)
    # The undo is itself audited.
    actions = [e["action"] for e in client.get(f"/api/events?project_id={pid}", headers=auth).json()["results"]]
    assert "undo_auto_shard" in actions


def test_read_only_member_cannot_undo(client):
    alex = _login(client, "alex@ascme-labs.com")
    key = _key(client, alex, project_id="core")
    pub = _mcp(client, key, "add_memory", {"text": "canary undo authz zzz"})
    client.post(f"/api/memory/shards/{pub['id']}/publish", headers=alex)
    dup = _mcp(client, key, "add_memory", {"text": "canary undo authz zzz"})
    ops = _login(client, "ops@ascme-labs.com")  # read-only on core
    r = client.post(f"/api/memory/shards/{dup['id']}/undo-auto", headers=ops)
    assert r.status_code == 403


# ---- LLM judge (AL-227) ----

from app.services import memory as mem  # noqa: E402


def test_parse_judge_variants():
    assert mem._parse_judge('{"keep": true, "quality": 0.9, "reason": "solid"}') == {
        "keep": True, "quality": 0.9, "reason": "solid"}
    # Embedded in prose + clamps out-of-range quality.
    v = mem._parse_judge('Sure!\n{"keep": false, "quality": 1.7, "reason": "vague"}\ndone')
    assert v["keep"] is False and v["quality"] == 1.0
    # Malformed / missing key / empty → None (caller falls back to similarity).
    assert mem._parse_judge("not json") is None
    assert mem._parse_judge('{"quality": 0.5}') is None
    assert mem._parse_judge("") is None


class _FakeChat:
    def __init__(self, reply: str):
        self._reply = reply

    def chat(self, *, system: str, context: str, question: str,
             temperature: float | None = None) -> str:
        return self._reply


def _patch_judge(monkeypatch, reply: str):
    """Point the project's chat resolution at a fake non-stub model returning `reply`."""
    from app.services import platform as platform_svc
    monkeypatch.setattr(platform_svc, "resolve_chat", lambda db, pid: ("anthropic", _FakeChat(reply)))


def test_llm_judge_auto_rejects_low_quality(client, auth, monkeypatch):
    pid = _proj(client, auth, "JudgeReject")
    client.patch(f"/api/projects/{pid}", json={"memory_llm_judge": True}, headers=auth)
    key = _key(client, auth, project_id=pid)
    _patch_judge(monkeypatch, '{"keep": false, "quality": 0.1, "reason": "too vague to act on"}')
    # A novel note similarity would merely queue for review — the judge rejects it.
    s = _mcp(client, key, "add_memory", {"text": "the build felt slow today"})
    assert s["status"] == "rejected"
    assert s["scoring_source"] == "llm"


def test_llm_judge_auto_accepts_high_quality(client, auth, monkeypatch):
    pid = _proj(client, auth, "JudgeAccept")
    client.patch(f"/api/projects/{pid}", json={"memory_llm_judge": True, "memory_write_mode": "auto"}, headers=auth)
    key = _key(client, auth, project_id=pid)
    _patch_judge(monkeypatch, '{"keep": true, "quality": 0.95, "reason": "durable, specific convention"}')
    # A novel note similarity would never auto-publish — the judge greenlights it.
    s = _mcp(client, key, "add_memory", {"text": "always pin the pgvector image to pg16 in CI"})
    assert s["status"] == "published"
    assert s["scoring_source"] == "llm"
    assert abs(s["auto_confidence"] - 0.95) < 1e-6


def test_llm_judge_keep_but_mediocre_stays_candidate(client, auth, monkeypatch):
    pid = _proj(client, auth, "JudgeMeh")
    client.patch(f"/api/projects/{pid}", json={"memory_llm_judge": True, "memory_write_mode": "auto"}, headers=auth)
    key = _key(client, auth, project_id=pid)
    _patch_judge(monkeypatch, '{"keep": true, "quality": 0.5, "reason": "ok but not strong"}')
    s = _mcp(client, key, "add_memory", {"text": "consider caching the config lookup"})
    assert s["status"] == "candidate"  # keep, but below the publish-worthy bar


def test_llm_judge_does_not_override_structural_veto(client, auth, monkeypatch):
    pid = _proj(client, auth, "JudgeDup")
    client.patch(f"/api/projects/{pid}", json={"memory_llm_judge": True}, headers=auth)
    key = _key(client, auth, project_id=pid)
    pub = _mcp(client, key, "add_memory", {"text": "prefer idempotency keys on writes"})
    client.post(f"/api/memory/shards/{pub['id']}/publish", headers=auth)
    # Even a glowing judge can't rescue a near-duplicate — similarity's veto wins.
    _patch_judge(monkeypatch, '{"keep": true, "quality": 0.99, "reason": "great"}')
    dup = _mcp(client, key, "add_memory", {"text": "prefer idempotency keys on writes"})
    assert dup["status"] == "rejected"
    assert dup["scoring_source"] == "similarity"  # structural, not the judge


def test_llm_judge_falls_back_to_similarity_when_stub(client, auth):
    """Toggle on, but only the offline stub provider → judge is a no-op; similarity rules."""
    pid = _proj(client, auth, "JudgeStub")
    client.patch(f"/api/projects/{pid}", json={"memory_llm_judge": True, "memory_write_mode": "auto"}, headers=auth)
    key = _key(client, auth, project_id=pid)
    s = _mcp(client, key, "add_memory", {"text": "a novel note with no configured model"})
    assert s["status"] == "candidate"
    assert s["scoring_source"] == ""


# ---- human writes are never triaged ----

def test_human_shard_not_triaged(client, auth):
    """A human write is trusted immediately — auto-triage only judges agent candidates."""
    pid = _proj(client, auth, "HumanNoTriage")
    key = _key(client, auth, project_id=pid)
    bad = _mcp(client, key, "add_memory", {"text": "human override note qwerty"})
    client.post(f"/api/memory/shards/{bad['id']}/reject", headers=auth)
    # Same text, but written by a human via REST → published, not auto-rejected.
    created = client.post("/api/memory/shards",
                          json={"text": "human override note qwerty", "scope": "global", "project_id": pid},
                          headers=auth).json()
    assert created["status"] == "published"
    assert created["scoring_source"] == ""


# ---- the judge has to agree with itself (GRPH-348) -------------------------------------------
# `temperature=0` was assumed to make one sample enough. Measured against ollama it does not:
# one stored shard judged five times returned keep=False four times and keep=True once. A
# single sample from a judge that disagrees with itself is not an adjudication, and
# `agent_publish` promises "the JUDGE decides" — worth something only if it decides twice.
class _SeqChat:
    """Returns a different reply per call, and counts how many it was asked for."""

    def __init__(self, *replies: str):
        self._replies, self.calls = list(replies), 0

    def chat(self, *, system, context, question, temperature=None) -> str:
        self.calls += 1
        self.seen = context
        return self._replies[min(self.calls - 1, len(self._replies) - 1)]


def _patch_seq(monkeypatch, chat):
    from app.services import platform as platform_svc
    monkeypatch.setattr(platform_svc, "resolve_chat", lambda db, pid: ("anthropic", chat))
    return chat


KEEP = '{"keep": true, "quality": 0.9, "reason": "durable and specific"}'
DROP = '{"keep": false, "quality": 0.1, "reason": "too vague to act on"}'


def test_a_split_verdict_is_no_verdict(client, auth, monkeypatch):
    """THE acceptance criterion. Disagreement means the judge has no answer — reporting a
    coin flip as a verdict is what let one sample decide a shard's fate."""
    pid = _proj(client, auth, "JudgeSplit")
    client.patch(f"/api/projects/{pid}",
                 json={"memory_llm_judge": True, "memory_write_mode": "auto"}, headers=auth)
    key = _key(client, auth, project_id=pid)
    _patch_seq(monkeypatch, _SeqChat(KEEP, DROP, KEEP))

    s = _mcp(client, key, "add_memory", {"text": "always pin the pgvector image in CI"})
    assert s["status"] == "candidate", "no adjudication, so it falls back to a human"
    assert s["scoring_source"] != "llm"


def test_the_judge_is_asked_more_than_once(client, auth, monkeypatch):
    """Pins the sampling itself. With one sample the test above passes by accident — the
    first reply would simply be taken as the answer."""
    pid = _proj(client, auth, "JudgeSamples")
    client.patch(f"/api/projects/{pid}", json={"memory_llm_judge": True}, headers=auth)
    key = _key(client, auth, project_id=pid)
    chat = _patch_seq(monkeypatch, _SeqChat(KEEP))

    _mcp(client, key, "add_memory", {"text": "always pin the pgvector image in CI"})
    assert chat.calls == mem_svc.JUDGE_SAMPLES


def test_disagreement_stops_early_rather_than_finishing_the_samples(client, auth, monkeypatch):
    """The extra cost is paid only where the answer was stable anyway."""
    pid = _proj(client, auth, "JudgeEarly")
    client.patch(f"/api/projects/{pid}", json={"memory_llm_judge": True}, headers=auth)
    key = _key(client, auth, project_id=pid)
    chat = _patch_seq(monkeypatch, _SeqChat(KEEP, DROP, KEEP))

    _mcp(client, key, "add_memory", {"text": "always pin the pgvector image in CI"})
    assert chat.calls == 2, "stopped at the first disagreement"


def test_an_agreeing_judge_still_decides(client, auth, monkeypatch):
    """The other half — a filter that refused everything would pass every test above."""
    pid = _proj(client, auth, "JudgeAgree")
    client.patch(f"/api/projects/{pid}",
                 json={"memory_llm_judge": True, "memory_write_mode": "auto"}, headers=auth)
    key = _key(client, auth, project_id=pid)
    _patch_seq(monkeypatch, _SeqChat(KEEP, KEEP, KEEP))

    s = _mcp(client, key, "add_memory", {"text": "always pin the pgvector image in CI"})
    assert s["status"] == "published" and s["scoring_source"] == "llm"


# ---- "I saw nothing" is not a quality score --------------------------------------------------
@pytest.mark.parametrize("reason", [
    "No memory note was provided",
    "No memory content was provided for evaluation",
    "Nothing was provided to review",
    "Empty input, cannot rate",
])
def test_a_judge_reporting_an_empty_prompt_has_not_judged(client, auth, monkeypatch, reason):
    """Three real shards were rejected at quality 0.00 on replies like these. The prompt was
    NOT empty — `'Exit code 1\\n(eval):cd:1: …'`. "I could not read this" and "this is
    worthless" are opposite claims, and only one is a reason to reject something."""
    pid = _proj(client, auth, "JudgeBlind")
    client.patch(f"/api/projects/{pid}", json={"memory_llm_judge": True}, headers=auth)
    key = _key(client, auth, project_id=pid)
    _patch_seq(monkeypatch, _SeqChat('{"keep": false, "quality": 0.0, "reason": "%s"}' % reason))

    s = _mcp(client, key, "add_memory", {"text": "the deploy step needs an absolute path"})
    assert s["status"] == "candidate", "must not be rejected on a non-verdict"


def test_an_ordinary_low_score_still_rejects(client, auth, monkeypatch):
    """The no-input guard must not swallow a real negative — "no actionable detail
    provided" is a judgement about the CONTENT and has to keep working."""
    pid = _proj(client, auth, "JudgeLow")
    client.patch(f"/api/projects/{pid}", json={"memory_llm_judge": True}, headers=auth)
    key = _key(client, auth, project_id=pid)
    _patch_seq(monkeypatch, _SeqChat(
        '{"keep": false, "quality": 0.2, "reason": "vague error message, no actionable detail"}'))

    s = _mcp(client, key, "add_memory", {"text": "the build felt slow today"})
    assert s["status"] == "rejected"


def test_the_judge_is_shown_text_the_shape_does_not_break(client, auth, monkeypatch):
    """A mitigation, not the fix: the stored `'Exit code 1\\n(eval):…'` made the model reply
    as though the prompt were empty, while the same characters with a space returned a real
    verdict. Storage keeps its newlines; only the judge's copy is normalised."""
    pid = _proj(client, auth, "JudgeWs")
    client.patch(f"/api/projects/{pid}", json={"memory_llm_judge": True}, headers=auth)
    key = _key(client, auth, project_id=pid)
    chat = _patch_seq(monkeypatch, _SeqChat(KEEP))

    s = _mcp(client, key, "add_memory", {"text": "Exit code 1\nthe deploy needs an absolute path"})
    assert "\n" not in chat.seen, "the judge sees it flattened"
    assert "\n" in s["text"], "the stored row keeps its shape"


# ---- the publish bar is its own number -------------------------------------------------------
def test_the_quality_bar_is_not_a_similarity_threshold(client, auth, monkeypatch):
    """It borrowed `_SIM_STRONG` (0.88), a COSINE SIMILARITY threshold, which rejected two
    shards the judge had itself called "specific" and "actionable" at 0.80 and 0.70. A
    model's self-rating and a vector distance are not the same scale."""
    assert mem_svc._JUDGE_PUBLISH_MIN != mem_svc._SIM_STRONG

    from app.db import SessionLocal

    _patch_seq(monkeypatch, _SeqChat(
        '{"keep": true, "quality": 0.8, "reason": "specific actionable instruction"}'))
    db = SessionLocal()
    try:
        shard = mem_svc.add_memory(db, text_body="read a file before writing to it",
                                   project_id="core", status="candidate", auto_triage=False)
        published, verdict = mem_svc.agent_publish(db, shard, origin="agent:test")

        assert verdict["keep"] is True and verdict["quality"] == 0.8
        assert published.status == "published", \
            "0.80 clears the judge's own bar; it did not clear the borrowed 0.88"
    finally:
        db.close()


# ---- say WHICH failure (GRPH-351) ------------------------------------------------------------
# `AdjudicationUnavailable` read "no independent chat model is configured" whatever went
# wrong. Running the first real adjudication, it said exactly that about a model that had
# just judged five other shards in the same run — sending a reader to look for a provider
# that was present and working. GRPH-348 fixed the judge's verdicts and left its own failure
# reporting conflating four causes under the most reassuring one.
def _adjudicate(db, monkeypatch, *replies, text="read a file before writing to it"):
    from app.db import SessionLocal

    if replies:
        _patch_seq(monkeypatch, _SeqChat(*replies))
    shard = mem_svc.add_memory(db, text_body=text, project_id="core",
                               status="candidate", auto_triage=False)
    try:
        mem_svc.agent_publish(db, shard, origin="agent:test")
        return None
    except mem_svc.AdjudicationUnavailable as e:
        return str(e)


@pytest.fixture()
def sdb():
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def test_a_split_verdict_does_not_report_a_missing_model(client, sdb, monkeypatch):
    """THE case that surfaced it. The model is present and answering; it simply cannot
    decide about this candidate, which is a fact about the SHARD."""
    msg = _adjudicate(sdb, monkeypatch, KEEP, DROP, KEEP)

    assert msg is not None
    assert "did not agree with itself" in msg
    assert "no independent chat model" not in msg, "the model is configured and working"


def test_a_missing_model_still_says_so(client, sdb, monkeypatch):
    """The original cause has to keep reporting accurately — that one IS something an
    operator can go and fix."""
    from app.services import platform as platform_svc

    monkeypatch.setattr(platform_svc, "resolve_chat", lambda db, pid: ("stub", None))
    msg = _adjudicate(sdb, monkeypatch)

    assert msg is not None and "no independent chat model is configured" in msg


def test_a_judge_that_saw_nothing_is_distinguished_from_one_that_is_absent(client, sdb,
                                                                          monkeypatch):
    msg = _adjudicate(sdb, monkeypatch,
                      '{"keep": false, "quality": 0.0, "reason": "No memory note was provided"}')

    assert msg is not None and "received no content to rate" in msg


def test_an_unparseable_reply_is_not_reported_as_an_empty_prompt(client, sdb, monkeypatch):
    """A model answering in the wrong FORM is a fact about the model; a model saying it got
    nothing is a fact about the candidate. Collapsing them is the conflation being fixed."""
    msg = _adjudicate(sdb, monkeypatch, "I'm afraid I can't help with that.")

    assert msg is not None
    assert "did not answer in the required form" in msg
    assert "received no content" not in msg


@pytest.mark.parametrize("cause", ["no_provider", "split", "no_input", "unparseable", "error"])
def test_every_cause_has_wording_of_its_own(cause):
    """A cause added later without a sentence would fall back to printing its own key."""
    assert cause in mem_svc.JUDGE_CAUSES and len(mem_svc.JUDGE_CAUSES[cause]) > 20


def test_the_causes_are_distinguishable_to_a_reader(client, sdb, monkeypatch):
    """Five different wordings, not five labels on one sentence."""
    assert len(set(mem_svc.JUDGE_CAUSES.values())) == len(mem_svc.JUDGE_CAUSES)
