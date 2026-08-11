"""Memory shard service — semantic search over pgvector with a SQLite fallback."""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import settings
from app.embeddings import cosine_similarity, get_embedder, safe_embed
from app.models import MemoryShard, Project
from app.services import events as events_svc

logger = logging.getLogger("graphban.memory")


def _includes_global(db: Session, project_id: str) -> bool:
    """Whether project-less ("global", project_id IS NULL) shards should surface in
    THIS project's memory. Honors the project's `share_global_memory` opt-out
    (AL-71) — previously ignored, so global shards bled into every project. In
    hosted mode global shards can't be created at all, so this stays False and no
    tenant's memory ever crosses into another's."""
    if settings.hosted_mode:
        return False
    project = db.get(Project, project_id)
    return bool(project and project.share_global_memory)


def age_state(shard: MemoryShard, *, now: datetime | None = None) -> str:
    """`fresh` | `expired` | `stale` — the decay clock (GRPH-306 / PRD-16).

    Computed from `created_at` rather than stored, so it cannot drift out of step with the
    row and there is no sweep to forget to run.

    Two different fates, because they mean different things:

    - a CANDIDATE nothing has repeated in `CANDIDATE_EXPIRY_DAYS` has gone cold — it was
      never corroborated and is dropped from retrieval;
    - a PUBLISHED shard with no fresh support in `PUBLISHED_STALE_DAYS` is `stale`-FLAGGED,
      never hidden. Something a human stood behind does not stop being true because nobody
      restated it lately, and silently retiring it would delete the corpus's oldest and
      most-settled knowledge first.
    """
    created = shard.created_at
    if created is None:
        return "fresh"
    now = now or datetime.now(timezone.utc)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    days = (now - created).days
    if shard.status == "candidate" and days >= CANDIDATE_EXPIRY_DAYS:
        return "expired"
    if shard.status == "published" and days >= PUBLISHED_STALE_DAYS:
        return "stale"
    return "fresh"


def list_shards(
    db: Session, project_id: str | None = None, status: str | None = None,
    include_expired: bool = False,
) -> list[MemoryShard]:
    """`include_expired` exists for the review UI and for tests, so an expired candidate is
    still REACHABLE — PRD-16 asks that it stop appearing in retrieval "without being
    hard-deleted", and a row nothing can fetch is deleted in every way that matters."""
    stmt = select(MemoryShard)
    if project_id:
        if _includes_global(db, project_id):
            stmt = stmt.where(
                (MemoryShard.project_id == project_id) | (MemoryShard.project_id.is_(None))
            )
        else:
            stmt = stmt.where(MemoryShard.project_id == project_id)
    if status is not None:
        stmt = stmt.where(MemoryShard.status == status)
    stmt = stmt.order_by(MemoryShard.created_at.desc())
    rows = list(db.scalars(stmt).all())
    if include_expired:
        return rows
    return [r for r in rows if age_state(r) != "expired"]


def add_memory(
    db: Session,
    *,
    text_body: str,
    scope: str = "global",
    source: str = "",
    item_id: str | None = None,
    project_id: str | None = "core",
    fresh: bool = True,
    status: str = "published",
    origin: str = "",
    auto_triage: bool = True,
    embed_text: str | None = None,
) -> MemoryShard:
    """`embed_text` lets a producer store a READABLE shard while the ladder compares only
    its content (GRPH-350).

    Everything else embeds exactly what it stores, which is right until a producer writes
    structure. Episodes do: every one carries `Tool:` / `Attempted:` / `Failed:` and, until
    this existed, a ~70-character sentence naming its state. `bge-m3` embeds all of it, so
    two episodes about unrelated tools scored as near-duplicates on their preamble — and
    cluster size is what the ladder promotes on, so the FORMAT was manufacturing the
    corroboration. The first promotion pass over the episode corpus returned 18 accepts
    split exactly 6/6/6 across the three states, which is what that looks like.

    Optional and defaulting to the stored text, so no existing producer changes behaviour.
    """
    # Redact BEFORE the row exists (GRPH-305). Scrubbing at publish time is too late: a
    # candidate is already persisted and already searchable, so the leak has happened.
    # Here, every producer inherits it — ingest, extract_lessons, the grill, agent writes.
    from app.services.scrub import scrub

    text_body, _redacted = scrub(text_body)
    # Scrubbed too: it never reaches a row, but a producer should not have to know that in
    # order to reason about where a secret can end up.
    vector_source = scrub(embed_text)[0] if embed_text else text_body
    shard = MemoryShard(
        id="m_" + uuid.uuid4().hex[:10],
        text=text_body,
        # Records that the redactor RAN, not that it found something. A False here means
        # "written before this existed", which is a different claim from "clean".
        scrubbed=True,
        scope=scope,
        source=source or ("global" if scope == "global" else (f"from {item_id}" if item_id else "")),
        item_id=item_id,
        project_id=project_id,
        # Never lose the write to a down embedder — backfill fills a NULL vector later.
        embedding=safe_embed(vector_source),
        fresh=fresh,
        status=status,
        origin=origin,
    )
    db.add(shard)
    db.commit()
    db.refresh(shard)
    # Agent candidates are triaged on write (AL-227): the scorer may auto-reject or
    # auto-publish per the project's toggles, giving the agent instant feedback in
    # the same call. Human/published writes skip it (only candidates are triaged).
    if auto_triage and shard.status == "candidate":
        shard = triage_candidate(db, shard)
    return shard


