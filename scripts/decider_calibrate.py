#!/usr/bin/env python3
"""Calibrate a decider credential against Graphban's own labelled shards (GRPH-894).

S0 — the gate that must pass before any decider threshold moves a shard.

Two corpora, both already in the database:

1. **Labelled shards** — every MemoryShard whose `origin` is the eval-sample origin
   (human-labelled keep/reject via Memory review), plus every shard whose
   `scoring_source` is ``llm`` or ``agent`` (the chat judge already decided keep/reject
   and recorded the outcome as the shard's status).

2. **Judged shards** — every shard with a stored ``review_judge_verdict`` (the chat
   judge's groundedness/readiness pass), to measure agreement with the incumbent.

For each head the decider exposes, the script reports:

- ROC-AUC on ``keep`` against the human label
- Cohen's kappa agreement with the chat judge on the judged corpus
- The ``keep`` threshold pair that yields ≤ 2 % false-accept on the labelled set,
  and the abstain band that implies
- The ``quality`` level at which precision on "human published" crosses 90 %
- p50 / p95 latency per call

Pass bar: AUC ≥ 0.85 on ≥ 60 labelled shards, on at least one head. Below that the
decider is not wired to adjudication and S3 does not ship.

Usage::

    # Auto-discover heads from /health, use the default laya endpoint:
    cd backend && python ../scripts/decider_calibrate.py

    # Explicit endpoint and heads:
    cd backend && python ../scripts/decider_calibrate.py \\
        --endpoint http://ms-s1-ubt:8090 \\
        --heads english typed-decisions multilingual

    # JSON output for piping:
    cd backend && python ../scripts/decider_calibrate.py --json

    # Review-pass `contradicts` calibration (needs conflict-labelled shards):
    cd backend && python ../scripts/decider_calibrate.py --review-pass --json
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# Ensure the backend package is importable when running from the repo root.
_BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import httpx

logger = logging.getLogger("graphban.calibrate")

# The memory judge's own instructions — the same prompt the chat judge answers, so the
# decider is measured against the same question. Imported from the service so the two
# cannot drift apart.
from app.services.memory import _JUDGE_SYSTEM, _JUDGE_QUESTION  # noqa: E402

# Eval-sample origin, matching evals.SAMPLE_ORIGIN so a rename in one place breaks this.
_SAMPLE_ORIGIN = "agent:eval-sample"

# Known heads from the PRD's measurement. The script auto-disovers from /health when
# possible; this list is the fallback for endpoints that do not implement /health.
_DEFAULT_HEADS = ("english", "typed-decisions", "multilingual")

# The pass bar from the PRD. A head that does not clear this is not a judge.
_AUC_PASS = 0.85
_MIN_LABELLED = 60
_FALSE_ACCEPT_MAX = 0.02
_QUALITY_PRECISION_MIN = 0.90


@dataclass
class ShardSample:
    """One shard presented to the decider."""
    shard_id: str
    text: str
    label_keep: bool | None  # None = no human label available
    chat_judge_keep: bool | None  # None = no stored review verdict
    chat_judge_quality: float | None
    human_published: bool  # True when a person published this (status=published, no scorer)


@dataclass
class HeadDecision:
    """What one head said about one shard."""
    keep_prob: float
    quality: float
    latency_ms: float


@dataclass
class HeadReport:
    """Calibration metrics for one head."""
    head: str
    n_labelled: int = 0
    n_judged: int = 0
    auc: float | None = None
    kappa: float | None = None
    keep_threshold: float | None = None
    abstain_low: float | None = None
    abstain_high: float | None = None
    quality_at_90_precision: float | None = None
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    pass_keep: bool = False
    pass_quality: bool = False
    decisions: list[HeadDecision] = field(default_factory=list)


@dataclass
class ReviewSample:
    """One candidate shard for the review-pass `contradicts` calibration."""
    shard_id: str
    candidate_text: str
    published: list[tuple[str, str]]  # (id, text), top-k by project
    label_choice: str  # published shard id, or "none"


@dataclass
class ReviewDecision:
    """What one head said on the review pass."""
    grounded_p: float
    ready_p: float
    contradicts: str
    contradicts_conf: float | None
    latency_ms: float


@dataclass
class ReviewReport:
    """Calibration metrics for the review-pass `contradicts` question."""
    head: str
    n_labelled: int = 0
    choice_accuracy: float | None = None
    choice_min_at_90_precision: float | None = None
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    measured: bool = False
    decisions: list[ReviewDecision] = field(default_factory=list)


def _discover_heads(endpoint: str, client: httpx.Client) -> list[str]:
    """Read loaded models from /health. Returns [] when the endpoint does not answer."""
    try:
        r = client.get(f"{endpoint}/health", timeout=5.0)
        r.raise_for_status()
        data = r.json()
        loaded = data.get("loaded") or []
        if isinstance(loaded, list) and loaded:
            return [str(m) for m in loaded]
    except Exception:
        pass
    return []


#: PRD-45 D6, verbatim: the write-path judge's two questions. The decider is measured on
#: the same question the chat judge answers (`_JUDGE_SYSTEM` is carried in the state so the
#: instruction it was written for travels with the text), but the ANSWER SPACE is declared
#: here, which is the whole difference between a decider and a chat model.
_QUALITY_LEVELS = ("useless", "weak", "fair", "good", "excellent")
_QUESTIONS = {
    "keep": {
        "type": "noul",
        "instructions": "The note is a durable, specific, actionable fact, decision or "
                        "convention a future agent working on this project can act on.",
        "criteria": {
            "true": "a rule, gotcha or decision with enough detail to act on",
            "false": "vague, transient, obvious, redundant, or a status update",
        },
    },
    "quality": {
        "type": "score",
        "instructions": "How useful this note is to a future agent on this project.",
        "criteria": list(_QUALITY_LEVELS),
    },
}


def _request_body(head: str, text: str) -> dict:
    """The System One request for one shard: TypeSafe's `{model, state, questions}`.

    The first version of this script sent `state.answer_space` — a shape that exists in no
    protocol — and every one of 1,626 calls on the first real run was a 400, which the
    per-shard `except` turned into an empty report with `n_labelled=0` on every head. The
    body is a function so a test can assert its shape without an endpoint.
    """
    return {
        "model": head,
        "state": {"instructions": _JUDGE_SYSTEM, "note": text, "question": _JUDGE_QUESTION},
        "questions": _QUESTIONS,
    }


def _parse_answers(data: dict) -> tuple[float, float]:
    """`(keep_prob, quality)` from a System One reply.

    `answers.keep.noul` is P(keep). `answers.quality.score` lands on the 0..4 level scale
    (fractional between levels) and is normalised to 0..1 here so the rest of the report
    can compare it with the chat judge's 0..1 quality. Missing or malformed answers raise:
    a shard the endpoint did not actually judge must not be counted at 0.5.
    """
    answers = data.get("answers")
    if not isinstance(answers, dict):
        raise ValueError(f"no answers in reply: {str(data)[:120]}")
    keep = answers.get("keep") or {}
    quality = answers.get("quality") or {}
    if not isinstance(keep.get("noul"), (int, float)):
        raise ValueError(f"keep is not a noul: {keep}")
    if not isinstance(quality.get("score"), (int, float)):
        raise ValueError(f"quality is not a score: {quality}")
    top = len(_QUALITY_LEVELS) - 1
    q = min(max(float(quality["score"]) / top, 0.0), 1.0)
    return float(keep["noul"]), q


def _review_state(sample: ReviewSample) -> str:
    """PRD-45 D6 review-pass state — mirrors `memory._decider_review_state`."""
    parts = [f"CANDIDATE:\n{sample.candidate_text.strip() or '(empty)'}"]
    if sample.published:
        numbered = "\n".join(
            f"{i}. [{sid}] {text}" for i, (sid, text) in enumerate(sample.published, 1)
        )
        parts.append("PUBLISHED MEMORY (trusted):\n" + numbered)
    else:
        parts.append("PUBLISHED MEMORY: none — there is no trusted memory to contradict.")
    return "\n\n".join(parts)


def _review_questions(published: list[tuple[str, str]]) -> dict:
    """PRD-45 D6 review-pass questions — mirrors `memory._decider_review_questions`."""
    opts: dict[str, str] = {"none": "does not contradict any published note"}
    for sid, _text in published:
        opts[sid] = f"published shard {sid}"
    return {
        "grounded": {
            "type": "noul",
            "instructions": (
                "The candidate is consistent with the numbered published memory; "
                "it does not contradict any of it."
            ),
            "criteria": {
                "true": "consistent with the published notes",
                "false": "contradicts a published note",
            },
        },
        "ready": {
            "type": "noul",
            "instructions": "Specific, durable and non-trivial enough to publish as-is.",
            "criteria": {
                "true": "specific, durable and non-trivial enough to publish as-is",
                "false": "vague, transient, or not ready to publish",
            },
        },
        "contradicts": {
            "type": "choice",
            "instructions": "Which published note, if any, the candidate contradicts.",
            "criteria": opts,
        },
    }


def _review_request_body(head: str, sample: ReviewSample) -> dict:
    return {
        "model": head,
        "state": _review_state(sample),
        "questions": _review_questions(sample.published),
    }


def _parse_review_answers(data: dict) -> ReviewDecision:
    """`grounded`, `ready`, and `contradicts` from a System One reply."""
    answers = data.get("answers")
    if not isinstance(answers, dict):
        raise ValueError(f"no answers in reply: {str(data)[:120]}")
    grounded = answers.get("grounded") or {}
    ready = answers.get("ready") or {}
    contra = answers.get("contradicts") or {}
    if not isinstance(grounded.get("noul"), (int, float)):
        raise ValueError(f"grounded is not a noul: {grounded}")
    if not isinstance(ready.get("noul"), (int, float)):
        raise ValueError(f"ready is not a noul: {ready}")
    choice_val = contra.get("choice")
    if not isinstance(choice_val, str) or not choice_val:
        raise ValueError(f"contradicts is not a choice: {contra}")
    conf = contra.get("confidence")
    confidence = float(conf) if isinstance(conf, (int, float)) and not isinstance(conf, bool) else None
    return ReviewDecision(
        grounded_p=float(grounded["noul"]),
        ready_p=float(ready["noul"]),
        contradicts=choice_val,
        contradicts_conf=confidence,
        latency_ms=0.0,
    )


def _effective_choice(decision: ReviewDecision, *, choice_min: float) -> str:
    """Apply `DECIDER_CHOICE_MIN` the way `memory._decider_review_judge` does."""
    if not decision.contradicts or decision.contradicts == "none":
        return "none"
    conf = decision.contradicts_conf
    if conf is None or conf >= choice_min:
        return decision.contradicts
    return "none"


def _choice_min_at_precision(labels: list[str], choices: list[str], confidences: list[float | None],
                             min_precision: float) -> float | None:
    """Confidence floor where precision on non-`none` predictions crosses `min_precision`."""
    scored = [(c, l, ch) for c, l, ch in zip(confidences, labels, choices)
              if c is not None and ch != "none"]
    if len(scored) < 3:
        return None
    thresholds = sorted({c for c, _, _ in scored}, reverse=True)
    for thr in thresholds:
        preds = [ch for c, _, ch in scored if c >= thr]
        truths = [l for c, l, _ in scored if c >= thr]
        if len(preds) < 3:
            continue
        correct = sum(1 for p, t in zip(preds, truths) if p == t)
        if correct / len(preds) >= min_precision:
            return round(thr, 4)
    return None


def _load_review_samples(db) -> list[ReviewSample]:
    """Shards with a resolvable `contradicts` label against published ids in the project."""
    from sqlalchemy import select
    from app.models import MemoryShard

    pubs_by_project: dict[str | None, list[tuple[str, str]]] = {}
    pub_stmt = select(MemoryShard).where(
        MemoryShard.status == "published",
        MemoryShard.text.isnot(None),
    )
    for row in db.scalars(pub_stmt):
        pubs_by_project.setdefault(row.project_id, []).append((row.id, row.text or ""))

    stmt = select(MemoryShard).where(
        MemoryShard.review_judge_verdict.isnot(None),
        MemoryShard.text.isnot(None),
    )
    samples: list[ReviewSample] = []
    for row in db.scalars(stmt):
        verdict = row.review_judge_verdict or {}
        if not isinstance(verdict, dict):
            continue
        published = pubs_by_project.get(row.project_id, [])[:5]
        pub_ids = {sid for sid, _ in published}
        conflicts = verdict.get("conflicts") or []
        label = "none"
        if conflicts:
            for raw in conflicts:
                token = str(raw).strip()
                if token in pub_ids:
                    label = token
                    break
            if label == "none":
                # Chat-judge labels are often quotes, not ids — skip until a corpus exists.
                continue
        samples.append(ReviewSample(
            shard_id=row.id,
            candidate_text=row.text or "",
            published=published,
            label_choice=label,
        ))
    return samples


def _decide_review(endpoint: str, head: str, sample: ReviewSample, client: httpx.Client,
                   *, api_key: str = "") -> ReviewDecision:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    t0 = time.perf_counter()
    r = client.post(
        f"{endpoint}/v1/systemone",
        json=_review_request_body(head, sample),
        headers=headers,
        timeout=30.0,
    )
    latency_ms = (time.perf_counter() - t0) * 1000.0
    r.raise_for_status()
    data = r.json()
    routed = (data.get("routing") or {}).get("model") if isinstance(data.get("routing"), dict) else None
    if routed and routed != head:
        logger.warning("head %s was answered by %s — the endpoint routed the name", head, routed)
    decision = _parse_review_answers(data)
    decision.latency_ms = latency_ms
    return decision


def _run_review_head(endpoint: str, head: str, samples: list[ReviewSample],
                     client: httpx.Client, *, api_key: str = "",
                     choice_min: float = 0.70) -> ReviewReport:
    report = ReviewReport(head=head)
    latencies: list[float] = []
    labels: list[str] = []
    preds: list[str] = []
    confidences: list[float | None] = []

    for sample in samples:
        if not sample.candidate_text.strip():
            continue
        try:
            decision = _decide_review(endpoint, head, sample, client, api_key=api_key)
        except Exception as e:
            logger.warning("head %s: review call failed on shard %s: %s", head, sample.shard_id, e)
            continue
        report.decisions.append(decision)
        latencies.append(decision.latency_ms)
        report.n_labelled += 1
        labels.append(sample.label_choice)
        preds.append(_effective_choice(decision, choice_min=choice_min))
        confidences.append(decision.contradicts_conf)

    if report.n_labelled >= 2:
        report.choice_accuracy = round(
            sum(1 for p, l in zip(preds, labels) if p == l) / report.n_labelled, 4)
        report.choice_min_at_90_precision = _choice_min_at_precision(
            labels, preds, confidences, _QUALITY_PRECISION_MIN)
        report.measured = report.choice_min_at_90_precision is not None

    if latencies:
        report.latency_p50_ms = round(statistics.median(latencies), 1)
        sorted_lat = sorted(latencies)
        p95_idx = max(0, int(len(sorted_lat) * 0.95) - 1)
        report.latency_p95_ms = round(sorted_lat[p95_idx], 1)
    return report


def _decide(endpoint: str, head: str, text: str, client: httpx.Client,
            *, api_key: str = "") -> HeadDecision:
    """Call the decider for one shard. Returns keep probability, quality, and latency."""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    t0 = time.perf_counter()
    r = client.post(f"{endpoint}/v1/systemone", json=_request_body(head, text),
                    headers=headers, timeout=30.0)
    latency_ms = (time.perf_counter() - t0) * 1000.0
    r.raise_for_status()
    data = r.json()
    routed = (data.get("routing") or {}).get("model") if isinstance(data.get("routing"), dict) else None
    if routed and routed != head:
        # laya routes unknown names to a default head and says so; a report that names a
        # head nothing answered for is the substitution PRD-45 §12 warns about.
        logger.warning("head %s was answered by %s — the endpoint routed the name", head, routed)
    keep_prob, quality = _parse_answers(data)
    return HeadDecision(keep_prob=keep_prob, quality=quality, latency_ms=latency_ms)


def _load_labelled_shards(db) -> list[ShardSample]:
    """Human-labelled shards: a published or rejected shard that no scorer decided.

    `scoring_source` is set by `triage_candidate` (`similarity` / `llm` / `trusted`) and by
    agent adjudication (`agent`); a decided shard with no source was decided by a person in
    the review queue. That is the label PRD-45 §6 asks for — "every shard whose outcome is a
    human action". The first version accepted only `origin=agent:eval-sample`, a corpus the
    deployed store does not have (0 rows on 2026-09-23, beside 147 human-decided shards).
    """
    from sqlalchemy import or_, select
    from app.models import MemoryShard

    stmt = select(MemoryShard).where(
        MemoryShard.status.in_(("published", "rejected")),
        or_(MemoryShard.scoring_source.is_(None), MemoryShard.scoring_source == ""),
        MemoryShard.text.isnot(None),
    )
    rows = list(db.scalars(stmt))
    samples = []
    for r in rows:
        samples.append(ShardSample(
            shard_id=r.id,
            text=r.text or "",
            label_keep=(r.status == "published"),
            chat_judge_keep=None,
            chat_judge_quality=None,
            human_published=(r.status == "published"),
        ))
    return samples


def _load_scored_shards(db) -> list[ShardSample]:
    """Shards the chat judge already decided (scoring_source = llm/agent).

    The stored outcome (status) is the label: published = keep, rejected = reject.
    ``auto_confidence`` is the quality the chat judge assigned.
    """
    from sqlalchemy import select
    from app.models import MemoryShard

    stmt = select(MemoryShard).where(
        MemoryShard.scoring_source.in_(("llm", "agent")),
        MemoryShard.status.in_(("published", "rejected")),
        # Exclude eval-samples — those are the human-labelled corpus, not the judge's.
        MemoryShard.origin != _SAMPLE_ORIGIN,
    )
    rows = list(db.scalars(stmt))
    samples = []
    for r in rows:
        samples.append(ShardSample(
            shard_id=r.id,
            text=r.text or "",
            label_keep=(r.status == "published"),
            chat_judge_keep=None,
            chat_judge_quality=float(r.auto_confidence) if r.auto_confidence is not None else None,
            human_published=False,
        ))
    return samples


def _load_judged_shards(db) -> list[ShardSample]:
    """Shards with a stored review_judge_verdict (the chat judge's groundedness pass)."""
    from sqlalchemy import select
    from app.models import MemoryShard

    stmt = select(MemoryShard).where(
        MemoryShard.review_judge_verdict.isnot(None),
        MemoryShard.text.isnot(None),
    )
    rows = list(db.scalars(stmt))
    samples = []
    for r in rows:
        verdict = r.review_judge_verdict or {}
        # The review judge answers {grounded, ready}, not {keep, quality}.
        # For agreement, we use `ready` as the keep analogue — a shard the review
        # judge says is ready to publish is the same direction as keep.
        chat_keep = verdict.get("ready") if isinstance(verdict, dict) else None
        samples.append(ShardSample(
            shard_id=r.id,
            text=r.text or "",
            label_keep=None,
            chat_judge_keep=bool(chat_keep) if chat_keep is not None else None,
            chat_judge_quality=None,
            human_published=False,
        ))
    return samples


def _roc_auc(labels: list[bool], scores: list[float]) -> float | None:
    """Trapezoidal ROC-AUC. None when the corpus is degenerate (all one class)."""
    if len(labels) < 2:
        return None
    pos = sum(1 for l in labels if l)
    neg = len(labels) - pos
    if pos == 0 or neg == 0:
        return None
    paired = sorted(zip(scores, labels), key=lambda x: -x[0])
    tp = 0
    fp = 0
    auc = 0.0
    prev_fpr = 0.0
    prev_tpr = 0.0
    for _score, label in paired:
        if label:
            tp += 1
        else:
            fp += 1
        tpr = tp / pos
        fpr = fp / neg
        auc += (fpr - prev_fpr) * (tpr + prev_tpr) / 2.0
        prev_fpr = fpr
        prev_tpr = tpr
    return round(auc, 4)


def _cohens_kappa(a: list[bool], b: list[bool]) -> float | None:
    """Agreement corrected for chance. None when either rater is constant."""
    if len(a) != len(b) or len(a) < 2:
        return None
    n = len(a)
    agree = sum(1 for x, y in zip(a, b) if x == y) / n
    p1 = sum(a) / n
    p2 = sum(b) / n
    pe = p1 * p2 + (1 - p1) * (1 - p2)
    if pe >= 1.0:
        return None
    return round((agree - pe) / (1.0 - pe), 4)


def _keep_threshold_at_fpr(labels: list[bool], scores: list[float],
                           max_fpr: float) -> tuple[float | None, float | None, float | None]:
    """Find the keep threshold where false-accept rate ≤ max_fpr.

    Returns (threshold, abstain_low, abstain_high) or (None, None, None) when the
    corpus is too small. The abstain band is the gap between the keep threshold and
    the reject threshold (where false-reject rate ≤ max_fpr).
    """
    if len(labels) < 10:
        return None, None, None
    pos_scores = sorted(s for s, l in zip(scores, labels) if l)
    neg_scores = sorted(s for s, l in zip(scores, labels) if not l)
    if not pos_scores or not neg_scores:
        return None, None, None

    # Keep threshold: the lowest score where ≤ max_fpr of negatives score above it.
    max_false_accepts = max(1, int(max_fpr * len(neg_scores)))
    # Sort negatives descending; the threshold is at position max_false_accepts.
    neg_desc = sorted(neg_scores, reverse=True)
    if max_false_accepts >= len(neg_desc):
        threshold = min(neg_scores) - 0.001
    else:
        threshold = neg_desc[max_false_accepts]

    # Reject threshold: the highest score where ≤ max_fpr of positives score below it.
    max_false_rejects = max(1, int(max_fpr * len(pos_scores)))
    pos_asc = sorted(pos_scores)
    if max_false_rejects >= len(pos_asc):
        reject_thr = max(pos_scores) + 0.001
    else:
        reject_thr = pos_asc[max_false_rejects]

    abstain_low = min(threshold, reject_thr)
    abstain_high = max(threshold, reject_thr)
    return round(threshold, 4), round(abstain_low, 4), round(abstain_high, 4)


def _quality_at_precision(labels_keep: list[bool], qualities: list[float],
                          min_precision: float) -> float | None:
    """The quality level at which precision on 'human published' crosses min_precision.

    Scans quality thresholds from high to low; returns the first where precision
    (fraction of kept shards among those above the threshold) ≥ min_precision.
    """
    if not labels_keep or len(labels_keep) < 5:
        return None
    thresholds = sorted(set(qualities), reverse=True)
    for thr in thresholds:
        above = [l for l, q in zip(labels_keep, qualities) if q >= thr]
        if len(above) < 3:
            continue
        precision = sum(above) / len(above)
        if precision >= min_precision:
            return round(thr, 4)
    return None


def _run_head(endpoint: str, head: str, samples: list[ShardSample],
              client: httpx.Client, *, api_key: str = "") -> HeadReport:
    """Run one head over every sample and compute metrics."""
    report = HeadReport(head=head)
    latencies: list[float] = []

    labelled_labels: list[bool] = []
    labelled_scores: list[float] = []
    human_pub_labels: list[bool] = []
    human_pub_qualities: list[float] = []
    judged_decider: list[bool] = []
    judged_chat: list[bool] = []

    for s in samples:
        if not s.text.strip():
            continue
        try:
            decision = _decide(endpoint, head, s.text, client, api_key=api_key)
        except Exception as e:
            logger.warning("head %s: decider call failed on shard %s: %s", head, s.shard_id, e)
            continue

        report.decisions.append(decision)
        latencies.append(decision.latency_ms)

        # Labelled corpus (human keep/reject).
        if s.label_keep is not None:
            report.n_labelled += 1
            labelled_labels.append(s.label_keep)
            labelled_scores.append(decision.keep_prob)
            if s.human_published:
                human_pub_labels.append(True)
                human_pub_qualities.append(decision.quality)
            else:
                human_pub_labels.append(False)
                human_pub_qualities.append(decision.quality)

        # Judged corpus (chat judge verdict).
        if s.chat_judge_keep is not None:
            report.n_judged += 1
            judged_decider.append(decision.keep_prob >= 0.5)
            judged_chat.append(s.chat_judge_keep)

    # ROC-AUC on labelled corpus.
    if report.n_labelled >= 2:
        report.auc = _roc_auc(labelled_labels, labelled_scores)

    # Cohen's kappa on judged corpus.
    if judged_decider and judged_chat:
        report.kappa = _cohens_kappa(judged_decider, judged_chat)

    # Keep threshold at ≤ 2% false-accept.
    if report.n_labelled >= 10:
        thr, low, high = _keep_threshold_at_fpr(
            labelled_labels, labelled_scores, _FALSE_ACCEPT_MAX)
        report.keep_threshold = thr
        report.abstain_low = low
        report.abstain_high = high

    # Quality at 90% precision on human-published.
    if human_pub_labels:
        report.quality_at_90_precision = _quality_at_precision(
            human_pub_labels, human_pub_qualities, _QUALITY_PRECISION_MIN)

    # Latency.
    if latencies:
        report.latency_p50_ms = round(statistics.median(latencies), 1)
        sorted_lat = sorted(latencies)
        p95_idx = max(0, int(len(sorted_lat) * 0.95) - 1)
        report.latency_p95_ms = round(sorted_lat[p95_idx], 1)

    # Pass bars.
    if report.auc is not None and report.auc >= _AUC_PASS and report.n_labelled >= _MIN_LABELLED:
        report.pass_keep = True
    if report.quality_at_90_precision is not None:
        report.pass_quality = True

    return report


def _load_id_set(path: str) -> set[str]:
    """Shard ids from a text file (one per line) or JSON list / {holdout_ids: [...]}."""
    raw = Path(path).read_text().strip()
    if not raw:
        return set()
    if raw[0] in "[{":
        data = json.loads(raw)
        if isinstance(data, dict):
            data = data.get("holdout_ids") or data.get("ids") or []
        return {str(x) for x in data}
    return {ln.strip() for ln in raw.splitlines() if ln.strip() and not ln.startswith("#")}


def _filter_samples(samples: list[ShardSample], ids: set[str]) -> list[ShardSample]:
    """Keep only samples whose shard_id is in `ids`. Empty ids → empty result, not the full set."""
    return [s for s in samples if s.shard_id in ids]


def _get_session():
    """Bootstrap a DB session the same way the CLI does."""
    from app.db import SessionLocal
    return SessionLocal()


def _format_report(reports: list[HeadReport], *, n_labelled: int, n_scored: int,
                   n_judged: int) -> str:
    """Human-readable calibration report."""
    lines = []
    lines.append(f"# Decider Calibration Report")
    lines.append(f"")
    lines.append(f"Corpus: {n_labelled} labelled, {n_scored} auto-scored, {n_judged} judged shards")
    lines.append(f"Pass bar: AUC ≥ {_AUC_PASS} on ≥ {_MIN_LABELLED} labelled shards")
    lines.append(f"")

    any_pass = False
    for r in reports:
        status = "PASS" if r.pass_keep else "FAIL"
        if r.pass_keep:
            any_pass = True
        lines.append(f"## Head: {r.head} [{status}]")
        lines.append(f"")
        lines.append(f"| Metric | Value |")
        lines.append(f"|---|---|")
        lines.append(f"| Labelled shards | {r.n_labelled} |")
        lines.append(f"| Judged shards | {r.n_judged} |")
        lines.append(f"| ROC-AUC (keep) | {r.auc if r.auc is not None else '—'} |")
        lines.append(f"| Cohen's κ (vs chat judge) | {r.kappa if r.kappa is not None else '—'} |")
        lines.append(f"| Keep threshold (≤2% FA) | {r.keep_threshold if r.keep_threshold is not None else '—'} |")
        lines.append(f"| Abstain band | [{r.abstain_low}, {r.abstain_high}] |" if r.abstain_low is not None else "| Abstain band | — |")
        lines.append(f"| Quality @ 90% precision | {r.quality_at_90_precision if r.quality_at_90_precision is not None else '—'} |")
        lines.append(f"| Latency p50 | {r.latency_p50_ms} ms |")
        lines.append(f"| Latency p95 | {r.latency_p95_ms} ms |")
        lines.append(f"| Pass keep | {'✓' if r.pass_keep else '✗'} |")
        lines.append(f"| Pass quality | {'✓' if r.pass_quality else '✗'} |")
        lines.append(f"")

    if not any_pass:
        lines.append("**No head clears the pass bar.** The decider is not wired to adjudication.")
        lines.append("S3 does not ship; S1, S2 and S4 still do (a decider type with no calibrated")
        lines.append("consumer is still the right shape).")
    else:
        best = max((r for r in reports if r.pass_keep), key=lambda r: r.auc or 0)
        lines.append(f"**Best head: `{best.head}`** (AUC {best.auc}, {best.n_labelled} labelled shards)")

    return "\n".join(lines)


def _report_dict(reports: list[HeadReport], *, n_labelled: int, n_scored: int,
                 n_judged: int) -> dict:
    """Machine-readable calibration report."""
    return {
        "corpus": {
            "labelled": n_labelled,
            "auto_scored": n_scored,
            "judged": n_judged,
        },
        "pass_bar": {
            "auc_min": _AUC_PASS,
            "min_labelled": _MIN_LABELLED,
            "false_accept_max": _FALSE_ACCEPT_MAX,
            "quality_precision_min": _QUALITY_PRECISION_MIN,
        },
        "heads": [
            {
                "head": r.head,
                "n_labelled": r.n_labelled,
                "n_judged": r.n_judged,
                "auc": r.auc,
                "kappa": r.kappa,
                "keep_threshold": r.keep_threshold,
                "abstain_low": r.abstain_low,
                "abstain_high": r.abstain_high,
                "quality_at_90_precision": r.quality_at_90_precision,
                "latency_p50_ms": r.latency_p50_ms,
                "latency_p95_ms": r.latency_p95_ms,
                "pass_keep": r.pass_keep,
                "pass_quality": r.pass_quality,
            }
            for r in reports
        ],
        "any_pass": any(r.pass_keep for r in reports),
    }


def _format_review_report(reports: list[ReviewReport], *, n_samples: int,
                          choice_min: float) -> str:
    lines = [
        "# Decider Review-Pass Calibration Report",
        "",
        f"Corpus: {n_samples} shards with resolvable `contradicts` labels",
        f"Applied floor while scoring: DECIDER_CHOICE_MIN = {choice_min}",
        "",
    ]
    for r in reports:
        status = "MEASURED" if r.measured else "UNMEASURED"
        lines.append(f"## Head: {r.head} [{status}]")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|---|---|")
        lines.append(f"| Labelled shards | {r.n_labelled} |")
        lines.append(f"| Choice accuracy @ {choice_min} | {r.choice_accuracy if r.choice_accuracy is not None else '—'} |")
        lines.append(
            f"| Suggested CHOICE_MIN @ 90% precision | "
            f"{r.choice_min_at_90_precision if r.choice_min_at_90_precision is not None else '—'} |"
        )
        lines.append(f"| Latency p50 | {r.latency_p50_ms} ms |")
        lines.append(f"| Latency p95 | {r.latency_p95_ms} ms |")
        lines.append("")
    if not any(r.measured for r in reports):
        lines.append(
            "**No head produced a measured `DECIDER_CHOICE_MIN`.** "
            "Either the corpus is too small, labels are missing, or the endpoint was unreachable."
        )
    return "\n".join(lines)


def _review_report_dict(reports: list[ReviewReport], *, n_samples: int,
                        choice_min: float) -> dict:
    return {
        "mode": "review-pass",
        "corpus": {"conflict_labelled": n_samples},
        "choice_min_applied": choice_min,
        "heads": [
            {
                "head": r.head,
                "n_labelled": r.n_labelled,
                "choice_accuracy": r.choice_accuracy,
                "choice_min_at_90_precision": r.choice_min_at_90_precision,
                "latency_p50_ms": r.latency_p50_ms,
                "latency_p95_ms": r.latency_p95_ms,
                "measured": r.measured,
            }
            for r in reports
        ],
        "any_measured": any(r.measured for r in reports),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Calibrate a decider against Graphban's labelled shards")
    parser.add_argument("--endpoint", default=os.environ.get("DECIDER_ENDPOINT", "http://localhost:8090"),
                        help="Decider endpoint URL (default: $DECIDER_ENDPOINT or http://localhost:8090)")
    parser.add_argument("--api-key", default=os.environ.get("DECIDER_API_KEY", ""),
                        help="API key for the decider endpoint (default: $DECIDER_API_KEY)")
    parser.add_argument("--heads", nargs="*", default=None,
                        help="Heads to test (default: auto-discover from /health, fallback to all known)")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of markdown")
    parser.add_argument("--project", default=None, help="Project ID to scope shards (default: all)")
    parser.add_argument("--max-shards", type=int, default=0,
                        help="Cap on shards per corpus (0 = unlimited, useful for dry runs)")
    parser.add_argument(
        "--shard-ids-file",
        default=None,
        help=(
            "Restrict every corpus to these shard ids (one per line, or JSON list / "
            "object with holdout_ids). Use this for a frozen holdout; the default "
            "full-DB load includes train shards and is not an honest S0."
        ),
    )
    parser.add_argument(
        "--review-pass",
        action="store_true",
        help=(
            "Calibrate the review-pass `contradicts` choice (PRD-45 D6/D7) instead of "
            "write-path keep/quality. Reports choice accuracy and a suggested "
            "DECIDER_CHOICE_MIN when a conflict-labelled corpus exists."
        ),
    )
    parser.add_argument(
        "--choice-min",
        type=float,
        default=0.70,
        help="Confidence floor applied while scoring review-pass choice accuracy (default: 0.70)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    endpoint = args.endpoint.rstrip("/")
    client = httpx.Client()

    # Discover heads.
    heads = args.heads or _discover_heads(endpoint, client) or list(_DEFAULT_HEADS)
    logger.info("calibrating heads: %s", heads)
    logger.info("endpoint: %s", endpoint)

    # Load corpora from the database.
    db = _get_session()
    try:
        if args.review_pass:
            review_samples = _load_review_samples(db)
        else:
            labelled = _load_labelled_shards(db)
            scored = _load_scored_shards(db)
            judged = _load_judged_shards(db)
    finally:
        db.close()

    if args.review_pass:
        if args.shard_ids_file:
            want = _load_id_set(args.shard_ids_file)
            review_samples = [s for s in review_samples if s.shard_id in want]
            logger.info("restricted review corpus to %d ids from %s", len(review_samples),
                        args.shard_ids_file)
        if args.max_shards:
            review_samples = review_samples[:args.max_shards]
        logger.info("review corpus: %d conflict-labelled shards", len(review_samples))
        if not review_samples:
            print("No conflict-labelled shards found. Seed a review corpus whose "
                  "`review_judge_verdict.conflicts` names published shard ids.")
            sys.exit(1)
        reports = [
            _run_review_head(endpoint, head, review_samples, client, api_key=args.api_key,
                             choice_min=args.choice_min)
            for head in heads
        ]
        if args.json:
            print(json.dumps(_review_report_dict(reports, n_samples=len(review_samples),
                                                 choice_min=args.choice_min), indent=2))
        else:
            print(_format_review_report(reports, n_samples=len(review_samples),
                                        choice_min=args.choice_min))
        return

    if args.shard_ids_file:
        want = _load_id_set(args.shard_ids_file)
        labelled = _filter_samples(labelled, want)
        scored = _filter_samples(scored, want)
        judged = _filter_samples(judged, want)
        logger.info("restricted to %d ids from %s (labelled %d, scored %d, judged %d)",
                    len(want), args.shard_ids_file, len(labelled), len(scored), len(judged))
        missing = want - {s.shard_id for s in labelled} - {s.shard_id for s in scored} - {s.shard_id for s in judged}
        if missing:
            logger.warning("shard-ids-file has %d ids not in any corpus, e.g. %s",
                           len(missing), sorted(missing)[:5])

    if args.max_shards:
        labelled = labelled[:args.max_shards]
        scored = scored[:args.max_shards]
        judged = judged[:args.max_shards]

    # The calibration corpus is the union: labelled + scored provide keep labels,
    # judged provides chat-judge agreement.
    all_samples = labelled + scored + judged
    # Deduplicate by shard_id (a shard could appear in both labelled and judged).
    seen: set[str] = set()
    deduped: list[ShardSample] = []
    for s in all_samples:
        if s.shard_id not in seen:
            seen.add(s.shard_id)
            deduped.append(s)
    all_samples = deduped

    logger.info("corpus: %d labelled, %d auto-scored, %d judged (%d unique)",
                len(labelled), len(scored), len(judged), len(all_samples))

    if not all_samples:
        print("No shards found in the database. Run the application and create some memory first.")
        sys.exit(1)

    # Run each head.
    reports: list[HeadReport] = []
    for head in heads:
        logger.info("running head: %s", head)
        report = _run_head(endpoint, head, all_samples, client, api_key=args.api_key)
        reports.append(report)
        logger.info("  %s: AUC=%s, labelled=%d, judged=%d, pass=%s",
                     head, report.auc, report.n_labelled, report.n_judged, report.pass_keep)

    # Output.
    if args.json:
        out = _report_dict(reports, n_labelled=len(labelled), n_scored=len(scored),
                           n_judged=len(judged))
        print(json.dumps(out, indent=2))
    else:
        print(_format_report(reports, n_labelled=len(labelled), n_scored=len(scored),
                             n_judged=len(judged)))


if __name__ == "__main__":
    main()
