"""Golden-set evals for generative surfaces (GRPH-224).

The suite has to distinguish three answers: mechanical fail, judge fail, and
ungraded. Collapsing the last into a pass is the absence rule — a stub that
"passed 4 evals" has judged nothing.

Several tests sabotage the CALL: a runner that never invokes `extract_lessons`
must not green `must_not_contain` by returning empty, and a stub judge must not
report `judge_passed`.
"""
import json
from pathlib import Path

import pytest

from app.cli import build_parser
from app.services import evals as evals_svc
from app.services import insights as insights_svc
from app.services.platform import Resolved


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _chat(replies):
    class Chat:
        def __init__(self):
            self.n = 0

        def chat(self, **kwargs):
            i = min(self.n, len(replies) - 1)
            self.n += 1
            return replies[i]

    return Chat()


def _verdict(grounded=True, **over):
    body = {"grounded": grounded, "relevant": True, "format_ok": True,
            "reason": "ok" if grounded else "states a forbidden claim"}
    body.update(over)
    return json.dumps(body)


# ---- loading -----------------------------------------------------------------

def test_a_missing_cases_dir_is_absent_not_ok(db, tmp_path):
    """Deleting the fixtures must not green the job. `[]` would be the clean reading."""
    report = evals_svc.run(db, root=tmp_path / "nope")
    assert report["status"] == "absent"
    assert report["cases"] == 0
    assert report["graded"] is False


def test_an_empty_cases_dir_is_absent_not_ok(db, tmp_path):
    (tmp_path / "extract_lessons").mkdir()
    report = evals_svc.run(db, root=tmp_path)
    assert report["status"] == "absent"


def test_unknown_surface_is_refused(db):
    with pytest.raises(evals_svc.UnknownSurface):
        evals_svc.run(db, surface="extract_lesson")


def test_cli_surfaces_match_the_service():
    """`--help` must not import the service layer, so the choices are spelled out."""
    parser = build_parser()
    for name in evals_svc.SURFACES:
        assert parser.parse_args(["eval", "--surface", name]).surface == name
    assert parser.parse_args(["eval", "--surface", "all"]).surface == "all"
    with pytest.raises(SystemExit):
        parser.parse_args(["eval", "--surface", "nope"])


# ---- mechanical golden set (the CALL) ----------------------------------------

def test_the_packaged_cases_hold_mechanically(db):
    """The shipped extract_lessons fixtures, through extract_lessons, not a helper."""
    report = evals_svc.run(db, surface="extract_lessons", judge=False)
    assert report["status"] == "ok", json.dumps(report["results"], indent=2, default=str)
    assert report["cases"] == 4
    assert report["mechanical_failed"] == 0
    assert report["judge_passed"] == 0
    assert report["judge_ungraded"] == 4
    assert report["graded"] is False


def test_stale_proposal_is_not_extracted(db):
    """GRPH-358: the labelled-stale proposal must not become a lesson."""
    cases = evals_svc.load_cases(surface="extract_lessons")
    case = next(c for c in cases if c["id"] == "stale-proposal")
    result = evals_svc.run_case(db, case, judge=False)
    assert result["invoked"] is True
    assert result["mechanical"]["passed"], result["mechanical"]
    blob = " ".join(result["outputs"]).lower()
    assert "inherit the existing measurable" not in blob


def test_prefer_outcome_extracts_the_evidence_not_the_plan(db):
    cases = evals_svc.load_cases(surface="extract_lessons")
    case = next(c for c in cases if c["id"] == "prefer-outcome")
    result = evals_svc.run_case(db, case, judge=False)
    assert result["mechanical"]["passed"], result["mechanical"]
    blob = " ".join(result["outputs"]).lower()
    assert "never infer main" in blob
    assert "default base_branch to main" not in blob


def test_prompt_injection_does_not_land_the_secret(db):
    cases = evals_svc.load_cases(surface="extract_lessons")
    case = next(c for c in cases if c["id"] == "prompt-injection")
    result = evals_svc.run_case(db, case, judge=False)
    assert result["mechanical"]["passed"], result["mechanical"]
    assert "hunter2" not in " ".join(result["outputs"]).lower()


def test_the_runner_invokes_extract_lessons(db, monkeypatch):
    """Empty output would pass every must_not_contain. The CALL has to happen."""
    called = {"n": 0}
    real = insights_svc.extract_lessons

    def wrapped(db, item_id):
        called["n"] += 1
        return real(db, item_id)

    monkeypatch.setattr(evals_svc.insights_svc, "extract_lessons", wrapped)
    cases = evals_svc.load_cases(surface="extract_lessons")
    evals_svc.run_case(db, next(c for c in cases if c["id"] == "stale-proposal"))
    assert called["n"] == 1


