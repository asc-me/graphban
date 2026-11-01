"""Recommended rules (GRPH-1160 / PRD-P61 §5): rules a project's own evidence implies.

A suggestion is never a rule. Nothing here writes a rule or changes enforcement, and no copy
claims the server will hold a suggested rule: a value suggestion for a gitops field is
ADVISORY when saved. Computed when the panel opens, cached briefly per project; dismissals
live in the database and outlive the cache. A source that could not be read is reported in
`sources` — an empty list must never read as "nothing to suggest".
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from collections import Counter
from datetime import datetime, timezone

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import CodeNode, Item, Observation, RuleSuggestionDismissal
from app.services import observations as observations_svc
from app.services import platform as platform_svc
from app.services import rule_pack
from app.services import rule_toggles
from app.services import rules as rules_svc
from app.services import stack as stack_svc

logger = logging.getLogger("graphban.rules_recommend")

CACHE_TTL_SECONDS = 60.0
SOURCES = ("stack", "code_graph", "github", "fleet")
KINDS = ("value", "code", "rule", "proposal")
_PR_FETCH_TIMEOUT = 3.0
_MIN_PRS = 3
_TICKET_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d+[:\s]")
#: Fewer matching observations than this is an anecdote, not a pattern (PRD-P63 D5).
_MIN_OBSERVATIONS = 2
#: An unmatched kind needs a few more before it is worth a proposal to the library (D7).
_MIN_PROPOSAL = 3
_EVIDENCE_LINKS = 5
#: A matching observation's weight halves every this many days.
_HALF_LIFE_DAYS = 30.0
_RELEASE_DOC_RE = re.compile(r"(^|/)(release|releasing|release-process|release_process)[^/]*\.md$", re.I)

#: Who the engine is, in the `(actor_type, id, label)` shape a refusal is attributed with. It
#: has no user, so `rule_toggles` refuses it as a writer (PRD-P63 D10): it suggests, a person
#: adopts. `tests/test_rule_toggles.py` hands this identity to every toggle write.
ENGINE_IDENTITY = ("system", "rules_recommend", "Recommended rules")

# project_id -> (computed_at, suggestions, sources). Dismissals and current rule values are
# applied AFTER the cache, so a dismissal or a saved value takes effect immediately.
_cache: dict[str, tuple[float, list[dict], list[dict]]] = {}


def clear_cache(project_id: str | None = None) -> None:
    if project_id is None:
        _cache.clear()
    else:
        _cache.pop(project_id, None)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fingerprint(evidence: list[dict]) -> str:
    raw = "\n".join(sorted(f"{e['text']}|{e['source']}|{e.get('link', '')}" for e in evidence))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _suggestion(id_: str, stage: str, level: str, title: str, why: str, kind: str,
                evidence: list[dict], value: str | None = None, rule_id: str | None = None,
                **extra) -> dict:
    return {
        "id": id_, "stage": stage, "level": level, "title": title, "why": why, "kind": kind,
        "rule_id": rule_id, "value": value, "evidence": evidence,
        "fingerprint": fingerprint(evidence),
        "score": None, "replay": None, "effect": None, "report": None, **extra,
    }


def _source(name: str, state: str, reason: str = "") -> dict:
    return {"source": name, "state": state, "read_at": _now_iso() if state == "read" else None,
            "reason": reason or None}


def _fetch_pr_titles(repo: str) -> list[str]:
    """Titles of the repo's recent pull requests. Raises on any failure; the caller reports it."""
    r = httpx.get(f"https://api.github.com/repos/{repo}/pulls",
                  params={"state": "all", "per_page": 50}, timeout=_PR_FETCH_TIMEOUT,
                  headers={"Accept": "application/vnd.github+json"})
    r.raise_for_status()
    return [str(p.get("title", "")) for p in r.json()]


