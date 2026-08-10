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
    row = {"sessionId": "sess-1", "type": "assistant", "cwd": "/repo",
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


LESSON = ("The migration guard caught a missing revision because the range in AGENTS.md "
          "was never bumped after adding one.")


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


def test_a_tool_call_with_an_exit_code_is_parsed(db, transcripts):
    """A lesson about a command that FAILED is worth more than one about a command that
    ran, so the exit code is a first-class field rather than buried in text."""
    adapter = ClaudeCodeAdapter(root=str(transcripts))
    path = _write(transcripts, "s.jsonl", [json.dumps({
        "sessionId": "s", "type": "assistant", "timestamp": "t",
        "message": {"content": [{"type": "tool_use", "name": "Bash"}]},
        "toolUseResult": {"exitCode": 1},
    }) + "\n"])

    events, _ = adapter.parse(path, None)
    assert events[0].tool_name == "Bash" and events[0].exit_code == 1


def test_kind_is_a_plain_string_so_a_new_harness_needs_no_core_change(db, transcripts):
    """An enum would make every new adapter a change to shared code, which is how a plugin
    point stops being one."""
    adapter = ClaudeCodeAdapter(root=str(transcripts))
    path = _write(transcripts, "s.jsonl", [_line(LESSON, type="something-brand-new")])

    events, _ = adapter.parse(path, None)
    assert events[0].kind == "something-brand-new"
