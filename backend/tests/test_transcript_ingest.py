"""Transcript ingest and extraction-time scrubbing (GRPH-304 + GRPH-305 / PRD-16).

Graphban learns from what people write DOWN about work. The richer signal — tool calls,
exit codes, corrections — was never read.

**These ship in the same change, and the ordering is the reason.** PRD-16: *"scrubbing at
publish time is too late, because a candidate is already persisted and searchable."* By the
time a human reviews a candidate the leak has already happened, so the redactor runs on the
write path in `add_memory`, where every producer inherits it.

Two properties the promotion ladder above this depends on:

- **A re-run must not duplicate evidence.** The ladder counts corroborating shards, so
  duplicates do not merely waste work — they manufacture corroboration, promoting a lesson
  that only ever happened once.
- **A bad record must not end the run.** A truncated final line is the normal state of a
  session in progress, not a corruption.
"""
import json

import pytest

from app.models import IngestWatermark, MemoryShard
from app.services import scrub as scrub_svc
from app.services.ingest.claude_code import ClaudeCodeAdapter
from app.services.ingest.runner import ingest


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _line(text, **over):
    """A record in the shape a transcript actually uses.

    `user`, not `assistant`, because since GRPH-345 only a PERSON's prose is evidence —
    assistant text is narration of work in progress, and recording it is what turned one
    transcript into 1,088 shards.
    """
    row = {"sessionId": "sess-1", "type": "user", "cwd": "/repo",
           "timestamp": "2026-08-09T00:00:00Z",
           "message": {"content": [{"type": "text", "text": text}]}}
    row.update(over)
    return json.dumps(row) + "\n"