def _from_stack(db: Session, project_id: str, paths: list[str]) -> tuple[list[dict], dict]:
    try:
        deployables = stack_svc.own_deployables(db, project_id)
    except Exception:  # noqa: BLE001 — a failed read is reported, not swallowed into "none"
        logger.debug("stack unreadable", exc_info=True)
        return [], _source("stack", "not_available", "read_failed")
    if not paths:
        return [], _source("stack", "not_available", "no_code_map")
    out = []
    has_ci = any(p.startswith(".github/workflows/") for p in paths)
    if deployables and not has_ci:
        d = deployables[0]
        out.append(_suggestion(
            "suggest.ci_runs_tests", "merge", "checked", "Run the build in CI",
            "The project ships a deployable but its code map shows no CI workflow.", "code",
            [{"text": f"Deployable in {d['dir'] or '.'}", "source": "stack",
              "link": d["files"][0][0] if d["files"] else ""}]))
    return out, _source("stack", "read")


def _from_code_graph(paths: list[str]) -> tuple[list[dict], dict]:
    if not paths:
        return [], _source("code_graph", "not_available", "no_code_map")
    out = []
    doc = next((p for p in sorted(paths) if "::" not in p and _RELEASE_DOC_RE.search(p)), None)
    if doc:
        out.append(_suggestion(
            "suggest.release_defined_in", "release", "advisory", "Name where releases are defined",
            "The code graph holds a release document. Saved, it is advisory context for agents.",
            "value", [{"text": f"{doc} is in the code graph", "source": "code_graph", "link": doc}],
            value=doc, rule_id="release.release_defined_in"))
    return out, _source("code_graph", "read")


def _from_github(db: Session, project_id: str) -> tuple[list[dict], dict]:
    cfg = platform_svc.get_config(db, project_id)
    if not cfg.github_connected or not cfg.github_repo:
        return [], _source("github", "not_available", "not_connected")
    try:
        titles = _fetch_pr_titles(cfg.github_repo)
    except Exception:  # noqa: BLE001
        logger.debug("github PR titles unreadable", exc_info=True)
        return [], _source("github", "not_available", "fetch_failed")
    out = []
    ticketed = [t for t in titles if _TICKET_RE.match(t)]
    if len(titles) >= _MIN_PRS and len(ticketed) * 2 > len(titles):
        prefix = Counter(t.split("-")[0] for t in ticketed).most_common(1)[0][0]
        out.append(_suggestion(
            "suggest.pr_title_pattern", "merge", "advisory", "Put the item id in PR titles",
            f"{len(ticketed)} of {len(titles)} recent PR titles start with an item id. "
            "Saved, it is advisory context for agents.", "value",
            [{"text": f"{len(ticketed)} of {len(titles)} recent PRs start with `{prefix}-…`",
              "source": "github", "link": f"https://github.com/{cfg.github_repo}/pulls"}],
            value="{item_id}: {slug}", rule_id="gitops.pr_title_pattern"))
    return out, _source("github", "read")


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _matches(o: Observation, m: dict) -> bool:
    """A mapping row names an observation kind and, optionally, `where`: ref keys that must
    equal. Only structured fields are compared; nothing is read from prose (D2)."""
    if o.kind != m.get("observation"):
        return False
    where = m.get("where")
    return not isinstance(where, dict) or all((o.ref or {}).get(k) == v for k, v in where.items())


def _score(rows: list[Observation], weight: float, now: datetime) -> float:
    """Weight times recency-decayed count (D5)."""
    return round(sum(weight * 0.5 ** (max(0.0, (now - _aware(o.occurred_at)).total_seconds())
                                      / 86400 / _HALF_LIFE_DAYS) for o in rows), 2)


def _replay(db: Session, project_id: str, rows: list[Observation]) -> dict | None:
    """How many of the project's signed-off items carry a matching observation. Shaped like
    `harness_rules.replay`: it reports what differs, and does NOT claim the outcome would have
    been better. None when no item has been signed off (nothing to replay against)."""
    signed = db.scalar(select(func.count(Item.id)).where(
        Item.project_id == project_id, Item.reviewed_by.is_not(None))) or 0
    if signed == 0:
        return None
    hit = len({o.item_id for o in rows if o.item_id})
    return {"signed_off": signed, "would_differ": min(hit, signed),
            "text": f"{min(hit, signed)} of the last {signed} signed-off items carry this observation"}


