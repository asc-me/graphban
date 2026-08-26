"""Something actually runs the loop (GRPH-353 / PRD-16).

PRD-16 shipped a complete engine with no caller: `ingest`, `classify` and `draft_pending`
were reachable from the test suite and from nowhere else, so on a running instance the loop
had never executed once. These tests are mostly about the properties that decide whether a
scheduled driver stays switched on rather than being disabled a week later:

- a second run over unchanged input costs **zero** provider calls;
- one broken harness does not take the other harnesses' work down with it;
- the two stages stay either side of human triage — the machine never publishes.

The stage-boundary test is the one that matters most, and it is the one an inattentive
implementation passes by accident: a driver that ran ingest and classification in one pass
would look correct on a corpus somebody had already triaged, and do nothing at all on a
fresh install.
"""
import json

import pytest

from app.models import ArtifactRecommendation, IngestWatermark, MemoryShard
from app.services import learning as learning_svc
from app.services.ingest.claude_code import ClaudeCodeAdapter
from app.services.platform import Resolved


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def transcripts(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    return d


@pytest.fixture()
def proj(db):
    """A project of its own. The seeded prototype dataset ships PUBLISHED shards in `core`,
    so a stage-B assertion made there would be counting the seed rather than the behaviour —
    `classified == 0` came back as 5 before this existed."""
    from app.models import Project

    db.add(Project(id="learning", name="Learning", tag="LN"))
    db.commit()
    return "learning"


LESSON = (
    "The migration guard caught a missing revision because the range in AGENTS.md was never "
    "bumped after adding one. Please make the guard part of the standard loop rather than "
    "something we remember to run, because we have now hit this three separate times and "
    "each time it was found by a human reading a diff rather than by anything automatic. "
    "The fix should fail the build, not warn, since a warning in a long log is the same as "
    "silence for our purposes here.")


def _line(text, **over):
    row = {"sessionId": "sess-1", "type": "user", "cwd": "/repo",
           "timestamp": "2026-08-09T00:00:00Z",
           "message": {"content": [{"type": "text", "text": text}]}}
    row.update(over)
    return json.dumps(row) + "\n"


def _write(d, name, lines):
    (d / name).write_text("".join(lines))


def _adapter(transcripts):
    return ClaudeCodeAdapter(root=str(transcripts))


@pytest.fixture()
def model(monkeypatch):
    """A configured chat provider that COUNTS its calls.

    The count is the instrument for "a repeat run is free". Without a counter the assertion
    would have to be about output, and output is identical whether the second run reused a
    stored draft or paid to regenerate the same bytes.
    """
    from app.services import platform as platform_svc

    state = {"calls": 0}

    class Chat:
        def chat(self, *, system, context, question, temperature=None):
            state["calls"] += 1
            if "LESSONS:" in context and "EXISTING ARTIFACTS" in context:
                ids = [l["id"] for l in json.loads(context.split("LESSONS:\n")[1])]
                # A scope PER LESSON, so N lessons produce N recommendations. One shared
                # scope makes the second supersede the first — correct behaviour, and it
                # silently reduced a two-row fixture to one queued row.
                #
                # `variant{n}` rather than a bare `{n}`: `_scope_key` drops tokens of two
                # characters or fewer, so "migration guard 0" and "migration guard 1"
                # normalise to the SAME key and collide exactly as if unnumbered.
                return json.dumps([{"id": i, "tier": "rule",
                                    "scope": f"migration guard variant{n}",
                                    "title": f"Run the migration guard {n}"}
                                   for n, i in enumerate(ids)])
            return "Always run the migration guard as part of the standard loop."

    monkeypatch.setattr(platform_svc, "resolve_chat", lambda db, pid: Resolved("openai", Chat()))
    return state


# ---- stage A: transcripts in, candidates out --------------------------------------------

def test_a_run_ingests_records_and_advances_the_watermark(db, transcripts):
    """The acceptance criterion, end to end: a manual invocation mines a transcript, records
    a candidate, and leaves a watermark behind so the next run knows where it got to."""
    _write(transcripts, "s.jsonl", [_line(LESSON)])

    out = learning_svc.run(db, stage="ingest", adapters=[_adapter(transcripts)])

    assert out["ingest"]["recorded"] == 1
    assert db.query(MemoryShard).filter(MemoryShard.source.like("transcript:%")).count() == 1
    assert db.query(IngestWatermark).count() == 1


def test_a_second_run_with_nothing_new_records_nothing(db, transcripts):
    """Re-running has to be free, or nobody schedules it. The watermark is what makes it so,
    and this is the test that would fail if a driver rebuilt the adapter's state each pass."""
    _write(transcripts, "s.jsonl", [_line(LESSON)])
    learning_svc.run(db, stage="ingest", adapters=[_adapter(transcripts)])

    out = learning_svc.run(db, stage="ingest", adapters=[_adapter(transcripts)])

    assert out["ingest"]["recorded"] == 0
    assert db.query(MemoryShard).filter(MemoryShard.source.like("transcript:%")).count() == 1


def test_ingest_records_candidates_never_published(db, transcripts):
    """THE boundary. The machine mines evidence; a human decides what is true. A driver that
    published its own findings would run a second lifecycle beside the review queue and make
    the queue decorative."""
    _write(transcripts, "s.jsonl", [_line(LESSON)])

    learning_svc.run(db, stage="ingest", adapters=[_adapter(transcripts)])

    rows = db.query(MemoryShard).filter(MemoryShard.source.like("transcript:%")).all()
    assert rows and all(r.status == "candidate" for r in rows)


def test_one_broken_adapter_does_not_end_the_run(db, transcripts):
    """A run over four harnesses must not lose three of them to the first one's bad day.

    The counter is reported separately from `skipped_sources` on purpose: "one file was
    unreadable" and "an entire harness never ran" are different facts, and folding them
    together lets a harness contribute nothing while the run still reports success.
    """
    _write(transcripts, "s.jsonl", [_line(LESSON)])

    class Broken:
        name = "broken"

        def discover(self):
            raise RuntimeError("transcript root exploded")

        def parse(self, source, watermark):  # pragma: no cover - never reached
            return [], None

    out = learning_svc.run(db, stage="ingest",
                           adapters=[Broken(), _adapter(transcripts)])

    assert out["ingest"]["failed_adapters"] == 1
    assert out["ingest"]["recorded"] == 1, "the healthy adapter still did its work"


# ---- stage B: published lessons in, drafted recommendations out --------------------------

def test_the_artifact_stage_ignores_untriaged_candidates(db, transcripts, model, proj):
    """The two stages sit either side of a human, and this is what proves it.

    Ingest writes `candidate`; classification reads `published`. A single-pass driver would
    pass every test above and still do nothing on a fresh install, because there would be
    nothing published on the first night. Asserting on the model call count rather than on
    the recommendation count is deliberate — it fails even if some future path created a
    recommendation without one.
    """
    _write(transcripts, "s.jsonl", [_line(LESSON)])

    out = learning_svc.run(db, stage="all", project_id=proj,
                           adapters=[_adapter(transcripts)])

    assert out["ingest"]["recorded"] == 1
    assert out["artifacts"]["classified"] == 0
    assert model["calls"] == 0, "nothing published, so nothing may reach a model"


def test_the_artifact_stage_classifies_and_drafts_what_a_human_published(db, model, proj):
    """Once a human triages, the second stage picks it up on its next pass — however long
    after the ingest that is. That lag is the design, not a shortcoming."""
    from app.services import memory as mem_svc

    mem_svc.add_memory(db, text_body=LESSON, project_id=proj, status="published",
                       source="transcript:claude-code:sess-1", auto_triage=False)

    out = learning_svc.run(db, stage="artifacts", project_id=proj)

    assert out["artifacts"]["classified"] == 1
    assert out["artifacts"]["drafted"] == 1
    rec = db.query(ArtifactRecommendation).one()
    assert rec.draft and rec.status == "queued", "drafted, and still awaiting a human"


def test_a_repeat_artifact_run_costs_no_model_calls(db, model, proj):
    """The acceptance criterion for the artifact stage, and the reason a nightly timer is
    affordable. Classification skips what it has already classified; drafting is keyed on a
    hash of the lesson text, so an unchanged set re-renders nothing."""
    from app.services import memory as mem_svc

    mem_svc.add_memory(db, text_body=LESSON, project_id=proj, status="published",
                       source="transcript:claude-code:sess-1", auto_triage=False)
    learning_svc.run(db, stage="artifacts", project_id=proj)
    spent = model["calls"]
    assert spent > 0, "the first pass must actually have used the model"

    out = learning_svc.run(db, stage="artifacts", project_id=proj)

    assert model["calls"] == spent, "a second pass over unchanged input must be free"
    assert out["artifacts"]["classified"] == 0
    assert out["artifacts"]["reused"] == 1 and out["artifacts"]["drafted"] == 0


def test_one_undraftable_recommendation_does_not_end_the_pass(db, model, proj, monkeypatch):
    """A queue of thirty must not lose twenty-nine drafts to one bad row.

    `draft` already swallows a failed MODEL call, which was enough while the only caller was
    a test. Everything else — a vanished lesson, a lost commit race — reached the caller, and
    under a scheduled driver that ends the pass.
    """
    from app.services import artifacts as art_svc
    from app.services import memory as mem_svc

    for i in range(2):
        mem_svc.add_memory(db, text_body=f"{LESSON} Variant {i}.", project_id=proj,
                           status="published", source=f"transcript:claude-code:sess-{i}",
                           auto_triage=False)
    art_svc.classify(db, proj)
    # `pending`, not every row: a superseded recommendation is history rather than queue, and
    # counting the table instead made this assert against rows drafting never visits.
    queued = art_svc.pending(db, proj)
    assert len(queued) >= 2, "need more than one queued row for 'the rest still drafted'"
    boom = {"first": True}
    real_draft = art_svc.draft

    def flaky(session, rec):
        if boom["first"]:
            boom["first"] = False
            raise RuntimeError("the row went away")
        return real_draft(session, rec)

    monkeypatch.setattr(art_svc, "draft", flaky)

    rows = art_svc.draft_pending(db, proj)

    assert len(rows) == len(queued) - 1, "the failing row is skipped, the rest still drafted"


# ---- the driver's own contract ------------------------------------------------------------

def test_an_unknown_stage_is_refused_rather_than_running_everything(db):
    """`stage=artifact` (singular) must not quietly ingest a 40k-line archive. A typo in a
    crontab is exactly how that would happen, and it would look like the job working."""
    with pytest.raises(learning_svc.UnknownStage):
        learning_svc.run(db, stage="artifact")


def test_cli_learn_stages_match_the_service(db):
    """The CLI spells its `--stage` choices out rather than importing `STAGES`, because
    building the parser happens on `--help` too and importing the service layer there drags
    in SQLAlchemy and the whole app. This is what stops the duplication drifting."""
    from app.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["learn", "run", "--stage", "ingest"])
    assert args.stage == "ingest"
    for stage in learning_svc.STAGES + ("all",):
        assert parser.parse_args(["learn", "run", "--stage", stage]).stage == stage
    with pytest.raises(SystemExit):
        parser.parse_args(["learn", "run", "--stage", "nope"])