def test_a_silent_extractor_fails_min_shards(db, monkeypatch):
    monkeypatch.setattr(evals_svc.insights_svc, "extract_lessons", lambda db, item_id: [])
    cases = evals_svc.load_cases(surface="extract_lessons")
    result = evals_svc.run_case(db, next(c for c in cases if c["id"] == "stale-proposal"))
    assert result["outcome"] == "fail"
    assert result["mechanical"]["passed"] is False


def test_extracting_the_stale_proposal_fails_the_case(db, monkeypatch):
    def poison(db, item_id):
        from app.services import memory as mem_svc
        from app.services import items as items_svc

        item = items_svc.get_item(db, item_id)
        mem_svc.add_memory(
            db, text_body="We decided discovered artifacts inherit the existing MEASURABLE_TIERS logic",
            scope="item", source=f"lesson from {item.id}", item_id=item.id,
            project_id=item.project_id, status="candidate", origin="agent:auto-extract",
        )
        return []

    monkeypatch.setattr(evals_svc.insights_svc, "extract_lessons", poison)
    cases = evals_svc.load_cases(surface="extract_lessons")
    result = evals_svc.run_case(db, next(c for c in cases if c["id"] == "stale-proposal"))
    assert result["outcome"] == "fail"
    assert any("forbidden" in f for f in result["mechanical"]["failures"])


def test_an_unknown_surface_case_is_ungraded_not_ok(db):
    result = evals_svc.run_case(db, {"id": "x", "surface": "assistant", "expect": {}})
    assert result["invoked"] is False
    assert result["outcome"] == "ungraded"
    assert result["graded"] is False


# ---- judge: ungraded vs fail vs pass -----------------------------------------

def test_stub_judge_is_ungraded_not_a_pass(db):
    report = evals_svc.run(db, surface="extract_lessons", judge=True)
    assert report["status"] == "ok"
    assert report["judge_passed"] == 0
    assert report["judge_failed"] == 0
    assert report["judge_ungraded"] == report["cases"]
    assert report["graded"] is False
    assert all(r["judge"]["reason"] == "stub cannot judge substance" for r in report["results"])


def test_judge_not_requested_is_ungraded(db):
    cases = evals_svc.load_cases(surface="extract_lessons")
    result = evals_svc.run_case(db, cases[0], judge=False)
    assert result["judge_outcome"] == "ungraded"
    assert result["graded"] is False
    assert result["judge"]["reason"] == "judge not requested"


def test_a_grounded_judge_passes(db, monkeypatch):
    monkeypatch.setattr(
        evals_svc.platform_svc, "resolve_chat",
        lambda db, pid: Resolved("ollama", _chat([_verdict(True)] * 3)),
    )
    cases = evals_svc.load_cases(surface="extract_lessons")
    result = evals_svc.run_case(
        db, next(c for c in cases if c["id"] == "stale-proposal"), judge=True)
    assert result["judge_outcome"] == "pass"
    assert result["graded"] is True
    assert result["outcome"] == "ok"


def test_an_ungrounded_judge_fails_the_case(db, monkeypatch):
    monkeypatch.setattr(
        evals_svc.platform_svc, "resolve_chat",
        lambda db, pid: Resolved("ollama", _chat([_verdict(False)] * 3)),
    )
    cases = evals_svc.load_cases(surface="extract_lessons")
    result = evals_svc.run_case(
        db, next(c for c in cases if c["id"] == "stale-proposal"), judge=True)
    assert result["judge_outcome"] == "fail"
    assert result["outcome"] == "fail"
    assert result["graded"] is True


def test_a_split_judge_is_ungraded(db, monkeypatch):
    """Unanimity, not majority. A coin flip is not a verdict (GRPH-348)."""
    replies = [_verdict(True), _verdict(False), _verdict(True)]
    monkeypatch.setattr(
        evals_svc.platform_svc, "resolve_chat",
        lambda db, pid: Resolved("ollama", _chat(replies)),
    )
    cases = evals_svc.load_cases(surface="extract_lessons")
    result = evals_svc.run_case(
        db, next(c for c in cases if c["id"] == "stale-proposal"), judge=True)
    assert result["judge_outcome"] == "ungraded"
    assert "agree" in result["judge"]["reason"]
    assert result["graded"] is False
    assert result["outcome"] == "ok"  # mechanical held; the judge had no answer


def test_an_unparseable_judge_is_ungraded(db, monkeypatch):
    monkeypatch.setattr(
        evals_svc.platform_svc, "resolve_chat",
        lambda db, pid: Resolved("ollama", _chat(["not json at all"])),
    )
    cases = evals_svc.load_cases(surface="extract_lessons")
    result = evals_svc.run_case(
        db, next(c for c in cases if c["id"] == "stale-proposal"), judge=True)
    assert result["judge_outcome"] == "ungraded"
    assert result["graded"] is False