def _evidence(rows: list[Observation]) -> list[dict]:
    """A count line (so one more observation always changes the fingerprint and re-opens a
    dismissal) and the newest few as links. Kind, item id and commit only — never copied text."""
    newest = sorted(rows, key=lambda o: (_aware(o.occurred_at), o.id), reverse=True)
    out = [{"text": f"{len(rows)} observation(s) of {', '.join(sorted({o.kind for o in rows}))}",
            "source": "fleet", "link": ""}]
    for o in newest[:_EVIDENCE_LINKS]:
        sha = f" @ {o.commit[:8]}" if o.commit else ""
        out.append({"text": f"{o.kind} on {o.item_id or 'a project record'}{sha}",
                    "source": "fleet", "link": o.item_id or ""})
    return out


def _fleet_suggestions(db: Session, project_id: str, rows: list[Observation]) -> list[dict]:
    pack = rule_pack.CURRENT
    mappings = pack.mappings if pack else []
    now = datetime.now(timezone.utc)
    out: list[dict] = []
    mapped_ids: set[int] = set()
    by_rule: dict[str, list[dict]] = {}
    for m in mappings:
        if isinstance(m, dict) and isinstance(m.get("rule"), str) and m.get("observation"):
            by_rule.setdefault(m["rule"], []).append(m)
    for rule_id, maps in sorted(by_rule.items()):
        lib = rule_toggles.LIBRARY.get(rule_id)
        hit = {o.id: (o, m) for m in maps for o in rows if _matches(o, m)}
        if lib is None or lib.unmeasured_reason:
            continue  # no library rule that loads whole: handled as unmatched below
        mapped_ids |= set(hit)
        matched = [o for o, _ in hit.values()]
        if len(matched) < _MIN_OBSERVATIONS:
            continue
        weight = max(float(m.get("weight", 1)) for _, m in hit.values())
        replay = _replay(db, project_id, matched) if any(m.get("replay") for _, m in hit.values()) \
            else None
        out.append(_suggestion(
            f"suggest.rule.{rule_id}", lib.stage, lib.enforcement, f"Turn on {lib.title}",
            f"{len(matched)} fleet observation(s) imply this rule. Nothing turns it on but a "
            "person: the toggle is yours.", "rule", _evidence(matched), rule_id=rule_id,
            score=_score(matched, weight, now), replay=replay,
            effect=f"{lib.summary} Turning it on changes what the server "
                   f"{'refuses' if lib.enforcement == 'enforced' else 'reports'} from now on; "
                   "past work is not re-judged."))
    # D7: observations no loadable library rule maps. A proposal, never a rule; the engine files
    # nothing (this box has no route to Graphban's backlogs), so it is an exportable report.
    leftover: dict[str, list[Observation]] = {}
    for o in rows:
        if o.id not in mapped_ids and o.kind != "refusal":
            leftover.setdefault(o.kind, []).append(o)
    for kind, group in sorted(leftover.items()):
        if len(group) < _MIN_PROPOSAL:
            continue
        stage = observations_svc.KINDS.get(kind, "unknown")
        out.append(_suggestion(
            f"propose.{kind}", stage, "advisory", f"Propose a library rule for {kind}",
            f"{len(group)} observation(s) of {kind} match no library rule. This is a proposal "
            "to the library, not a rule: it is exported for Graphban's backlogs, not filed.",
            "proposal", _evidence(group),
            report={"kind": kind, "stage": stage, "count": len(group),
                    "items": len({o.item_id for o in group if o.item_id}),
                    "tag": "rule-library"}))
    return out