@pytest.fixture()
def transcripts(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    return d


def _write(d, name, lines):
    p = d / name
    p.write_text("".join(lines))
    return str(p)


# Long enough to clear MIN_PROSE_CHARS. The length is the point rather than an accident of
# phrasing: a message this size is someone stating a constraint, which is what the bar is
# there to select for.
LESSON = (
    "The migration guard caught a missing revision because the range in AGENTS.md was never "
    "bumped after adding one. Please make the guard part of the standard loop rather than "
    "something we remember to run, because we have now hit this three separate times and "
    "each time it was found by a human reading a diff rather than by anything automatic. "
    "The fix should fail the build, not warn, since a warning in a long log is the same as "
    "silence for our purposes here.")


# ---- scrubbing runs before anything is stored -------------------------------------------
@pytest.mark.parametrize("raw,gone", [
    ("token is sk-abcd1234efgh5678 here", "sk-abcd1234efgh5678"),
    ("Authorization: Bearer eyJhbGciOi1234567", "eyJhbGciOi1234567"),
    ("built in /Users/alexcain/working/git/x", "alexcain"),
    ("mail me at alex.cain@example.com ok", "alex.cain@example.com"),
    ("db is postgres://admin:hunter2@db.prod:5432/app", "hunter2"),
    ("api_key = 'abcdefgh12345678'", "abcdefgh12345678"),
    ("host was 192.168.1.44 at the time", "192.168.1.44"),
])
def test_secrets_and_pii_do_not_survive(raw, gone):
    clean, changed = scrub_svc.scrub(raw)
    assert gone not in clean and changed is True


def test_redaction_leaves_a_placeholder_rather_than_a_hole(db):
    """A shard whose secret is cut out reads as if the author never mentioned one. One that
    says [redacted:token] still carries the fact that a token was involved, which is often
    the lesson — and redaction that destroys meaning gets switched off."""
    clean, _ = scrub_svc.scrub("we leaked sk-abcd1234efgh5678 in the log")
    assert "[redacted:" in clean and "we leaked" in clean and "in the log" in clean


def test_a_named_secret_keeps_its_name(db):
    """`password = hunter2` — the NAME is usually the point of the lesson; only the value
    is dangerous."""
    clean, _ = scrub_svc.scrub("password = hunter2xyz")
    assert "password" in clean and "hunter2xyz" not in clean


def test_ordinary_code_paths_are_not_mangled(db):
    """Deliberately conservative. A general hostname pattern matches `services/memory.py`
    and `app.get` — most of the corpus this exists to learn from. A scrubber that mangles
    ordinary text is one people route around, so the gap is stated rather than papered."""
    text = "backend/app/services/memory.py calls add_memory and returns MemoryShard"
    clean, changed = scrub_svc.scrub(text)
    assert clean == text and changed is False


def test_loopback_addresses_are_left_alone(db):
    """127.0.0.1 identifies nothing. Redacting it trains readers to ignore the marker."""
    clean, _ = scrub_svc.scrub("bound to 127.0.0.1 and 0.0.0.0")
    assert "127.0.0.1" in clean and "0.0.0.0" in clean


def test_the_stored_row_is_clean_not_just_the_response(db, transcripts):
    """THE acceptance criterion, asserted where it matters. Checking the API response would
    pass against a service that redacts on the way out and stores the secret."""
    _write(transcripts, "s.jsonl", [_line(
        f"{LESSON} The key sk-abcd1234efgh5678 was in /Users/alexcain/.env "
        f"and mailed to alex.cain@example.com.")])

    ingest(db, ClaudeCodeAdapter(root=str(transcripts)))

    rows = db.query(MemoryShard).filter(MemoryShard.source.like("transcript:%")).all()
    assert rows, "the fixture should have produced a shard"
    blob = " ".join(r.text for r in rows)
    for secret in ("sk-abcd1234efgh5678", "alexcain", "alex.cain@example.com"):
        assert secret not in blob


def test_a_row_records_that_scrubbing_ran(db, transcripts):
    """Not that it FOUND something. A False here means "written before the redactor
    existed", which is a different claim from "clean" — and inferring cleanliness from text
    that looks fine is the same mistake."""
    _write(transcripts, "s.jsonl", [_line(LESSON)])
    ingest(db, ClaudeCodeAdapter(root=str(transcripts)))

    row = db.query(MemoryShard).filter(MemoryShard.source.like("transcript:%")).first()
    assert row.scrubbed is True


def test_every_producer_inherits_scrubbing_not_just_ingest(db):
    """It lives on the write path in add_memory precisely so extract_lessons, the grill and
    agent writes cannot each forget to ask."""
    from app.services import memory as mem_svc

    shard = mem_svc.add_memory(db, text_body="key sk-abcd1234efgh5678 leaked",
                               project_id="core")
    assert "sk-abcd1234efgh5678" not in shard.text and shard.scrubbed is True


# ---- incremental, and it does not duplicate ---------------------------------------------
def test_ingesting_the_same_transcript_twice_records_nothing_new(db, transcripts):
    """THE acceptance criterion. The ladder counts corroborating shards, so a duplicate does
    not merely waste work — it MANUFACTURES corroboration, promoting a lesson that only
    ever happened once."""
    _write(transcripts, "s.jsonl", [_line(LESSON), _line(LESSON + " Twice over.")])
    adapter = ClaudeCodeAdapter(root=str(transcripts))

    first = ingest(db, adapter)
    second = ingest(db, adapter)

    assert first["recorded"] == 2
    assert second["recorded"] == 0 and second["events"] == 0
    assert db.query(MemoryShard).filter(MemoryShard.source.like("transcript:%")).count() == 2


def test_appended_lines_are_picked_up_on_the_next_run(db, transcripts):
    """Incremental has to mean incremental, not once-only: a session is still being written
    while ingest runs."""
    path = _write(transcripts, "s.jsonl", [_line(LESSON)])
    adapter = ClaudeCodeAdapter(root=str(transcripts))
    ingest(db, adapter)

    with open(path, "a") as fh:
        fh.write(_line(LESSON + " A later realisation entirely."))

    assert ingest(db, adapter)["recorded"] == 1
    assert db.query(MemoryShard).filter(MemoryShard.source.like("transcript:%")).count() == 2


def test_the_watermark_is_recorded_per_source(db, transcripts):
    _write(transcripts, "a.jsonl", [_line(LESSON)])
    _write(transcripts, "b.jsonl", [_line(LESSON), _line(LESSON + " More.")])
    ingest(db, ClaudeCodeAdapter(root=str(transcripts)))

    marks = {m.source.split("/")[-1]: m.watermark for m in db.query(IngestWatermark).all()}
    assert marks == {"a.jsonl": "1", "b.jsonl": "2"}


# ---- resilience is a hard requirement ----------------------------------------------------
def test_a_corrupted_record_is_skipped_and_the_run_completes(db, transcripts):
    """THE acceptance criterion. A truncated final line is the NORMAL state of a session in
    progress, not a corruption worth failing a run over."""
    _write(transcripts, "s.jsonl", [
        _line(LESSON),
        '{"sessionId": "sess-1", "message": {"content": [{"type": "te\n',   # truncated
        "not json at all\n",
        _line(LESSON + " And a second, later lesson worth keeping."),
    ])

    stats = ingest(db, ClaudeCodeAdapter(root=str(transcripts)))
    assert stats["recorded"] == 2, "the good records on both sides must survive"


def test_one_failed_WRITE_does_not_kill_the_rest_of_the_run(db, transcripts, monkeypatch):
    """The parse-failure case above covers a malformed record. This covers a failed WRITE,
    which is a different animal: a rejected insert leaves the session poisoned, so without
    a rollback every later event dies with PendingRollbackError and the run ends anyway —
    noisily rather than cleanly, reporting zero while looking like it tried.

    Found on the first real ingest attempt: one row was rejected and all 40k events behind
    it failed in cascade."""
    from app.services import memory as mem_svc

    calls = {"n": 0}
    real = mem_svc.add_memory
    db.add(MemoryShard(id="m_taken", text="already here", project_id="core"))
    db.commit()

    def flaky(db_, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            # A failed FLUSH, which is what actually happens: a rejected INSERT leaves the
            # session unusable. A failed SELECT does not, which is why the first version of
            # this test passed with the rollback removed.
            db_.add(MemoryShard(id="m_taken", text="duplicate", project_id="core"))
            db_.commit()
        return real(db_, **kw)

    monkeypatch.setattr(mem_svc, "add_memory", flaky)
    _write(transcripts, "s.jsonl", [_line(LESSON + f" Number {i}.") for i in range(4)])

    stats = ingest(db, ClaudeCodeAdapter(root=str(transcripts)))
    assert stats["recorded"] == 3, "the three good rows after the failure must survive"


def test_a_vanished_root_is_an_empty_run_not_a_crash(db, tmp_path):
    """The common case on a server is that nobody has ever run the harness there, and that
    is not an error condition.

    This and the test below pin PATHLIB's tolerance, not ours. An `except OSError` sat in
    `discover` until sabotage showed it could never fire — rglob swallows a missing root, a
    locked one, and a file-as-root alike. The handler was removed rather than kept as an
    unreachable reassurance; these tests are what stop a future refactor to `os.scandir`
    losing the behaviour silently."""
    stats = ingest(db, ClaudeCodeAdapter(root=str(tmp_path / "nope")))
    assert stats == {"sources": 0, "events": 0, "recorded": 0, "skipped_sources": 0}


def test_an_unreadable_root_is_an_empty_run_not_a_crash(db, tmp_path):
    """PRD-16 names a locked path as a WARN-and-skip. A permission-denied ROOT is how that
    arrives on a server — a transcript directory owned by another user."""
    root = tmp_path / "locked"
    root.mkdir()
    (root / "s.jsonl").write_text(_line(LESSON))
    root.chmod(0o000)
    try:
        stats = ingest(db, ClaudeCodeAdapter(root=str(root)))
        assert stats["sources"] == 0 and stats["recorded"] == 0
    finally:
        root.chmod(0o755)


def test_an_unreadable_source_does_not_stop_the_others(db, transcripts):
    _write(transcripts, "good.jsonl", [_line(LESSON)])
    bad = transcripts / "bad.jsonl"
    bad.write_text(_line(LESSON))
    bad.chmod(0o000)
    try:
        stats = ingest(db, ClaudeCodeAdapter(root=str(transcripts)))
        assert stats["recorded"] >= 1, "the readable source must still be ingested"
    finally:
        bad.chmod(0o644)


# ---- what it records, and what it refuses to decide ---------------------------------------
def test_evidence_enters_as_a_candidate_never_published(db, transcripts):
    """PRD-16's non-goal, in as many words: no second scorer. Graphban's existing triage
    path stays the sole owner of "is this worth keeping" — publishing here would run a
    parallel lifecycle beside the one that already exists."""
    _write(transcripts, "s.jsonl", [_line(LESSON)])
    ingest(db, ClaudeCodeAdapter(root=str(transcripts)))

    row = db.query(MemoryShard).filter(MemoryShard.source.like("transcript:%")).first()
    assert row.status == "candidate" and row.origin == "ingest:claude-code"


def test_fragments_are_not_recorded_as_evidence(db, transcripts):
    """"ok", "yes", a bare path. A corpus of fragments is one nobody can promote from."""
    _write(transcripts, "s.jsonl", [_line("ok"), _line("yes"), _line(LESSON)])

    assert ingest(db, ClaudeCodeAdapter(root=str(transcripts)))["recorded"] == 1


def test_the_source_names_the_session_it_came_from(db, transcripts):
    """The promotion ladder gates on DISTINCT sessions, so a lesson repeated in one long
    session cannot promote itself. That needs the session on the row."""
    _write(transcripts, "s.jsonl", [_line(LESSON, sessionId="sess-abc")])
    ingest(db, ClaudeCodeAdapter(root=str(transcripts)))

    row = db.query(MemoryShard).filter(MemoryShard.source.like("transcript:%")).first()
    assert row.source == "transcript:claude-code:sess-abc"


def _tool_call(result, name="Bash"):
    """A record in the shape a REAL Claude Code transcript uses. The first version of these
    tests invented `{"toolUseResult": {"exitCode": 1}}`, which no harness produces — so it
    passed while the parser extracted nothing from 18,167 real tool calls (GRPH-342)."""
    return json.dumps({
        "sessionId": "s", "type": "assistant", "timestamp": "t",
        "message": {"content": [{"type": "tool_use", "name": name}]},
        "toolUseResult": result,
    }) + "\n"


def test_a_tool_call_is_parsed(db, transcripts):
    adapter = ClaudeCodeAdapter(root=str(transcripts))
    path = _write(transcripts, "s.jsonl", [_tool_call({"stdout": "done", "stderr": ""})])

    events, _ = adapter.parse(path, None)
    assert events[0].tool_name == "Bash"


@pytest.mark.parametrize("result,outcome", [
    ({"stdout": "ok", "stderr": ""}, "ok"),
    ({"stdout": "", "stderr": "", "interrupted": True}, "failed"),
    # A recorded exit code is a fact. Claude Code writes none, but the Event shape is shared.
    ({"exitCode": 0}, "ok"),
    ({"exitCode": 2}, "failed"),
    ({}, "unknown"),
    # **Not a failure, however much it looks like one.** These two shapes are why the
    # inference was removed: on the real corpus they produced 90 and 28 false failures
    # respectively, and `Shell cwd was reset` is a routine notice on a command that WORKED.
    ({"stdout": "fine", "stderr": "\nShell cwd was reset to /repo"}, "ok"),
    ({"stdout": "", "stderr": "", "returnCodeInterpretation": "No matches found"}, "ok"),
])
def test_the_outcome_is_derived_from_what_the_harness_actually_records(db, transcripts,
                                                                       result, outcome):
    """PRD-16 makes the failure signal first-class — "a lesson about a command that FAILED
    is worth more than one about a command that ran". Claude Code emits no numeric exit
    code, so `exit_code` alone left every real tool call reading as untroubled."""
    adapter = ClaudeCodeAdapter(root=str(transcripts))
    path = _write(transcripts, "s.jsonl", [_tool_call(result)])

    events, _ = adapter.parse(path, None)
    assert events[0].metadata["outcome"] == outcome


def _failed_result(text="Exit code 1\npytest: 3 failed", tool_use_id="t1"):
    """The shape a FAILED tool call actually arrives in — verified against the corpus.

    There is **no `toolUseResult`**: the call produced no structured output, so the error
    text sits directly on the `tool_result` block, and `is_error` carries the verdict. All
    154 real errors look like this, and every one was being dropped (GRPH-345).
    """
    return json.dumps({
        "sessionId": "s", "type": "user", "timestamp": "t",
        "message": {"content": [{"type": "tool_result", "tool_use_id": tool_use_id,
                                 "is_error": True, "content": text}]},
    }) + "\n"


def test_the_harness_verdict_beats_any_inference_about_it(db, transcripts):
    """`is_error` is what Claude Code itself recorded. Inferring from stderr instead
    disagreed with it on 118 of 118 events in the real corpus."""
    adapter = ClaudeCodeAdapter(root=str(transcripts))
    path = _write(transcripts, "s.jsonl", [json.dumps({
        "sessionId": "s", "type": "user", "timestamp": "t",
        "message": {"content": [{"type": "tool_result", "is_error": False,
                                 "content": "done"}]},
        "toolUseResult": {"stdout": "done", "stderr": "warning: deprecated flag"},
    }) + "\n"])

    events, _ = adapter.parse(path, None)
    assert events[0].metadata["outcome"] == "ok", "stderr on a call the harness passed"


def test_a_failed_call_carries_its_error_text_with_no_structured_result(db, transcripts):
    """THE regression. A failed call has no `toolUseResult` at all, so the old guard —
    no text, no tool name, no result dict — dropped all 154 real errors while GRPH-342
    believed it had recovered the failure signal."""
    adapter = ClaudeCodeAdapter(root=str(transcripts))
    path = _write(transcripts, "s.jsonl", [_failed_result()])

    events, _ = adapter.parse(path, None)
    assert len(events) == 1
    assert events[0].metadata["outcome"] == "failed"
    assert "pytest: 3 failed" in events[0].text


def test_a_tool_result_record_is_not_dropped(db, transcripts):
    """The deeper half of GRPH-342, and invisible without real data. A tool CALL and its
    RESULT are separate records — 910 results against 18,173 calls in the real corpus — and
    a result carries neither text nor a tool name. Requiring one dropped every result
    record, and with it the entire failure signal."""
    adapter = ClaudeCodeAdapter(root=str(transcripts))
    path = _write(transcripts, "s.jsonl", [json.dumps({
        "sessionId": "s", "type": "user", "timestamp": "t",
        "toolUseResult": {"stdout": "", "stderr": "fatal: not a git repository"},
    }) + "\n"])

    events, _ = adapter.parse(path, None)
    assert len(events) == 1
    assert "not a git repository" in events[0].text


def test_stderr_leads_the_result_text(db, transcripts):
    """A command that failed is the higher-value signal, and burying it under kilobytes of
    successful stdout is how it gets lost."""
    adapter = ClaudeCodeAdapter(root=str(transcripts))
    path = _write(transcripts, "s.jsonl", [json.dumps({
        "sessionId": "s", "type": "user", "timestamp": "t",
        "toolUseResult": {"stdout": "x" * 900, "stderr": "permission denied"},
    }) + "\n"])

    events, _ = adapter.parse(path, None)
    assert events[0].text.startswith("stderr: permission denied")


def test_a_missing_result_is_unknown_not_success(db, transcripts):
    """The sixth instance of the class the AGENTS.md default names: `None` cannot mean both
    "it succeeded" and "nothing recorded whether it did"."""
    adapter = ClaudeCodeAdapter(root=str(transcripts))
    path = _write(transcripts, "s.jsonl", [_line(LESSON)])

    events, _ = adapter.parse(path, None)
    assert events[0].metadata["outcome"] == "unknown"
    assert events[0].exit_code is None


def test_a_numeric_exit_code_is_still_read_when_a_harness_emits_one(db, transcripts):
    """The field stays because the Event shape is shared and other harnesses do provide it."""
    adapter = ClaudeCodeAdapter(root=str(transcripts))
    path = _write(transcripts, "s.jsonl", [_tool_call({"exitCode": 1, "stderr": "boom"})])

    events, _ = adapter.parse(path, None)
    assert events[0].exit_code == 1 and events[0].metadata["outcome"] == "failed"


def test_kind_is_a_plain_string_so_a_new_harness_needs_no_core_change(db, transcripts):
    """An enum would make every new adapter a change to shared code, which is how a plugin
    point stops being one."""
    adapter = ClaudeCodeAdapter(root=str(transcripts))
    path = _write(transcripts, "s.jsonl", [_line(LESSON, type="something-brand-new")])

    events, _ = adapter.parse(path, None)
    assert events[0].kind == "something-brand-new"


# ---- what counts as a lesson (GRPH-345) -----------------------------------------------------
# The filter that exists because the first REAL run recorded 1,088 shards from one transcript
# — pytest summaries, branch deletions, assertion fragments. PRD-16 says the loop exists so
# the corpus "does not become landfill"; at that rate it WAS the landfill. Every test below
# is a claim about where lessons come from, and each was written against measured output.
def _ok_result(stdout="-- Docs: https://docs.pytest.org/\n124 passed in 64.80s"):
    """A tool call that WORKED. The single largest source of junk: 6,796 of 6,838 tool
    results in the sample corpus, every one of them recorded as a lesson."""
    return json.dumps({
        "sessionId": "s", "type": "user", "timestamp": "t",
        "message": {"content": [{"type": "tool_result", "is_error": False,
                                 "content": stdout}]},
        "toolUseResult": {"stdout": stdout, "stderr": ""},
    }) + "\n"


def test_a_command_that_SUCCEEDED_is_not_a_lesson(db, transcripts):
    """THE cut. `124 passed` is what is supposed to happen, and a corpus of things going to
    plan teaches nothing."""
    _write(transcripts, "s.jsonl", [_ok_result()])

    assert ingest(db, ClaudeCodeAdapter(root=str(transcripts)))["recorded"] == 0


def test_a_command_that_FAILED_is_a_lesson(db, transcripts):
    """The other half, and the half PRD-16 calls first-class. Without this the filter above
    would be indistinguishable from switching ingest off."""
    _write(transcripts, "s.jsonl", [_failed_result(
        "Exit code 23\nrsync: [generator] failed to set times on /srv/app: "
        "Operation not permitted (1)")])

    assert ingest(db, ClaudeCodeAdapter(root=str(transcripts)))["recorded"] == 1
    row = db.query(MemoryShard).filter(MemoryShard.source.like("transcript:%")).first()
    assert "Operation not permitted" in row.text


def test_a_tool_result_is_never_mistaken_for_a_person_talking(db, transcripts):
    """Claude Code delivers EVERY tool result as a user-role record. Left alone they inherit
    the standing of a human turn, so any result the harness did not flag sails past the prose
    bar — which is how "Async agent launched successfully" became a lesson 600 times over."""
    long_output = "Async agent launched successfully. " * 40
    _write(transcripts, "s.jsonl", [json.dumps({
        "sessionId": "s", "type": "user", "timestamp": "t",
        "message": {"content": [{"type": "tool_result", "content": long_output}]},
    }) + "\n"])

    assert ingest(db, ClaudeCodeAdapter(root=str(transcripts)))["recorded"] == 0


@pytest.mark.parametrize("text", [
    "<task-notification><task-id>abc</task-id><output>" + "x" * 500 + "</output>",
    "<system-reminder>" + "y" * 500 + "</system-reminder>",
    "<bash-stdout>" + "z" * 500 + "</bash-stdout>",
    "This session is being continued from a previous conversation. " + "w" * 500,
    "Base directory for this skill: /skills/use-railway\n" + "v" * 500,
])
def test_the_harness_writing_in_the_user_slot_is_not_the_user(db, transcripts, text):
    """All of these arrive as `type: "user"` and all are long enough to clear the bar. On the
    real corpus they were HALF of every surviving message — 19 of them context-compaction
    summaries of 11k–19k characters, written by the assistant about the session itself."""
    _write(transcripts, "s.jsonl", [_line(text)])

    assert ingest(db, ClaudeCodeAdapter(root=str(transcripts)))["recorded"] == 0


def test_assistant_narration_is_not_a_lesson(db, transcripts):
    """It is work in progress, and it is where a confident wrong explanation looks exactly
    like a right one. The durable form of anything worth keeping is what the human then
    confirmed or corrected."""
    _write(transcripts, "s.jsonl", [_line(LESSON, type="assistant")])

    assert ingest(db, ClaudeCodeAdapter(root=str(transcripts)))["recorded"] == 0


def test_a_persons_own_words_are_a_lesson(db, transcripts):
    """The one place in a transcript where somebody says what they actually wanted. If this
    fails the filter has eaten the signal along with the noise."""
    _write(transcripts, "s.jsonl", [_line(LESSON)])

    assert ingest(db, ClaudeCodeAdapter(root=str(transcripts)))["recorded"] == 1


@pytest.mark.parametrize("chatter", [
    "merged, deploy it",
    "yes do that",
    "run the ingest",
    # **Pins the bar itself.** Everything above is under 40 characters and so would be
    # dropped by the OLD threshold too — a test that cannot tell 40 from 400 does not
    # defend the value it was written for. This one sits between the two, and sabotage
    # found it: reverting MIN_PROSE_CHARS to 40 passed all 53 tests without it.
    "merged, deploy it and run the audit on prd-12 when the pipeline goes green, then move "
    "the two finished items over and start on the next one in the list please",
])
def test_workflow_chatter_is_below_the_prose_bar(db, transcripts, chatter):
    """The median user message in the real corpus is 16 characters, and p75 is 49 — a bar of
    40 waved most of a session's traffic through as lessons."""
    _write(transcripts, "s.jsonl", [_line(chatter)])
    assert ingest(db, ClaudeCodeAdapter(root=str(transcripts)))["recorded"] == 0


def test_an_enormous_record_is_truncated_rather_than_dropped(db, transcripts):
    """The corpus contains a single 804,441-character record. Past a few thousand characters
    a shard is a transcript rather than a lesson — the embedder cannot represent it and the
    ladder cannot compare it — but the opening is usually still the point."""
    from app.services.ingest.runner import MAX_EVIDENCE_CHARS

    _write(transcripts, "s.jsonl", [_line(LESSON + " " + "padding " * 200_000)])

    assert ingest(db, ClaudeCodeAdapter(root=str(transcripts)))["recorded"] == 1
    row = db.query(MemoryShard).filter(MemoryShard.source.like("transcript:%")).first()
    assert len(row.text) <= MAX_EVIDENCE_CHARS
    assert "migration guard" in row.text


def test_a_realistic_session_yields_a_handful_of_lessons_not_hundreds(db, transcripts):
    """**The measurement that matters**, and the one no unit test caught before the ingest
    was run for real. A session is overwhelmingly successful tool calls; a handful of things
    actually went wrong or were actually decided.

    Asserted as a RATIO of what was seen, so the test cannot pass by the parser silently
    reading nothing — which is how the original 1,088-shard run looked healthy."""
    lines = [_ok_result() for _ in range(200)]
    lines += [_line("<system-reminder>" + "x" * 500 + "</system-reminder>") for _ in range(20)]
    lines += [_line("merged") for _ in range(20)]
    lines += [_line(LESSON, sessionId="s")]
    lines += [_failed_result("Exit code 1\nFrontend tests + typecheck + build  fail  5s")]
    _write(transcripts, "s.jsonl", lines)

    stats = ingest(db, ClaudeCodeAdapter(root=str(transcripts)))

    assert stats["events"] > 200, "the parser must actually be reading the transcript"
    assert stats["recorded"] == 2, f"expected the 1 decision + 1 failure, got {stats}"


# ---- the harness says so itself (GRPH-347) --------------------------------------------------
# The filter above started as a denylist of opening lines, which meant every new skill or
# slash command was a shape nobody had added yet — and the gap only surfaced as junk in the
# corpus weeks later. Claude Code records what it wrote itself; reading that is a positive
# signal rather than a guess about wording.
# Comfortably over MIN_PROSE_CHARS on purpose. The first version was ~320 characters, so
# every flag test below passed because the message was too SHORT to be evidence — the flags
# were never exercised at all. Asserted by test_the_fixture_is_long_enough_to_be_evidence.
HUMAN = ("i see it in the tracker so it is working. the whole point of this tool is to create "
         "a join between the spec and the work, and right now the two drift apart the moment "
         "anybody edits either one of them without telling the other. please make the link "
         "survive a rename, because that is the case that keeps biting us in practice. we "
         "have hit it three times now and each time somebody noticed by accident rather than "
         "because anything told them, which is the part i actually want fixed here.")


def test_the_fixture_is_long_enough_to_be_evidence(db, transcripts):
    """Guards every flag test below. If HUMAN drops under the prose bar they all pass
    vacuously — the message would be filtered for its length and the flag never consulted."""
    from app.services.ingest.runner import MIN_PROSE_CHARS

    assert len(HUMAN) > MIN_PROSE_CHARS
    _write(transcripts, "s.jsonl", [_line(HUMAN)])
    assert ingest(db, ClaudeCodeAdapter(root=str(transcripts)))["recorded"] == 1


@pytest.mark.parametrize("flag", [
    {"isMeta": True},                     # skill bodies, system-prompt sections
    {"isCompactSummary": True},           # a summary written ABOUT the session
    {"isVisibleInTranscriptOnly": True},  # shown to the reader, never part of the talk
    {"promptSource": "system"},           # generated, not typed
])
def test_a_record_the_harness_flags_as_its_own_is_not_a_lesson(db, transcripts, flag):
    """Structural, so a brand-new skill needs no change here. The text of these is
    indistinguishable from a person writing at length — an 8,835-character design brief and
    a 27,808-character skill body both read as prose."""
    _write(transcripts, "s.jsonl", [_line(HUMAN, **flag)])

    assert ingest(db, ClaudeCodeAdapter(root=str(transcripts)))["recorded"] == 0


def test_the_flag_beats_the_wording(db, transcripts):
    """The text here is a real human message copied verbatim. Only the flag distinguishes
    it, which is the whole point of preferring the flag."""
    _write(transcripts, "s.jsonl", [_line(HUMAN), _line(HUMAN, isMeta=True)])

    assert ingest(db, ClaudeCodeAdapter(root=str(transcripts)))["recorded"] == 1


def test_an_ordinary_turn_carries_none_of_them_and_is_kept(db, transcripts):
    """The other half. A filter that dropped everything would pass every test above."""
    _write(transcripts, "s.jsonl", [_line(HUMAN, userType="external")])

    assert ingest(db, ClaudeCodeAdapter(root=str(transcripts)))["recorded"] == 1


def test_an_older_transcript_without_the_flags_still_gets_filtered(db, transcripts):
    """The fallback, and why it stays. A transcript written by an older Claude Code carries
    none of these fields, and treating their absence as "a person typed this" would be the
    same mistake as reading an absence as a clean result."""
    _write(transcripts, "s.jsonl", [
        _line("Base directory for this skill: /skills/x\n" + "v" * 500),
        _line("<task-notification>" + "y" * 500 + "</task-notification>"),
    ])

    assert ingest(db, ClaudeCodeAdapter(root=str(transcripts)))["recorded"] == 0


# ---- episodes: the failure AND what fixed it (GRPH-349) --------------------------------------
# A failure on its own is a symptom. Measured: feeding bare failures to the classifier drafted
# confidently wrong rules — `cd:1: no such file or directory: backend` (x10) became "Ensure
# 'backend' directory exists before running commands", rendered as a shell guard, for a
# directory that exists. The shell does not persist a working directory between calls, and
# nothing in the failure text says so. What disambiguates it is what was done DIFFERENTLY next.
def _call(tool_use_id, name="Bash", **inp):
    return json.dumps({
        "sessionId": "s", "type": "assistant", "timestamp": "t",
        "message": {"content": [{"type": "tool_use", "id": tool_use_id,
                                 "name": name, "input": inp}]},
    }) + "\n"


def _result(tool_use_id, is_error=False, content="ok"):
    return json.dumps({
        "sessionId": "s", "type": "user", "timestamp": "t",
        "message": {"content": [{"type": "tool_result", "tool_use_id": tool_use_id,
                                 "is_error": is_error, "content": content}]},
    }) + "\n"


BROKE = "Exit code 23\nrsync: failed to set times on /srv/sync: Operation not permitted"
CMD_BAD = "rsync -az --delete --exclude .git --exclude .env ./ srv:~/app/"
CMD_FIX = "rsync -az --delete --exclude .git --exclude .env --exclude sync ./ srv:~/app/"


def _episode_shard(db):
    return db.query(MemoryShard).filter(MemoryShard.source.like("transcript:%")).first()


def test_a_failure_and_its_fix_become_one_lesson(db, transcripts):
    """THE acceptance criterion. Both halves in one shard, so the DIFFERENCE — here
    `--exclude sync` — is visible to whatever has to state the rule."""
    _write(transcripts, "s.jsonl", [
        _call("t1", command=CMD_BAD), _result("t1", is_error=True, content=BROKE),
        _call("t2", command=CMD_FIX), _result("t2"),
    ])

    assert ingest(db, ClaudeCodeAdapter(root=str(transcripts)))["recorded"] == 1
    row = _episode_shard(db)
    assert "Operation not permitted" in row.text, "the failure"
    assert "--exclude sync" in row.text, "and what fixed it"
    assert row.origin.endswith(":resolved")


def test_the_failure_is_not_also_filed_on_its_own(db, transcripts):
    """It would be the symptom sitting beside the lesson, and the two would cluster and
    corroborate each other — a pair manufacturing its own evidence."""
    _write(transcripts, "s.jsonl", [
        _call("t1", command=CMD_BAD), _result("t1", is_error=True, content=BROKE),
        _call("t2", command=CMD_FIX), _result("t2"),
    ])

    assert ingest(db, ClaudeCodeAdapter(root=str(transcripts)))["recorded"] == 1


def test_an_identical_retry_is_transient_not_a_lesson(db, transcripts):
    """Nothing was changed, so nothing was learned. It says the tool is flaky, which is a
    different claim from "here is what to do instead"."""
    _write(transcripts, "s.jsonl", [
        _call("t1", command=CMD_BAD), _result("t1", is_error=True, content=BROKE),
        _call("t2", command=CMD_BAD), _result("t2"),
    ])
    ingest(db, ClaudeCodeAdapter(root=str(transcripts)))

    row = _episode_shard(db)
    assert row.origin.endswith(":transient")
    assert "TRANSIENT" in row.text and "Resolved by" not in row.text


def test_a_failure_nothing_fixed_is_marked_unresolved(db, transcripts):
    """Recorded, because repeated friction is worth knowing about. Marked, because an
    unresolved failure must not read as a lesson."""
    _write(transcripts, "s.jsonl", [
        _call("t1", command=CMD_BAD), _result("t1", is_error=True, content=BROKE)])
    ingest(db, ClaudeCodeAdapter(root=str(transcripts)))

    row = _episode_shard(db)
    assert row.origin.endswith(":unresolved")
    assert row.text.startswith("UNRESOLVED")


def test_a_different_tool_succeeding_is_not_a_fix(db, transcripts):
    """`Read` working afterwards says nothing about why `Bash` failed."""
    _write(transcripts, "s.jsonl", [
        _call("t1", command=CMD_BAD), _result("t1", is_error=True, content=BROKE),
        _call("t2", name="Read", file_path="/x/y.py"), _result("t2"),
    ])
    ingest(db, ClaudeCodeAdapter(root=str(transcripts)))

    assert _episode_shard(db).origin.endswith(":unresolved")


def test_an_unrelated_command_succeeding_is_not_a_fix(db, transcripts):
    """Same tool, different work. Bash runs constantly, so "the next Bash that worked"
    would pair nearly every failure with something irrelevant."""
    _write(transcripts, "s.jsonl", [
        _call("t1", command=CMD_BAD), _result("t1", is_error=True, content=BROKE),
        _call("t2", command="git log --oneline -5"), _result("t2"),
    ])
    ingest(db, ClaudeCodeAdapter(root=str(transcripts)))

    assert _episode_shard(db).origin.endswith(":unresolved")


def test_prose_arguments_do_not_make_a_rerun_look_like_a_fix(db, transcripts):
    """A Bash `description` is commentary ABOUT the call. Comparing it made an identical
    rerun ("Show final CI results" vs "Show CI results after rerun") read as a change."""
    _write(transcripts, "s.jsonl", [
        _call("t1", command="gh pr checks 7", description="Show final CI results"),
        _result("t1", is_error=True, content="Exit code 1\nfrontend tests fail"),
        _call("t2", command="gh pr checks 7", description="Show CI results after rerun"),
        _result("t2"),
    ])
    ingest(db, ClaudeCodeAdapter(root=str(transcripts)))

    assert _episode_shard(db).origin.endswith(":transient")


def test_two_different_targets_are_not_a_fix_for_each_other(db, transcripts):
    """`update_item AL-41` failing and `update_item AL-40` succeeding is two items, not a
    lesson. Real data paired them: they share the word "done", two tokens out of three."""
    _write(transcripts, "s.jsonl", [
        _call("t1", name="update_item", id="AL-41", status="done"),
        _result("t1", is_error=True, content="internal_error executing 'update_item' retry"),
        _call("t2", name="update_item", id="AL-40", status="done"), _result("t2"),
    ])
    ingest(db, ClaudeCodeAdapter(root=str(transcripts)))

    assert _episode_shard(db).origin.endswith(":unresolved")


def test_a_fix_far_beyond_the_window_is_not_credited(db, transcripts):
    """Bounded on purpose: the further away a success is, the less it has to do with the
    failure, and an unbounded search would always find something."""
    from app.services.ingest.claude_code import EPISODE_LOOKAHEAD

    lines = [_call("t1", command=CMD_BAD), _result("t1", is_error=True, content=BROKE)]
    for i in range(EPISODE_LOOKAHEAD + 2):
        lines += [_call(f"n{i}", command=f"echo filler {i}"), _result(f"n{i}")]
    lines += [_call("t2", command=CMD_FIX), _result("t2")]
    _write(transcripts, "s.jsonl", lines)
    ingest(db, ClaudeCodeAdapter(root=str(transcripts)))

    assert _episode_shard(db).origin.endswith(":unresolved")


# Each of these pins ONE rule, with a case where that rule is the only thing standing
# between a false pairing and an invented lesson. Sabotage caught all three: the tests above
# passed with the rule removed, because some other guard happened to catch their fixtures.
def test_identity_arguments_are_what_is_compared(db, transcripts):
    """Not "every field that isn't prose". Two items differing only in id share `status` and
    `project_id`, which is 3 tokens of 4 in common — enough to pair them on a whole-input
    comparison and credit one item's success as the other's fix."""
    _write(transcripts, "s.jsonl", [
        _call("t1", name="update_item", id="AL-41", status="done", project_id="agentledger"),
        _result("t1", is_error=True, content="internal_error executing 'update_item' retry"),
        _call("t2", name="update_item", id="AL-40", status="done", project_id="agentledger"),
        _result("t2"),
    ])
    ingest(db, ClaudeCodeAdapter(root=str(transcripts)))

    assert _episode_shard(db).origin.endswith(":unresolved")


def test_two_short_identities_must_match_exactly(db, transcripts):
    """`/a/b.py` and `/a/c.py` share two tokens of three — 0.67, over the bar — so a partial
    match on a short identity would call reading one file the fix for failing on another."""
    _write(transcripts, "s.jsonl", [
        _call("t1", name="Read", file_path="/a/b.py"),
        _result("t1", is_error=True, content="Exit code 1\nfile not found: /a/b.py"),
        _call("t2", name="Read", file_path="/a/c.py"), _result("t2"),
    ])
    ingest(db, ClaudeCodeAdapter(root=str(transcripts)))

    assert _episode_shard(db).origin.endswith(":unresolved")


def test_the_same_target_under_a_different_tool_is_not_a_fix(db, transcripts):
    """Reading the test file after the test run failed is the single most common thing that
    happens next, and it fixes nothing. The arguments overlap completely — only the tool
    name distinguishes them."""
    _write(transcripts, "s.jsonl", [
        _call("t1", command="pytest tests/test_foo.py -q"),
        _result("t1", is_error=True, content="Exit code 1\n3 failed in tests/test_foo.py"),
        _call("t2", name="Read", file_path="tests/test_foo.py"), _result("t2"),
    ])
    ingest(db, ClaudeCodeAdapter(root=str(transcripts)))

    assert _episode_shard(db).origin.endswith(":unresolved")
