"""Metric functions for the decider calibration script (GRPH-894).

These are pure functions — no database, no HTTP — so they test the math that
the calibration report rests on. A wrong AUC or a wrong threshold would make
the pass bar meaningless, which is the whole point of S0.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

# Load the script as a module (it lives outside the package tree).
# Must register in sys.modules BEFORE exec_module so dataclass can resolve __module__.
_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "decider_calibrate.py"
spec = importlib.util.spec_from_file_location("decider_calibrate", _SCRIPT)
assert spec and spec.loader
cal = importlib.util.module_from_spec(spec)
sys.modules["decider_calibrate"] = cal
os.environ.setdefault("DATABASE_URL", "sqlite:///test.db")
spec.loader.exec_module(cal)


class TestRocAuc:
    def test_perfect_separator(self):
        labels = [True, True, True, False, False, False]
        scores = [0.9, 0.8, 0.7, 0.3, 0.2, 0.1]
        assert cal._roc_auc(labels, scores) == 1.0

    def test_random_is_half(self):
        # Interleaved scores → AUC ≈ 0.5
        labels = [True, False, True, False, True, False]
        scores = [0.6, 0.5, 0.4, 0.3, 0.2, 0.1]
        auc = cal._roc_auc(labels, scores)
        assert auc is not None
        assert 0.3 < auc < 0.8

    def test_inverted(self):
        labels = [True, True, False, False]
        scores = [0.1, 0.2, 0.8, 0.9]
        auc = cal._roc_auc(labels, scores)
        assert auc is not None
        assert auc < 0.5

    def test_all_one_class_returns_none(self):
        assert cal._roc_auc([True, True], [0.9, 0.8]) is None
        assert cal._roc_auc([False, False], [0.1, 0.2]) is None

    def test_too_few_returns_none(self):
        assert cal._roc_auc([True], [0.9]) is None


class TestCohensKappa:
    def test_perfect_agreement(self):
        a = [True, True, False, False]
        b = [True, True, False, False]
        assert cal._cohens_kappa(a, b) == 1.0

    def test_chance_agreement(self):
        # 50/50 split, independent → kappa ≈ 0
        a = [True, True, False, False]
        b = [False, False, True, True]
        kappa = cal._cohens_kappa(a, b)
        assert kappa is not None
        assert kappa < 0

    def test_constant_rater_returns_none(self):
        # Both raters always say True → pe = 1.0 → kappa undefined.
        assert cal._cohens_kappa([True, True], [True, True]) is None


class TestKeepThreshold:
    def test_basic_threshold(self):
        labels = [True] * 50 + [False] * 50
        scores = [0.8] * 50 + [0.2] * 50
        thr, low, high = cal._keep_threshold_at_fpr(labels, scores, 0.02)
        assert thr is not None
        # With 2% FA on 50 negatives (1 allowed), threshold sits just above the
        # negative cluster at 0.2 — the function finds where negatives stop.
        assert 0.19 < thr < 0.81

    def test_too_few_returns_none(self):
        thr, _, _ = cal._keep_threshold_at_fpr([True, False], [0.9, 0.1], 0.02)
        assert thr is None


class TestQualityAtPrecision:
    def test_high_quality_is_published(self):
        labels = [True, True, True, False, False]
        qualities = [0.9, 0.85, 0.8, 0.3, 0.2]
        result = cal._quality_at_precision(labels, qualities, 0.90)
        assert result is not None
        assert result >= 0.8

    def test_too_few_returns_none(self):
        assert cal._quality_at_precision([True], [0.9], 0.90) is None


class TestHoldoutIdFilter:
    """--shard-ids-file must not silently fall back to the full DB (S0 leak)."""

    def test_json_holdout_ids(self, tmp_path):
        p = tmp_path / "manifest.json"
        p.write_text(json.dumps({"holdout_ids": ["m_a", "m_b"], "seed": 1}))
        assert cal._load_id_set(str(p)) == {"m_a", "m_b"}

    def test_text_lines(self, tmp_path):
        p = tmp_path / "ids.txt"
        p.write_text("# comment\nm_a\n\nm_b\n")
        assert cal._load_id_set(str(p)) == {"m_a", "m_b"}

    def test_filter_keeps_only_named(self):
        samples = [
            cal.ShardSample("m_a", "t", True, None, None, True),
            cal.ShardSample("m_b", "t", False, None, None, False),
            cal.ShardSample("m_c", "t", True, None, None, True),
        ]
        out = cal._filter_samples(samples, {"m_b", "m_z"})
        assert [s.shard_id for s in out] == ["m_b"]

    def test_empty_id_set_does_not_return_all(self):
        samples = [cal.ShardSample("m_a", "t", True, None, None, True)]
        assert cal._filter_samples(samples, set()) == []


class TestShardLoading:
    """The loader functions need a database; test they handle empty gracefully."""

    def test_load_labelled_empty(self, db):
        samples = cal._load_labelled_shards(db)
        assert samples == []

    def test_load_scored_empty(self, db):
        samples = cal._load_scored_shards(db)
        assert samples == []

    def test_load_judged_empty(self, db):
        samples = cal._load_judged_shards(db)
        assert samples == []


@pytest.fixture()
def db(_clean_database):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


class TestWire:
    """The request is TypeSafe's shape and the reply is read from `answers` (2026-09-23).

    The first run against laya was 1,626 × 400 Bad Request: the body was `state.answer_space`,
    a shape from no protocol, and the per-shard `except` turned that into a clean-looking
    report with `n_labelled=0` on every head. Sabotage: send `answer_space` again; the first
    test fails on the missing `questions`."""

    def test_the_body_declares_the_two_questions(self):
        body = cal._request_body("multilingual", "Always run pnpm install --frozen-lockfile")
        assert body["model"] == "multilingual"
        assert body["state"]["note"] == "Always run pnpm install --frozen-lockfile"
        q = body["questions"]
        assert q["keep"]["type"] == "noul" and set(q["keep"]["criteria"]) == {"true", "false"}
        assert q["quality"]["type"] == "score" and len(q["quality"]["criteria"]) == 5
        assert "answer_space" not in body["state"]

    def test_a_laya_reply_is_read_from_answers(self):
        keep, quality = cal._parse_answers({
            "model": "laya-rl-agent",
            "answers": {"keep": {"type": "noul", "noul": 0.708, "confidence": 0.5},
                        "quality": {"type": "score", "score": 2.63,
                                    "probabilities": {"0": 0.1, "1": 0.2, "2": 0.4, "3": 0.2, "4": 0.1}}},
            "usage": {"input_tokens": 223, "output_tokens": 0},
        })
        assert keep == 0.708
        assert abs(quality - 2.63 / 4) < 1e-9

    def test_a_reply_without_answers_is_an_error_not_a_coin_flip(self):
        with pytest.raises(ValueError):
            cal._parse_answers({"detail": "request body must be an object with a questions field"})
        with pytest.raises(ValueError):
            cal._parse_answers({"answers": {"keep": {"type": "noul"}, "quality": {"score": 1}}})


class TestReviewPass:
    def test_review_body_declares_the_three_questions(self):
        sample = cal.ReviewSample(
            shard_id="m_cand",
            candidate_text="Always heartbeat while you work",
            published=[("m_pub", "Claim before you start")],
            label_choice="none",
        )
        body = cal._review_request_body("typed-decisions", sample)
        assert body["model"] == "typed-decisions"
        assert "CANDIDATE:" in body["state"]
        assert "[m_pub]" in body["state"]
        q = body["questions"]
        assert q["grounded"]["type"] == "noul"
        assert q["ready"]["type"] == "noul"
        assert q["contradicts"]["type"] == "choice"
        assert set(q["contradicts"]["criteria"]) == {"none", "m_pub"}

    def test_a_laya_review_reply_is_parsed(self):
        decision = cal._parse_review_answers({
            "answers": {
                "grounded": {"type": "noul", "noul": 0.91},
                "ready": {"type": "noul", "noul": 0.88},
                "contradicts": {"type": "choice", "choice": "m_pub", "confidence": 0.82},
            },
        })
        assert decision.grounded_p == 0.91
        assert decision.contradicts == "m_pub"
        assert decision.contradicts_conf == 0.82

    def test_choice_min_suppresses_low_confidence_conflicts(self):
        low = cal.ReviewDecision(0.9, 0.9, "m_pub", 0.55, 1.0)
        high = cal.ReviewDecision(0.9, 0.9, "m_pub", 0.82, 1.0)
        assert cal._effective_choice(low, choice_min=0.70) == "none"
        assert cal._effective_choice(high, choice_min=0.70) == "m_pub"

    def test_choice_min_at_precision_finds_a_floor(self):
        labels = ["m_a", "m_a", "m_a", "m_b", "none"]
        choices = ["m_a", "m_a", "m_a", "m_b", "m_a"]
        confidences = [0.95, 0.92, 0.88, 0.85, 0.70]
        thr = cal._choice_min_at_precision(labels, choices, confidences, 0.90)
        assert thr is not None
        assert thr >= 0.85

    def test_review_loader_keeps_only_resolvable_labels(self, db):
        from app.models import MemoryShard

        db.add_all([
            MemoryShard(id="m_pub", project_id=None, text="published rule", status="published"),
            MemoryShard(
                id="m_ok", project_id=None, text="candidate", status="candidate",
                review_judge_verdict={"grounded": False, "ready": True,
                                      "conflicts": ["m_pub"]},
            ),
            MemoryShard(
                id="m_quote", project_id=None, text="other", status="candidate",
                review_judge_verdict={"grounded": False, "ready": True,
                                      "conflicts": ["published: no timeouts"]},
            ),
        ])
        db.commit()
        got = {s.shard_id for s in cal._load_review_samples(db)}
        assert got == {"m_ok"}


class TestLabelledCorpus:
    def test_a_human_decision_counts_and_a_scored_one_does_not(self, db):
        from app.models import MemoryShard
        db.add_all([
            MemoryShard(id="m_human_pub", project_id=None, text="a rule", status="published",
                        scoring_source=None, origin="agent:gb-cc"),
            MemoryShard(id="m_human_rej", project_id=None, text="noise", status="rejected",
                        scoring_source="", origin="ingest:claude-code:transient"),
            MemoryShard(id="m_llm", project_id=None, text="judged", status="published",
                        scoring_source="llm", origin="agent:auto-extract"),
            MemoryShard(id="m_cand", project_id=None, text="undecided", status="candidate",
                        scoring_source=None, origin="agent:gb-cc"),
        ])
        db.commit()
        got = {s.shard_id: s for s in cal._load_labelled_shards(db)}
        assert set(got) == {"m_human_pub", "m_human_rej"}, set(got)
        assert got["m_human_pub"].label_keep is True and got["m_human_pub"].human_published
        assert got["m_human_rej"].label_keep is False
        scored = {s.shard_id for s in cal._load_scored_shards(db)}
        assert scored == {"m_llm"}
