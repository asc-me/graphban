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
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CodeNode, RuleSuggestionDismissal
from app.services import platform as platform_svc
from app.services import rules as rules_svc
from app.services import stack as stack_svc

logger = logging.getLogger("graphban.rules_recommend")

CACHE_TTL_SECONDS = 60.0
SOURCES = ("stack", "code_graph", "github")
KINDS = ("value", "code")
_PR_FETCH_TIMEOUT = 3.0
_MIN_PRS = 3
_TICKET_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d+[:\s]")
_RELEASE_DOC_RE = re.compile(r"(^|/)(release|releasing|release-process|release_process)[^/]*\.md$", re.I)

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
                evidence: list[dict], value: str | None = None, rule_id: str | None = None) -> dict:
    return {
        "id": id_, "stage": stage, "level": level, "title": title, "why": why, "kind": kind,
        "rule_id": rule_id, "value": value, "evidence": evidence,
        "fingerprint": fingerprint(evidence),
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


def _compute(db: Session, project_id: str) -> tuple[list[dict], list[dict]]:
    paths = list(db.scalars(select(CodeNode.path).where(CodeNode.project_id == project_id)))
    suggestions: list[dict] = []
    sources: list[dict] = []
    for found, src in (_from_stack(db, project_id, paths), _from_code_graph(paths),
                       _from_github(db, project_id)):
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
    shown = [s for s in suggestions
             if not (s["kind"] == "value" and s["rule_id"] in held)
             and dismissed.get(s["id"]) != s["fingerprint"]]
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