def test_the_judge_is_actually_asked(db, monkeypatch):
    """A runner that stamps pass without calling chat would green `--judge` on nothing."""
    asked = {"n": 0}

    class Chat:
        def chat(self, **kwargs):
            asked["n"] += 1
            return _verdict(True)

    monkeypatch.setattr(
        evals_svc.platform_svc, "resolve_chat",
        lambda db, pid: Resolved("ollama", Chat()),
    )
    cases = evals_svc.load_cases(surface="extract_lessons")
    evals_svc.run_case(db, next(c for c in cases if c["id"] == "stale-proposal"), judge=True)
    assert asked["n"] == evals_svc.JUDGE_SAMPLES


# ---- stub extractor + CLI ----------------------------------------------------

def test_stub_extractor_skips_the_labelled_proposal():
    from app.providers.stub import StubExtractor

    doc = (
        "WHAT ACTUALLY HAPPENED (authoritative — prefer this where the two disagree):\n"
        "- test: a discovered artifact is never measurable\n\n"
        "ORIGINAL PROPOSAL (written BEFORE the work; the build may have revised or reversed "
        "it — do not state anything from here as settled fact):\n"
        "We decided discovered artifacts inherit the existing MEASURABLE_TIERS logic."
    )
    out = StubExtractor().extract(title="x", description=doc)
    blob = " ".join(out).lower()
    assert "inherit the existing measurable" not in blob
    assert out == ["Completed: x."]


def test_stub_extractor_still_pulls_markers_when_unwrapped():
    from app.providers.stub import StubExtractor

    out = StubExtractor().extract(
        title="HTTP",
        description="We decided to always set a timeout on outbound HTTP.",
    )
    assert any("timeout" in s.lower() for s in out)


def test_stub_extractor_keeps_outcome_bullets_past_the_heading():
    """The heading and the first bullet are one 'sentence' if we split on `. ` first."""
    from app.providers.stub import StubExtractor

    doc = (
        "WHAT ACTUALLY HAPPENED (authoritative — prefer this where the two disagree):\n"
        "- note: Learning: never infer main from GitHub when gitops is unmeasured.\n\n"
        "ORIGINAL PROPOSAL (written BEFORE the work; the build may have revised or reversed "
        "it — do not state anything from here as settled fact):\n"
        "We decided to default base_branch to main when the form is empty."
    )
    out = StubExtractor().extract(title="gitops", description=doc)
    blob = " ".join(out).lower()
    assert "never infer main" in blob
    assert "default base_branch to main" not in blob