def cluster_candidates(
    db: Session, *, project_id: str | None = None, threshold: float = 0.88, min_size: int = 2
) -> list[list[MemoryShard]]:
    """Group candidate shards by embedding similarity — recurring agent lessons
    worth promoting together (AL-50). When the same correction shows up N times,
    it points at an underlying principle that deserves a durable owner (the
    feedback thesis). Greedy single-pass; returns clusters of size >= min_size,
    largest first. Small by construction — it runs over the review queue only."""
    cands = [
        s for s in list_shards(db, project_id=project_id, status="candidate")
        if s.embedding is not None
    ]
    vecs = {s.id: list(s.embedding) for s in cands}  # coerce pgvector arrays → lists
    used: set[str] = set()
    clusters: list[list[MemoryShard]] = []
    for i, a in enumerate(cands):
        if a.id in used:
            continue
        group = [a]
        used.add(a.id)
        for b in cands[i + 1:]:
            if b.id in used:
                continue
            if cosine_similarity(vecs[a.id], vecs[b.id]) >= threshold:
                group.append(b)
                used.add(b.id)
        if len(group) >= min_size:
            clusters.append(group)
    clusters.sort(key=len, reverse=True)
    return clusters


# --- Candidate scoring (AL-151) -------------------------------------------------
# Similarity + heuristics over embeddings we already store — no LLM, works offline.
_SIM_STRONG = 0.88     # corroborated by / clusters with trusted memory (matches cluster threshold)
_SIM_DUP = 0.95        # near-identical → duplicate of an existing published shard
_SIM_REJECTED = 0.85   # resembles something a human already rejected
# Auto-triage (AL-227): auto-publish is reserved for the strongest corroboration —
# a higher bar than the advisory "accept" so novel recurrence alone won't publish.
_AUTO_ACCEPT_MIN = 0.9


# Published shards that NO human ever assessed. `trusted` (AL-280) publishes on write
# with nothing looked at; `agent` (AL-282) passed a judge but no person. Both are real
# memory and both surface in search — they are simply not evidence that a NEW claim is
# sound, which is what the corroboration signal is supposed to mean.
#
# This is what stops a long unattended run from hollowing out the review boundary: without
# it, months of unreviewed shards become the pool that later candidates are measured
# against, so new junk auto-accepts for corroborating with old junk and the gate reads as
# enabled while meaning nothing.
_UNVETTED_SOURCES = ("trusted", "agent")


def _corroboration_pool(db: Session, project_id: str | None) -> list[tuple[MemoryShard, list[float]]]:
    return [
        (s, list(s.embedding))
        for s in list_shards(db, project_id=project_id, status="published")
        if s.embedding is not None and s.scoring_source not in _UNVETTED_SOURCES
    ]


def _best_match(vec: list[float], pool: list[tuple[MemoryShard, list[float]]]) -> tuple[MemoryShard | None, float]:
    """The most-similar shard in `pool` and its cosine score (0.0 if the pool is empty)."""
    best, score = None, 0.0
    for shard, svec in pool:
        sim = cosine_similarity(vec, svec)
        if sim > score:
            best, score = shard, sim
    return best, score


# The promotion ladder (GRPH-306 / PRD-16). Named constants, deliberately, and NOT project
# settings: we do not know the right values yet, and a slider is how you avoid finding out.
# Pick one number; if it is wrong somebody hits it and we learn something real.
MIN_DISTINCT_SOURCES = 3        # independent sessions before recurrence counts as evidence
MIN_DISTINCT_SOURCES_CORRECTION = 2  # corrections earn trust faster — they are self-punishing
CANDIDATE_EXPIRY_DAYS = 45      # a candidate nothing has repeated has gone cold
PUBLISHED_STALE_DAYS = 120      # a published shard nothing corroborates any more


# Sources that identify a genuine ORIGIN rather than a category. `source` doubles as both:
# an ingested event carries `transcript:<harness>:<session>`, while an ordinary write
# carries `global` or `from <item>` — a bucket every producer shares.
_ORIGIN_PREFIXES = ("transcript:",)


def _origin_of(shard: MemoryShard) -> str | None:
    """The session this shard came from, or None when nothing records one.

    None matters more than it looks. Counting `global` as an origin would collapse every
    agent write in a project into one apparent session, and the ladder would then refuse to
    promote anything an agent learned — a shared PLACEHOLDER is not evidence of a shared
    source, and reading it as one is the same mistake as reading an absence as a clean
    result.
    """
    src = shard.source or ""
    return src if any(src.startswith(p) for p in _ORIGIN_PREFIXES) else None


def _distinct_origins(group) -> int | None:
    """Distinct sessions across a cluster, or None when the signal is not available.

    All-or-nothing on purpose: if any member's origin is unknown, the count is a lower
    bound rather than a fact, and vetoing on a lower bound holds back lessons that may well
    be independent."""
    origins = [_origin_of(g) for g in group]
    if not origins or any(o is None for o in origins):
        return None
    return len(set(origins))


def _cluster_representative(group: list[MemoryShard]) -> MemoryShard:
    """The one member that speaks for a cluster (GRPH-346).

    A cluster IS one recurring lesson — that is exactly what makes its size count as
    evidence — so promoting it must publish one shard, not one per occurrence.

    The medoid, not the first or the longest: the member with the highest total similarity
    to the rest is the one whose wording the others agree on. `Failed to get project`,
    `Failed to list projects` and `Failed to create project` are three spellings of "re-auth
    to Railway", and the medoid picks the phrasing nearest all three rather than whichever
    happened to be written first. Ties fall to the longer text (more of the lesson survives)
    and then to the id, so the choice is stable across runs.
    """
    pairs = [(s, list(s.embedding)) for s in group if s.embedding is not None]
    if not pairs:
        return group[0]
    centrality = {
        s.id: sum(cosine_similarity(v, other) for t, other in pairs if t.id != s.id)
        for s, v in pairs
    }
    return sorted(
        pairs, key=lambda it: (-centrality[it[0].id], -len(it[0].text or ""), str(it[0].id))
    )[0][0]