def _from_fleet(db: Session, project_id: str) -> tuple[list[dict], dict]:
    """The observations ledger (PRD-63 S1) mapped to library rules by the pack (S3 / D5). An
    empty ledger is `not_available`, not a clean `read` — nothing recorded is not nothing
    happened."""
    try:
        n = observations_svc.count_for_project(db, project_id)
        if n == 0:
            return [], _source("fleet", "not_available", "no_observations")
        rows = list(db.scalars(select(Observation).where(Observation.project_id == project_id)))
        return _fleet_suggestions(db, project_id, rows), _source("fleet", "read", f"{n} observation(s)")
    except Exception:  # noqa: BLE001
        logger.debug("observations unreadable", exc_info=True)
        return [], _source("fleet", "not_available", "read_failed")


def _compute(db: Session, project_id: str) -> tuple[list[dict], list[dict]]:
    paths = list(db.scalars(select(CodeNode.path).where(CodeNode.project_id == project_id)))
    suggestions: list[dict] = []
    sources: list[dict] = []
    for found, src in (_from_stack(db, project_id, paths), _from_code_graph(paths),
                       _from_github(db, project_id), _from_fleet(db, project_id)):
        suggestions += found
        sources.append(src)
    return suggestions, sources


def _held(db: Session, project_id: str) -> set[str]:
    """Rule ids that already have a value — a value suggestion for one is not returned."""
    return {r["id"] for r in rules_svc.registry(db, project_id)
            if r["truth"] == "db" and r["source"] not in (None, "unmeasured")}


def _cached(db: Session, project_id: str) -> tuple[list[dict], list[dict]]:
    hit = _cache.get(project_id)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_SECONDS:
        return hit[1], hit[2]
    suggestions, sources = _compute(db, project_id)
    _cache[project_id] = (time.monotonic(), suggestions, sources)
    return suggestions, sources


def recommended(db: Session, project_id: str) -> dict:
    suggestions, sources = _cached(db, project_id)
    held = _held(db, project_id)
    dismissed = {d.suggestion_id: d.fingerprint for d in db.scalars(
        select(RuleSuggestionDismissal).where(RuleSuggestionDismissal.project_id == project_id))}
    on = {t["rule_id"] for t in rule_toggles.effective(db, project_id) if t["on"]["value"]}
    shown = [s for s in suggestions
             if not (s["kind"] == "value" and s["rule_id"] in held)
             and not (s["kind"] == "rule" and s["rule_id"] in on)
             and dismissed.get(s["id"]) != s["fingerprint"]]
    shown.sort(key=lambda s: -(s["score"] or 0))
    return {"suggestions": shown, "sources": sources}


def dismiss(db: Session, project_id: str, suggestion_id: str) -> bool:
    """Dismiss against the evidence as it stands now. False when no such suggestion exists."""
    current = next((s for s in _cached(db, project_id)[0] if s["id"] == suggestion_id), None)
    if current is None:
        return False
    row = db.get(RuleSuggestionDismissal, (project_id, suggestion_id))
    if row is None:
        db.add(RuleSuggestionDismissal(project_id=project_id, suggestion_id=suggestion_id,
                                       fingerprint=current["fingerprint"]))
    else:
        row.fingerprint = current["fingerprint"]
        row.dismissed_at = datetime.now(timezone.utc)
    db.commit()
    return True


def dismissed(db: Session, project_id: str) -> list[dict]:
    """Suggestions dismissed against their CURRENT evidence — the ones Restore can bring back.
    A dismissal whose evidence has since changed is already showing in the main list."""
    marks = {d.suggestion_id: d.fingerprint for d in db.scalars(
        select(RuleSuggestionDismissal).where(RuleSuggestionDismissal.project_id == project_id))}
    return [s for s in _cached(db, project_id)[0] if marks.get(s["id"]) == s["fingerprint"]]


def restore(db: Session, project_id: str, suggestion_id: str) -> bool:
    """Undo a dismissal. False when that suggestion was not dismissed."""
    row = db.get(RuleSuggestionDismissal, (project_id, suggestion_id))
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True