def test_cli_eval_exit_codes(db, monkeypatch, capsys):
    monkeypatch.setattr("app.cli._session", lambda: db)
    assert evals_svc.run  # imported path
    from app import cli

    assert cli.main(["eval", "--surface", "extract_lessons"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["graded"] is False


def test_cli_eval_absent_is_exit_2(db, monkeypatch, tmp_path, capsys):
    from app import cli

    monkeypatch.setattr("app.cli._session", lambda: db)
    empty = tmp_path / "empty"
    empty.mkdir()
    assert cli.main(["eval", "--dir", str(empty)]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "absent"


def test_load_cases_sees_the_packaged_tree():
    cases = evals_svc.load_cases()
    assert cases is not None
    by_surface = {}
    for c in cases:
        by_surface.setdefault(c["surface"], set()).add(c["id"])
    assert by_surface["extract_lessons"] == {
        "stale-proposal", "prefer-outcome", "prompt-injection", "thin-description",
    }
    assert by_surface["grill_prd"] == {
        "asks-dimensions", "prompt-injection", "classify-zero-answers", "deferred-stays",
    }
    assert all(Path(c["_path"]).is_file() for c in cases)


# ---- grill_prd (GRPH-642) ----------------------------------------------------

def _grill(cid):
    cases = evals_svc.load_cases(surface="grill_prd")
    return next(c for c in cases if c["id"] == cid)


def test_the_grill_cases_hold_mechanically(db):
    report = evals_svc.run(db, surface="grill_prd", judge=False)
    assert report["status"] == "ok", json.dumps(report["results"], indent=2, default=str)
    assert report["cases"] == 4
    assert report["mechanical_failed"] == 0
    assert report["judge_ungraded"] == 4
    assert report["graded"] is False


def test_grill_questions_name_the_four_dimensions(db):
    result = evals_svc.run_case(db, _grill("asks-dimensions"))
    assert result["mechanical"]["passed"], result["mechanical"]
    assert result["output_count"] >= 4
    blob = " ".join(result["outputs"]).lower()
    assert "out of scope" in blob
    assert result["outputs"]  # CALL produced text


def test_grill_questions_do_not_complete_the_prd(db):
    """A questions eval that also classified dummy answers would approve the fixture."""
    result = evals_svc.run_case(db, _grill("asks-dimensions"))
    extras = result  # mechanical already pinned grill_answers=0 and complete=false
    assert extras["mechanical"]["passed"], extras["mechanical"]


def test_grill_prompt_injection_does_not_land_the_secret(db):
    result = evals_svc.run_case(db, _grill("prompt-injection"))
    assert result["mechanical"]["passed"], result["mechanical"]
    assert "hunter2" not in " ".join(result["outputs"]).lower()


def test_classify_with_no_answers_is_not_complete(db):
    result = evals_svc.run_case(db, _grill("classify-zero-answers"))
    assert result["mechanical"]["passed"], result["mechanical"]


def test_deferred_stays_deferred_through_classify(db):
    result = evals_svc.run_case(db, _grill("deferred-stays"))
    assert result["mechanical"]["passed"], result["mechanical"]


def test_the_questions_runner_invokes_ai_command(db, monkeypatch):
    called = {"n": 0}
    real = evals_svc.prd_svc.ai_command_detail

    def wrapped(db, prd_id, command):
        called["n"] += 1
        return real(db, prd_id, command)

    monkeypatch.setattr(evals_svc.prd_svc, "ai_command_detail", wrapped)
    evals_svc.run_case(db, _grill("asks-dimensions"))
    assert called["n"] == 1


def test_the_classify_runner_invokes_classify_grill(db, monkeypatch):
    called = {"n": 0}
    real = evals_svc.prd_svc.classify_grill

    def wrapped(db, prd):
        called["n"] += 1
        return real(db, prd)

    monkeypatch.setattr(evals_svc.prd_svc, "classify_grill", wrapped)
    evals_svc.run_case(db, _grill("classify-zero-answers"))
    assert called["n"] == 1


def test_a_silent_questions_runner_fails_min_shards(db, monkeypatch):
    monkeypatch.setattr(
        evals_svc.prd_svc, "ai_command_detail", lambda db, prd_id, command: ("", False))
    result = evals_svc.run_case(db, _grill("asks-dimensions"))
    assert result["outcome"] == "fail"
    assert result["mechanical"]["passed"] is False


def test_an_unreachable_grader_is_ungraded_not_a_pass(db, monkeypatch):
    """GRPH-485: a real provider that cannot be asked is graded=false, not the stub bar."""

    class Boom:
        def chat(self, **kwargs):
            raise RuntimeError("grader down")

    monkeypatch.setattr(
        evals_svc.prd_svc.platform_svc, "resolve_chat",
        lambda db, pid: Resolved("ollama", Boom()),
    )
    case = {
        "id": "grader-down",
        "surface": "grill_prd",
        "action": "classify",
        "input": {
            "title": "[eval] grader down",
            "body": "# X\n\n## Problem\n\nNeeds an answer so classify has something to grade.\n",
            "answers": ["Gitflow is out of scope."],
        },
        "expect": {
            "min_shards": 1,
            "grill_graded": False,
            "ungraded_reason_contains": "could not be asked",
        },
    }
    result = evals_svc.run_case(db, case)
    assert result["mechanical"]["passed"], result["mechanical"]
    assert result["outcome"] == "ok"


def test_deferred_is_not_overwritten_when_the_classifier_says_resolved(db, monkeypatch):
    """The CALL is classify_grill's guard, not set_dimension."""

    def all_resolved(db, prd, history):
        return {name: {"outcome": "resolved", "note": "forced", "answered_by": 1}
                for name in evals_svc.prd_svc.DIMENSIONS}

    monkeypatch.setattr(evals_svc.prd_svc, "_classify_dimensions", all_resolved)
    # Non-stub so classify uses the mock instead of the stub bar.
    monkeypatch.setattr(
        evals_svc.prd_svc, "_grader_id", lambda db, prd: "ollama")
    result = evals_svc.run_case(db, _grill("deferred-stays"))
    assert result["mechanical"]["passed"], result["mechanical"]


def test_questions_eval_does_not_record_answers(db, monkeypatch):
    """Sabotage the CALL: recording answers during questions would let complete go true."""
    recorded = {"n": 0}
    real = evals_svc.prd_svc.record_grill_turns

    def wrapped(*a, **k):
        recorded["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(evals_svc.prd_svc, "record_grill_turns", wrapped)
    evals_svc.run_case(db, _grill("asks-dimensions"))
    assert recorded["n"] == 0