# Ingested episodes that record no CHANGE, and so cannot be a lesson however often they
# recur. Named for the state rather than listed as "not resolved", so a fourth state added
# later has to be classified deliberately instead of defaulting to promotable (GRPH-350).
_UNPROMOTABLE_STATES = ("unresolved", "transient")


def _unpromotable_state(shard: MemoryShard) -> str | None:
    """`unresolved` | `transient` | None — why this shard must not promote, if it must not.

    Reads `origin`, which is where the ingest runner records the episode's state, and where
    `artifacts._is_unresolved` already reads it for the classification side.
    """
    state = (shard.origin or "").rsplit(":", 1)[-1]
    return state if state in _UNPROMOTABLE_STATES else None


def _is_correction(shard: MemoryShard) -> bool:
    """Correction-class lessons earn trust faster (PRD-16).

    A lesson learned from something GOING WRONG carries its own evidence: the failure
    happened, and nobody writes down a correction they did not need. That is a different
    epistemic footing from a general observation, which is why the gate relaxes rather
    than being uniformly lowered."""
    text = f"{shard.source} {shard.origin} {shard.text}".lower()
    return any(w in text for w in ("fix", "bug", "broke", "regression", "failed",
                                   "corrected", "mistake", "wrong"))


def _score_shard(
    cv: list[float],
    published: list[tuple[MemoryShard, list[float]]],
    rejected: list[tuple[MemoryShard, list[float]]],
    support: int,
    human_derived: bool,
    corroborating: list[tuple[MemoryShard, list[float]]] | None = None,
    distinct_sources: int | None = None,
    correction: bool = False,
) -> tuple[str, float, list[str], str | None]:
    """Score one candidate embedding into (suggestion, confidence, reasons, duplicate_of).

    The single source of truth for the accept/reject/review heuristic — shared by the
    advisory review queue (`score_candidates`) and synchronous auto-triage
    (`triage_candidate`) so both judge a shard identically. Vetoes (rejection
    resemblance, duplication) win over accept signals.

    Two pools, deliberately (AL-282). `published` is every published shard and drives
    DEDUP — a trusted project must still detect duplicates of its own trusted shards, or
    it fills with restatements of one fact. `corroborating` is the vetted subset and
    drives the ACCEPT signal, because "something already published looks like this" is
    only evidence when a human or a judge stood behind that something. Defaults to
    `published` so a caller that doesn't care gets the old single-pool behaviour."""
    if corroborating is None:
        corroborating = published
    best_dup, dup = _best_match(cv, published)
    best_corr, corr = _best_match(cv, corroborating)
    _, rej = _best_match(cv, rejected)
    reasons: list[str] = []
    duplicate_of: str | None = None

    if rej >= _SIM_REJECTED:
        suggestion, confidence = "reject", rej
        reasons.append(f"resembles a previously rejected shard ({rej:.0%})")
    elif dup >= _SIM_DUP and best_dup is not None:
        suggestion, confidence = "reject", dup
        duplicate_of = best_dup.id
        reasons.append(f"near-duplicate of published {best_dup.id} ({dup:.0%}) — merge candidate")
    elif support >= 2 or corr >= _SIM_STRONG:
        suggestion = "accept"
        confidence = min(1.0, max(corr, 0.6 + 0.1 * support) + (0.1 if human_derived else 0.0))
        if support >= 2:
            reasons.append(f"recurs across {support} candidates")
        if corr >= _SIM_STRONG and best_corr is not None:
            reasons.append(f"corroborated by trusted {best_corr.id} ({corr:.0%})")
        if human_derived:
            reasons.append("from a human-reviewed decision")
    else:
        suggestion, confidence = "review", 0.3
        reasons.append("novel — no strong signal either way")

    # Distinct-source recurrence (GRPH-306). A VETO on an accept, never a new accept path —
    # PRD-16's success metric requires the scorer's verdicts to be unchanged for inputs that
    # lack the new signal, and a veto is the only shape that guarantees that.
    #
    # The failure it closes: `support` counts corroborating candidates, and one long session
    # restating itself produces as many as three independent ones. Recurrence within a single
    # source is repetition, not evidence, and promoting on it would let a lesson that happened
    # once look like a pattern.
    if suggestion == "accept" and distinct_sources is not None and support >= 2:
        need = MIN_DISTINCT_SOURCES_CORRECTION if correction else MIN_DISTINCT_SOURCES
        if distinct_sources < need:
            suggestion, confidence = "review", min(confidence, 0.45)
            reasons.append(
                f"recurs {support}× but across only {distinct_sources} source(s) — "
                f"repetition within one session is not independent evidence")

    return suggestion, round(confidence, 3), reasons, duplicate_of