def test_the_route_runs_the_loop_and_reports_counts(client, transcripts, monkeypatch):
    """The HTTP half of the driver, authenticated the way a scheduler actually can be.

    An API key rather than a session: the caller is cron, which cannot hold a 30-minute
    access token. Same reasoning as `/artifacts/{id}/used`.
    """
    _write(transcripts, "s.jsonl", [_line(LESSON)])
    monkeypatch.setenv("GRAPHBAN_CLAUDE_TRANSCRIPTS", str(transcripts))

    login = client.post("/api/auth/login",
                        json={"email": "alex@ascme-labs.com", "password": "graphban"})
    auth = {"Authorization": f"Bearer {login.json()['access_token']}"}
    key = client.post("/api/api-keys", json={"name": "scheduler", "project_id": "core"},
                      headers=auth)
    assert key.status_code in (200, 201), key.text
    raw = key.json()["plaintext"]

    r = client.post("/api/learning/run", json={"stage": "ingest"},
                    headers={"X-API-Key": raw})

    assert r.status_code == 200, r.text
    assert r.json()["ingest"]["recorded"] == 1


def test_the_route_refuses_an_anonymous_caller(client):
    """It creates rows and spends provider budget. An unauthenticated trigger is a way to
    bill somebody else's instance for as many runs as you like."""
    r = client.post("/api/learning/run", json={"stage": "ingest"})
    assert r.status_code == 401