def score_candidates(db: Session, *, project_id: str | None = None) -> list[dict]:
    """Advisory accept/reject suggestions for the review queue (AL-151).

    For each candidate, compare its embedding against the trusted (`published`) and
    vetoed (`rejected`) pools and its own recurrence (clusters), then emit a suggestion
    (`accept` | `reject` | `review`), a confidence, and human-readable reasons. Never
    mutates and never auto-publishes — the AL-49 human boundary holds. Sorted most
    actionable first (highest confidence), so obvious accepts/dupes rise to the top.

    A cluster is collapsed to one representative first (GRPH-346), so a recurring lesson
    is offered for promotion ONCE with its recurrence as the evidence, and its other
    occurrences are offered as merges into it.

    Similarity-only, so it degrades to noise (not an error) when embeddings are the
    offline stub — it needs no chat provider."""
    cands = [s for s in list_shards(db, project_id=project_id, status="candidate") if s.embedding is not None]
    published = [(s, list(s.embedding)) for s in list_shards(db, project_id=project_id, status="published") if s.embedding is not None]
    rejected = [(s, list(s.embedding)) for s in list_shards(db, project_id=project_id, status="rejected") if s.embedding is not None]
    corroborating = _corroboration_pool(db, project_id)

    # Recurrence: how many candidates each one clusters with (reuse AL-50 clustering).
    cluster_size: dict[str, int] = {}
    cluster_sources: dict[str, int] = {}
    # Every member EXCEPT the one that speaks for the cluster, mapped to it (GRPH-346).
    speaks_for: dict[str, tuple[MemoryShard, list[float]]] = {}
    for group in cluster_candidates(db, project_id=project_id):
        distinct = _distinct_origins(group)
        rep = _cluster_representative(group)
        rep_vec = list(rep.embedding) if rep.embedding is not None else None
        for s in group:
            cluster_size[s.id] = len(group)
            cluster_sources[s.id] = distinct
            if s.id != rep.id and rep_vec is not None:
                speaks_for[s.id] = (rep, rep_vec)

    out: list[dict] = []
    for c in cands:
        # A cluster is ONE lesson. Scoring every member on the cluster's own size made 96
        # accepts out of 19 distinct texts, 32 of them the same string — and publishing that
        # set would have put 32 copies into the corroboration pool, where they would read as
        # 32 independent pieces of evidence for whatever was scored next. The ingest runner
        # closes this exact hole one layer down; the ladder had it open (GRPH-346).
        #
        # Surfaced as a duplicate rather than dropped: a queue that silently shed 77 rows
        # would look like a corpus that never had them.
        rep_pair = speaks_for.get(c.id)
        if rep_pair is not None:
            rep, rep_vec = rep_pair
            sim = cosine_similarity(list(c.embedding), rep_vec)
            out.append({
                "shard": c,
                "suggestion": "reject",
                "confidence": round(sim, 3),
                "reasons": [f"same lesson as candidate {rep.id} ({sim:.0%}) — "
                            f"one of {cluster_size.get(c.id, 1)} occurrences; "
                            f"promote {rep.id} and merge this into it"],
                "duplicate_of": rep.id,
            })
            continue
        support = cluster_size.get(c.id, 1)
        human_derived = c.origin.startswith("user:") or "grill" in c.origin
        suggestion, confidence, reasons, duplicate_of = _score_shard(
            list(c.embedding), published, rejected, support, human_derived, corroborating,
            distinct_sources=cluster_sources.get(c.id), correction=_is_correction(c),
        )
        # An ingested failure nothing ever fixed, or one that succeeded on an IDENTICAL
        # retry, is evidence — not a lesson. Classification already refuses both; the
        # promotion path refused neither, so "the identical call succeeded on retry" could
        # be published into trusted memory as something learned (GRPH-350).
        #
        # A VETO, never a new accept path — it can only hold something back, so every
        # verdict that does not involve one of these is unchanged. Same shape as the
        # distinct-source rule, and for the same reason.
        if suggestion == "accept" and _unpromotable_state(c):
            suggestion, confidence = "review", min(confidence, 0.45)
            reasons.append(
                f"an {_unpromotable_state(c)} failure — evidence that something is "
                f"repeatedly painful, but it records nothing that was done differently")
        out.append({
            "shard": c,
            "suggestion": suggestion,
            "confidence": confidence,
            "reasons": reasons,
            "duplicate_of": duplicate_of,
        })

    out.sort(key=lambda r: r["confidence"], reverse=True)
    return out


WRITE_MODES = ("review", "auto", "trusted")


def _triage_prefs(db: Session, project_id: str | None) -> tuple[str, bool, bool]:
    """(write_mode, auto_reject, llm_judge) for a project (AL-280). Project-less
    ("global") shards, or an unknown project, fall back to the platform defaults:
    review, reject on, judge off — the conservative combination, because a shard
    with no project has no owner to have chosen otherwise."""
    if project_id is None:
        return "review", True, False
    project = db.get(Project, project_id)
    if project is None:
        return "review", True, False
    mode = project.memory_write_mode if project.memory_write_mode in WRITE_MODES else "review"
    return mode, bool(project.memory_auto_reject), bool(project.memory_llm_judge)


# --- LLM judge (AL-227) ---------------------------------------------------------
# When `memory_llm_judge` is on, the project's chat model rates a candidate's quality
# / actionability, enriching the offline similarity signal. Structural vetoes (near-
# duplicate, resembles-rejected) still win — the judge refines the accept/quality view,
# it can't rescue a duplicate. Falls back to similarity when no real model is configured.
_JUDGE_SYSTEM = (
    "You are a strict reviewer of an AI coding agent's memory notes. A GOOD memory is a "
    "durable, specific, actionable fact, decision, or convention that will help a future "
    "agent working on this project. REJECT notes that are vague, transient, obvious, "
    "redundant, or low-signal. Respond with ONLY a compact JSON object and nothing else: "
    '{"keep": <true|false>, "quality": <number 0..1>, "reason": "<one short sentence>"}'
)
_JUDGE_QUESTION = "Rate this candidate memory. Return only the JSON object."
_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)

# How many times the judge is asked before its answer is trusted (GRPH-348).
#
# `temperature=0` was assumed to make this unnecessary. It does not: judging one stored
# shard five times through ollama returned `keep=False q=0.1` four times and
# `keep=True q=0.8` once. A single sample from a judge that disagrees with itself is not an
# adjudication, and `agent_publish` promises "the JUDGE decides" — which is only worth
# something if it decides the same thing twice.
#
# Unanimity on `keep`, not a majority: a split means the judge has no answer, and inventing
# one from 2-of-3 would report a coin flip as a verdict. Disagreement returns None, which
# every caller already handles as "degrade to similarity" / "a human decides".
JUDGE_SAMPLES = 3

# The bar a judge's own quality score must clear to publish. Its own constant, because this
# previously borrowed `_SIM_STRONG` (0.88) — a COSINE SIMILARITY threshold. A model's
# self-reported rating and a vector distance are not the same scale and nothing says they
# should share a number; the coincidence rejected two shards the judge had described as
# "Specific actionable instruction to read a file before writing" (0.80) and "Specific
# actionable advice for a common unauthorized error" (0.70).
#
# 0.75 sits above the observed junk (0.0-0.2 for vague error text) and below the observed
# clear positives (0.8-0.9). A number to move on evidence, which is the point of naming it.
_JUDGE_PUBLISH_MIN = 0.75

# The judge answering that it received NOTHING. Not a quality verdict — a report that the
# prompt was empty — and `_parse_judge` was accepting it as quality 0.0, which reads as
# "worthless" when it means "unread". Three shards were rejected on exactly this.
#
# A heuristic over model prose, and deliberately biased safe: a false positive degrades to
# the similarity signal, which is the behaviour when no model is configured at all.
_NO_INPUT_RE = re.compile(
    r"\bno\s+(?:memory\s+)?(?:note|content|text|input|candidate)\b[^.]*\b"
    r"(?:provided|supplied|given|found|present)\b"
    r"|\b(?:nothing|no\s+data)\s+(?:was\s+)?(?:provided|supplied|given)\b"
    r"|\bempty\s+(?:input|prompt|note|content)\b", re.I)


def _parse_judge(raw: str) -> dict | None:
    """Parse the judge's reply into {keep, quality, reason}, defensively. Returns None
    if no well-formed verdict is present — the caller then falls back to similarity."""
    if not raw:
        return None
    match = _JSON_OBJ_RE.search(raw)
    if match is None:
        return None
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or "keep" not in data:
        return None
    reason = str(data.get("reason", "")).strip()
    # A judge that says it saw no input has not judged (GRPH-348). Treating that as
    # quality 0.0 makes "I could not read this" indistinguishable from "this is worthless",
    # and only one of those is a reason to reject something.
    if _NO_INPUT_RE.search(reason):
        logger.warning("llm judge: reply claims no input was given (%r); "
                       "treating as no verdict", reason[:120])
        return None
    try:
        quality = float(data.get("quality", 0.0))
    except (ValueError, TypeError):
        quality = 0.0
    return {
        "keep": bool(data["keep"]),
        "quality": max(0.0, min(1.0, quality)),
        "reason": reason,
    }


# Why the judge produced no verdict (GRPH-351). `None` used to have exactly one cause, and
# the message a caller showed said so. GRPH-348 gave it three more and left the message
# alone, so an operator was told "no chat model is configured" about a model that had just
# judged five other shards in the same run.
#
# `split` and `no_input` are properties of the CANDIDATE — this shard is contested, or is
# shaped so the judge cannot read it. `no_provider` and `error` are properties of the
# INSTALLATION. Only the second pair is something an operator can go and fix.
JUDGE_CAUSES = {
    "no_provider": "no independent chat model is configured for this project",
    "split": "the judge did not agree with itself across samples, so this candidate has no "
             "adjudication rather than a negative one",
    "no_input": "the judge replied that it received no content to rate, so its answer is "
                "not a verdict about this candidate",
    "unparseable": "the judge did not answer in the required form",
    "error": "the judge could not be reached",
}


def judge_verdict(db: Session, shard: MemoryShard) -> tuple[dict | None, str]:
    """`(verdict, cause)` — the judge's answer and, when there is none, WHY (GRPH-351).

    `_llm_judge` keeps returning a bare `dict | None` for callers that only act on a
    verdict; anything that reports a failure to a human should come through here, because
    "the model is missing" and "the model cannot decide about this shard" send a reader to
    completely different places.
    """
    from app.services import platform as platform_svc  # lazy: avoid import cycle

    try:
        provider, model = platform_svc.resolve_chat(db, shard.project_id or "core")
    except Exception:  # noqa: BLE001 — never let provider resolution break a write
        logger.exception("llm judge: provider resolution failed")
        return None, "error"
    if provider == "stub":
        return None, "no_provider"

    text = " ".join((shard.text or "").split())
    verdicts: list[dict] = []
    for _ in range(JUDGE_SAMPLES):
        try:
            raw = model.chat(system=_JUDGE_SYSTEM, context=text,
                             question=_JUDGE_QUESTION, temperature=0)
        except Exception:  # noqa: BLE001 — a model outage must not fail the memory write
            logger.exception("llm judge: chat call failed")
            return None, "error"
        verdict = _parse_judge(raw)
        if verdict is None:
            # Two different failures, and collapsing them would be the very conflation this
            # function exists to undo: the judge SAYING it saw nothing is a fact about this
            # candidate, while an unparseable reply is a fact about the model.
            return None, ("no_input" if _NO_INPUT_RE.search(raw or "") else "unparseable")
        if verdicts and verdict["keep"] != verdicts[0]["keep"]:
            logger.info("llm judge: split verdict on shard %s after %d samples; "
                        "no adjudication", shard.id, len(verdicts) + 1)
            return None, "split"
        verdicts.append(verdict)

    # Agreed. Average the quality so one outlier rating cannot carry a publish on its own,
    # and keep the first reason — they concur on the verdict, so any of them explains it.
    return {
        "keep": verdicts[0]["keep"],
        "quality": round(sum(v["quality"] for v in verdicts) / len(verdicts), 3),
        "reason": verdicts[0]["reason"],
    }, "ok"


def _llm_judge(db: Session, shard: MemoryShard) -> dict | None:
    """The judge's verdict, or None however it failed (AL-227 / GRPH-348).

    A thin view over `judge_verdict` for callers that only act on a verdict and never
    report why there wasn't one. Anything that shows a human a reason must call
    `judge_verdict` instead — see JUDGE_CAUSES for why the distinction matters.
    """
    return judge_verdict(db, shard)[0]


# Origins whose shards may be auto-REJECTED but never auto-PUBLISHED (GRPH-358).
#
# `agent:auto-extract` is a distillation of an item's own text, produced when the item
# closes. An item's DESCRIPTION is written before the work — it holds the proposal, the
# options weighed, and framing the build then revised — so an extracted lesson can state,
# fluently and specifically, something the shipped code contradicts. That is not
# hypothetical: closing GRPH-354 published *"discovered artifacts inherit the existing
# MEASURABLE_TIERS logic"* on the same day the opposite shipped and was pinned by a test.
#
# The reason this is a hard veto rather than a higher threshold: **no signal the scorer
# reads can detect this class.** The shard is fluent, specific, novel, and perfectly
# consistent with the item it came from — similarity says "not a duplicate", recurrence says
# nothing either way, and an LLM judge asked "is this a good lesson?" says yes, because it
# reads like one. Every input agrees while the claim is false. Raising `_AUTO_ACCEPT_MIN`
# would not have caught it at any value.
#
# Shaped as a VETO deliberately, the same as the distinct-source signal above: it can only
# withhold an accept, never create one, so every other verdict is provably unchanged.
# Auto-REJECT still applies — near-duplicate cleanup is safe here and keeps the queue
# readable, and holding back a publish is not a reason to also stop discarding restatements.
_UNPUBLISHABLE_ORIGINS = ("agent:auto-extract",)


def may_auto_publish(shard: MemoryShard) -> bool:
    """Whether anything may publish this shard without a human (GRPH-358).

    Covers `trusted` mode too, and that is the deliberate part. `trusted` (AL-280) exists so
    an agent can read back what it just wrote inside the same task — an extracted lesson has
    no such consumer, nobody is waiting on it, and it is the one write on that path whose
    source text is known to go stale.
    """
    return not (shard.origin or "").startswith(_UNPUBLISHABLE_ORIGINS)


def triage_candidate(db: Session, shard: MemoryShard) -> MemoryShard:
    """Synchronously score a freshly-written candidate and act on it if the project
    opts in (AL-227): auto-reject near-dups / resembles-rejected, or auto-publish a
    high-confidence corroborated lesson (>= `_AUTO_ACCEPT_MIN`). Records the score on
    the shard plus a system audit event, so every auto-action shows in the "recent
    auto-actions" lane and can be undone.

    When `memory_llm_judge` is on and a real chat model is configured, the model's
    quality assessment refines the accept-side decision (and can auto-reject a
    low-quality note) — but never overrides a structural veto. It degrades to the
    similarity signal when only the offline stub is available.

    Under `trusted` (AL-280) a novel shard publishes on write, so an agent can read
    back what it just wrote. The structural vetoes still run first: `auto_reject`
    is orthogonal to the mode, and trusted-without-dedup would fill the store with
    restatements of one fact.

    **Deliberately does NOT collapse clusters the way `score_candidates` does (GRPH-346).**
    On the write path duplicates are the point: `support` counts sibling candidates, so a
    lesson only becomes evidence by recurring, and rejecting the second occurrence on
    arrival would mean no cluster ever reached two and nothing ever promoted on recurrence
    at all. Collapsing belongs at PROMOTION, where the question is "how many shards should
    this publish", not at arrival, where it is "has this happened before".

    It self-limits without needing the rule: once one member of a cluster publishes, the
    next arrival is a near-duplicate of a PUBLISHED shard and the existing dedup veto
    catches it.

    A no-op returning the shard unchanged when it isn't a candidate, has no embedding,
    or nothing is switched on — so the AL-49 human boundary holds by default for
    anything novel."""
    if shard.status != "candidate" or shard.embedding is None:
        return shard
    mode, auto_reject, llm_judge = _triage_prefs(db, shard.project_id)
    if mode == "review" and not auto_reject:
        return shard  # nothing may act; skip the scoring work entirely

    published = [(s, list(s.embedding)) for s in list_shards(db, project_id=shard.project_id, status="published") if s.embedding is not None]
    rejected = [(s, list(s.embedding)) for s in list_shards(db, project_id=shard.project_id, status="rejected") if s.embedding is not None]
    # Recurrence: the size of the candidate cluster this shard belongs to (AL-50).
    support, distinct_sources = 1, None
    for group in cluster_candidates(db, project_id=shard.project_id):
        if any(s.id == shard.id for s in group):
            support = len(group)
            distinct_sources = _distinct_origins(group)
            break
    human_derived = shard.origin.startswith("user:") or "grill" in shard.origin
    suggestion, confidence, reasons, duplicate_of = _score_shard(
        list(shard.embedding), published, rejected, support, human_derived,
        _corroboration_pool(db, shard.project_id),
        distinct_sources=distinct_sources, correction=_is_correction(shard),
    )
    source = "similarity"

    # LLM enrichment: refine the accept/quality view unless similarity already vetoed
    # it as a structural duplicate / rejected-alike (those stay hard rejects).
    if llm_judge and suggestion != "reject":
        verdict = _llm_judge(db, shard)
        if verdict is not None:
            source = "llm"
            reason = verdict["reason"]
            if verdict["keep"]:
                suggestion = ("accept" if verdict["quality"] >= _JUDGE_PUBLISH_MIN
                              else "review")
                confidence = verdict["quality"]
                reasons = [f"LLM judge: {reason}"] if reason else ["LLM judge rated it publish-worthy"]
            else:
                suggestion = "reject"
                confidence = round(1.0 - verdict["quality"], 3)
                reasons = [f"LLM judge: {reason}"] if reason else ["LLM judge rated it low-quality"]

    # Vetoes win over every accept path, in every mode.
    if suggestion == "reject" and auto_reject:
        action, new_status = "auto_reject_shard", "rejected"
    elif not may_auto_publish(shard):
        return shard  # GRPH-358 — see below; reject above still applies
    elif mode == "trusted":
        # Provenance, not a score: `trusted` marks the shard as published without
        # anyone — human or judge — having assessed it, so a human arriving later
        # can find exactly this set (and AL-282 can exclude it from corroboration).
        action, new_status, source = "trusted_publish_shard", "published", "trusted"
    elif mode == "auto" and suggestion == "accept" and confidence >= _AUTO_ACCEPT_MIN:
        action, new_status = "auto_publish_shard", "published"
    else:
        return shard  # left as a candidate for human review

    shard.status = new_status
    shard.scoring_source = source
    shard.auto_confidence = confidence
    db.commit()
    db.refresh(shard)
    events_svc.record(
        db, actor_type="system", actor_label="memory-auto-triage", surface="system",
        action=action, target_type="shard", target_id=shard.id, project_id=shard.project_id,
        meta={"confidence": confidence, "source": source, "reasons": reasons,
              "duplicate_of": duplicate_of},
    )
    return shard


def auto_triaged_shards(
    db: Session, *, project_id: str | None = None, limit: int = 20
) -> list[MemoryShard]:
    """The "recent auto-actions" lane (AL-227): shards the scorer published or rejected
    without a human, newest first. `scoring_source != ""` marks an auto-action."""
    stmt = select(MemoryShard).where(MemoryShard.scoring_source != "")
    if project_id:
        stmt = stmt.where(MemoryShard.project_id == project_id)
    stmt = stmt.order_by(MemoryShard.created_at.desc()).limit(limit)
    return list(db.scalars(stmt).all())


def undo_triage(db: Session, shard_id: str) -> MemoryShard | None:
    """Undo an auto-action (AL-227): return the shard to the `candidate` queue for
    human review and clear the auto-triage markers so it leaves the auto-actions lane."""
    shard = db.get(MemoryShard, shard_id)
    if shard is None:
        return None
    shard.status = "candidate"
    shard.scoring_source = ""
    shard.auto_confidence = None
    db.commit()
    db.refresh(shard)
    return shard


# --- Agent adjudication of the memory quality gate (AL-282 / PRD-14 D2) --------------
# The product could already let an agent approve its own PRD (`update_prd` takes a
# status) while refusing to let it publish its own memory note. PRD-14 resolves that by
# deciding WHO may operate each gate rather than which features exist.
#
# The asymmetry below is the whole design:
#
#   REJECT is not an escalation. An agent discarding its own candidate removes nothing
#   from the trusted pool, so it may do that directly.
#
#   PUBLISH is an escalation, so an agent never performs it — it SUBMITS the shard and
#   an independent judge decides. `agent_publish` therefore returns the verdict, not an
#   acknowledgement, and a rejected submission is a normal outcome rather than an error.
#
# That is what makes "an agent may hold a quality gate" different from "an agent may
# approve its own work". Without a real chat model the judge cannot run and the shard
# stays a candidate — it degrades to the human boundary, never past it.

class AdjudicationUnavailable(Exception):
    """No independent judge is configured, so nothing can be adjudicated. Raised rather
    than falling back to publishing: silently self-approving is the failure this whole
    path exists to prevent."""


def agent_adjudication_enabled(db: Session, project_id: str | None) -> bool:
    if project_id is None:
        return False  # a project-less shard has no owner to have opted in
    project = db.get(Project, project_id)
    return bool(project and project.agent_adjudication)


def agent_reject(db: Session, shard: MemoryShard, *, origin: str) -> MemoryShard:
    """An agent discards its own candidate. Kept for provenance, never surfaced."""
    shard.status = "rejected"
    shard.scoring_source = "agent"
    db.commit()
    db.refresh(shard)
    events_svc.record(
        db, actor_type="agent", actor_label=origin, surface="mcp",
        action="agent_reject_shard", target_type="shard", target_id=shard.id,
        project_id=shard.project_id, meta={"source": "agent"},
    )
    return shard


def agent_publish(db: Session, shard: MemoryShard, *, origin: str) -> tuple[MemoryShard, dict]:
    """Submit a candidate for independent adjudication. The JUDGE decides; the caller
    does not. Returns (shard, verdict) — `verdict["keep"]` False means the judge rejected
    it, which is a successful call with a negative outcome.

    Raises AdjudicationUnavailable when no real chat model is configured, so an offline
    instance degrades to the human boundary instead of rubber-stamping."""
    verdict, cause = judge_verdict(db, shard)
    if verdict is None:
        # Say WHICH failure (GRPH-351). This read "no independent chat model is configured"
        # for every cause, and reported exactly that about a model that had just judged five
        # other shards in the same run — sending a reader to look for a provider that was
        # present and working, while the real finding (this shard is one the judge cannot
        # decide) stayed invisible.
        raise AdjudicationUnavailable(
            f"{JUDGE_CAUSES.get(cause, cause)}, so this candidate cannot be adjudicated; "
            f"a human publishes it from Memory review"
        )
    keep = verdict["keep"] and verdict["quality"] >= _JUDGE_PUBLISH_MIN
    shard.status = "published" if keep else "rejected"
    shard.scoring_source = "agent"
    shard.auto_confidence = verdict["quality"]
    db.commit()
    db.refresh(shard)
    events_svc.record(
        db, actor_type="agent", actor_label=origin, surface="mcp",
        action="agent_publish_shard" if keep else "agent_reject_shard",
        target_type="shard", target_id=shard.id, project_id=shard.project_id,
        meta={"source": "agent", "confidence": verdict["quality"],
              "reason": verdict.get("reason", ""), "kept": keep},
    )
    return shard, verdict


def set_status(db: Session, shard_id: str, status: str) -> MemoryShard | None:
    """Promote (→published) or reject (→rejected) a candidate shard (AL-49)."""
    if status not in ("candidate", "published", "rejected"):
        raise ValueError(f"invalid shard status: {status}")
    shard = db.get(MemoryShard, shard_id)
    if shard is None:
        return None
    shard.status = status
    db.commit()
    db.refresh(shard)
    return shard


def update_shard(db: Session, shard_id: str, *, text_body: str) -> MemoryShard | None:
    """Edit a shard's text and RE-EMBED it (fixes stale-embedding-after-edit, R-27)."""
    shard = db.get(MemoryShard, shard_id)
    if shard is None:
        return None
    shard.text = text_body
    # An edit is a write too — don't lose the user's text to a down embedder.
    shard.embedding = safe_embed(text_body)
    db.commit()
    db.refresh(shard)
    return shard


def backfill_embeddings(db: Session) -> int:
    """Re-embed every shard with the current provider. Run after switching providers."""
    embedder = get_embedder()
    shards = list(db.scalars(select(MemoryShard)).all())
    for s in shards:
        s.embedding = embedder.embed(s.text)
    db.commit()
    return len(shards)


def export_shards(db: Session, project_id: str | None = None) -> list[dict]:
    out = []
    for s in list_shards(db, project_id=project_id):
        out.append({"text": s.text, "scope": s.scope, "source": s.source, "item_id": s.item_id})
    return out


def import_shards(db: Session, rows: list[dict], project_id: str = "core") -> int:
    for row in rows:
        add_memory(
            db,
            text_body=row["text"],
            scope=row.get("scope", "global"),
            source=row.get("source", ""),
            item_id=row.get("item_id"),
            project_id=project_id,
        )
    return len(rows)


def _vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.8f}" for x in vec) + "]"


def search_memory(
    db: Session, query: str, top_k: int = 5, project_id: str | None = None,
    include_candidates: bool = False,
) -> list[tuple[MemoryShard, float]]:
    """Return (shard, similarity) pairs ranked by cosine similarity, best first.

    The trusted-publication boundary (AL-49): only `published` shards surface by
    default. `include_candidates` also returns unreviewed agent self-reports;
    `rejected` shards never surface."""
    qvec = get_embedder().embed(query)
    allowed = ("published", "candidate") if include_candidates else ("published",)

    if not settings.is_sqlite:
        # pgvector: cosine distance operator `<=>`; similarity = 1 - distance.
        params: dict = {"qv": _vector_literal(qvec), "k": top_k}
        project_clause = ""
        if project_id is not None:
            params["pid"] = project_id
            # Honor share_global_memory: only fold in global (NULL) shards when the
            # project opts in and we're not in hosted mode (AL-71).
            if _includes_global(db, project_id):
                project_clause = "AND (project_id = :pid OR project_id IS NULL)"
            else:
                project_clause = "AND project_id = :pid"
        # Bind the allowed statuses as an IN-list (never surface `rejected`).
        status_names = [f":st{i}" for i in range(len(allowed))]
        for i, st in enumerate(allowed):
            params[f"st{i}"] = st
        sql = text(
            f"""
            SELECT id, (embedding <=> (:qv)::vector) AS distance
            FROM memory_shards
            WHERE embedding IS NOT NULL
              AND status IN ({", ".join(status_names)})
              {project_clause}
            ORDER BY distance ASC
            LIMIT :k
            """
        )
        rows = db.execute(sql, params).all()
        out: list[tuple[MemoryShard, float]] = []
        for row in rows:
            shard = db.get(MemoryShard, row.id)
            if shard is not None:
                out.append((shard, 1.0 - float(row.distance)))
        return out

    # SQLite fallback: cosine in Python over the (small) shard set.
    shards = [s for s in list_shards(db, project_id=project_id) if s.status in allowed]
    scored = [
        (s, cosine_similarity(qvec, s.embedding)) for s in shards if s.embedding is not None
    ]
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored[:top_k]
