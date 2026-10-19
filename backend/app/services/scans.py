"""Code-health scans — agent-reported findings for the Stack overlay (GRPH-1035).

Producer = agent-with-repo (MCP `record_scan`), same shape as `describe_code`. The server
stores + serves; it never runs the scanner. A health badge NEVER defaults to green:

  clean_at_sha  — a scan exists, zero findings, scanned_sha matches head
  has_findings  — a scan exists with findings (at head, or head unknown)
  never_scanned — no scan for this category × component
  stale         — a scan exists but scanned_sha ≠ head (result does not cover what's running)

Ship security end-to-end first. Coverage and open-work are derived from the code graph /
tracker (built-in, no agent producer required). Refactor and stub stay never_scanned until
their own scanners report.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import CodeRef, CodeScan, Item, ScanFinding
from app.services import code_graph as code_svc
from app.services.clustering import _match

# Overlay categories — match docs/design/Stack.dc.html CAT list.
CATEGORIES = ("security", "refactor", "stub", "coverage", "work")
CATEGORY_LABELS = {
    "security": "Security",
    "refactor": "Refactoring · complexity",
    "stub": "Partial implementation",
    "coverage": "Coverage · graph staleness",
    "work": "Open work",
}

SEVERITIES = ("crit", "critical", "high", "med", "medium", "low", "info")
_SEV_NORM = {
    "crit": "crit", "critical": "crit",
    "high": "high",
    "med": "med", "medium": "med",
    "low": "low",
    "info": "low",
}
_SEV_ORDER = ("crit", "high", "med", "low")

# Stack deployable id → repo path prefix (ticket paths; api owns backend/, not "api/").
COMPONENT_PATHS: dict[str, str] = {
    "web": "web/",
    "api": "backend/",
    "fleet": "fleet/",
    "cli": "cli/",
}

# Scanners that roll into the security category when `category` is omitted.
_SECURITY_SCANNERS = frozenset({
    "security", "pip-audit", "bandit", "npm-audit", "osv-scanner", "osv", "semgrep",
    "safety", "trivy",
})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _norm_path(path: str) -> str:
    p = (path or "").strip().lstrip("./")
    if not p:
        raise ValueError("path is required")
    return p


def _norm_sev(raw: str) -> str:
    key = (raw or "medium").strip().lower()
    return _SEV_NORM.get(key, "med")


def _infer_category(scanner_id: str, category: str | None) -> str:
    if category:
        c = category.strip().lower()
        if c not in CATEGORIES:
            raise ValueError(f"category must be one of {list(CATEGORIES)}")
        return c
    sid = (scanner_id or "").strip().lower()
    if sid in _SECURITY_SCANNERS:
        return "security"
    if sid in ("radon", "complexity", "eslint-complexity", "refactor"):
        return "refactor"
    if sid in ("stubscan", "stub", "partial"):
        return "stub"
    if sid in ("graphcheck", "coverage", "code-graph"):
        return "coverage"
    # Default the first shipped producer.
    return "security"


def _under(prefix: str, path: str) -> bool:
    """Is `path` inside component prefix `prefix`?"""
    pref = prefix.rstrip("/")
    p = path.lstrip("./")
    return p == pref or p.startswith(pref + "/") or _match(pref + "/**", p) or _match(prefix, p)


def record_scan(
    db: Session,
    *,
    project_id: str,
    scanner_id: str,
    path: str,
    scanned_sha: str,
    findings: list[dict] | None = None,
    scanner_version: str = "",
    category: str | None = None,
    note: str = "",
    reported_by: str = "",
) -> dict:
    """Upsert a scanner's report for `path` at `scanned_sha`.

    Empty `findings` is a deliberate clean result at that sha — not the same as never
    having scanned. Replaces any prior row for the same (project, scanner_id, path).
    """
    sid = (scanner_id or "").strip()
    if not sid:
        raise ValueError("scanner_id is required")
    path_n = _norm_path(path)
    sha = (scanned_sha or "").strip()
    if not sha:
        raise ValueError(
            "scanned_sha is required — a clean result without a revision cannot be told "
            "apart from a stale one when head moves"
        )
    cat = _infer_category(sid, category)
    rows = list(findings or [])

    # Replace prior result for this scanner+path (one live answer).
    prior = list(db.scalars(
        select(CodeScan).where(
            CodeScan.project_id == project_id,
            CodeScan.scanner_id == sid,
            CodeScan.path == path_n,
        )
    ).all())
    for old in prior:
        db.execute(delete(ScanFinding).where(ScanFinding.scan_id == old.id))
        db.delete(old)

    scan = CodeScan(
        id="cs_" + uuid.uuid4().hex[:12],
        project_id=project_id,
        category=cat,
        scanner_id=sid,
        scanner_version=(scanner_version or "").strip(),
        path=path_n,
        scanned_sha=sha,
        scanned_at=_utcnow(),
        reported_by=(reported_by or "").strip(),
        note=(note or "").strip(),
    )
    db.add(scan)
    stored = 0
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        summary = str(raw.get("summary") or "").strip()
        if not summary:
            continue
        db.add(ScanFinding(
            scan_id=scan.id,
            project_id=project_id,
            path=str(raw.get("path") or path_n).strip() or path_n,
            severity=_norm_sev(str(raw.get("severity") or "medium")),
            summary=summary,
            link_kind=str(raw.get("link_kind") or "").strip(),
            link_ref=str(raw.get("link_ref") or "").strip(),
        ))
        stored += 1
    db.commit()
    db.refresh(scan)
    return {
        "scan_id": scan.id,
        "scanner_id": scan.scanner_id,
        "scanner_version": scan.scanner_version,
        "category": scan.category,
        "path": scan.path,
        "scanned_sha": scan.scanned_sha,
        "findings_recorded": stored,
        "clean": stored == 0,
        "reported_by": scan.reported_by,
        "scanned_at": scan.scanned_at.isoformat(),
    }


def _findings_for(db: Session, scan_id: str) -> list[dict]:
    rows = list(db.scalars(
        select(ScanFinding).where(ScanFinding.scan_id == scan_id)
        .order_by(ScanFinding.id.asc())
    ).all())
    return [
        {
            "severity": f.severity,
            "summary": f.summary,
            "path": f.path,
            "link_kind": f.link_kind or None,
            "link_ref": f.link_ref or None,
        }
        for f in rows
    ]


def _state_for_scan(scan: CodeScan | None, findings: list[dict], head_sha: str) -> dict:
    """Three-state (+ stale) answer. Never defaults to clean."""
    if scan is None:
        return {
            "state": "never_scanned",
            "scanner": None,
            "scanner_version": None,
            "scanned_sha": None,
            "scanned_at": None,
            "findings": [],
            "note": "Nothing has scanned this path. Until something does, its state is unknown.",
        }
    label = scan.scanner_id + (f" {scan.scanner_version}" if scan.scanner_version else "")
    base = {
        "scanner": label,
        "scanner_version": scan.scanner_version or None,
        "scanned_sha": scan.scanned_sha,
        "scanned_at": scan.scanned_at.isoformat() if scan.scanned_at else None,
        "reported_by": scan.reported_by or None,
        "findings": findings,
        "note": scan.note or None,
    }
    head = (head_sha or "").strip()
    if findings:
        # Findings still matter even if the sha drifted — show them, flag stale in note.
        stale = bool(head and scan.scanned_sha != head)
        note = base["note"]
        if stale:
            extra = f"Scanned at {scan.scanned_sha[:12]}; head is {head[:12]}."
            note = f"{note} {extra}".strip() if note else extra
        return {**base, "state": "has_findings", "note": note}
    if not head:
        return {
            **base,
            "state": "stale",
            "note": (base["note"] or "") + (
                " Head revision unknown — a clean result cannot be confirmed at head."
            ).strip(),
        }
    if scan.scanned_sha != head:
        return {
            **base,
            "state": "stale",
            "note": (
                f"0 findings at {scan.scanned_sha[:12]}. Head is {head[:12]}, so that "
                "result does not cover what is running."
            ),
        }
    return {**base, "state": "clean_at_sha"}


def _latest_scans_for_prefix(db: Session, project_id: str, prefix: str) -> list[CodeScan]:
    """Latest scan per (scanner_id) whose path is under or equal to the component prefix."""
    all_scans = list(db.scalars(
        select(CodeScan).where(CodeScan.project_id == project_id)
        .order_by(CodeScan.scanned_at.desc())
    ).all())
    pref = prefix.rstrip("/")
    best: dict[str, CodeScan] = {}
    for s in all_scans:
        if not (s.path == pref or s.path == prefix or s.path.startswith(pref + "/")
                or pref.startswith(s.path.rstrip("/") + "/") or s.path.rstrip("/") == pref):
            # Also accept a scan whose path is exactly the component root with/without slash.
            sp = s.path.rstrip("/")
            if sp != pref and not _under(prefix, s.path) and not _under(s.path, pref):
                continue
        key = f"{s.category}:{s.scanner_id}"
        if key not in best:
            best[key] = s
    return list(best.values())


def _coverage_category(db: Session, project_id: str, prefix: str, head_sha: str) -> dict:
    """Built-in: code-graph health rolled to this component path."""
    h = code_svc.health(db, project_id, limit=80)
    findings: list[dict] = []
    if not h.get("ever_described"):
        return {
            "state": "never_scanned",
            "scanner": "code-graph health",
            "scanner_version": None,
            "scanned_sha": None,
            "scanned_at": None,
            "findings": [],
            "note": "No describe_code pass has run — coverage cannot be judged.",
        }
    for row in h.get("stale_but_claimed") or []:
        paths = [p for p in (row.get("paths") or []) if _under(prefix, p)]
        if paths or _under(prefix, row.get("area") or ""):
            findings.append({
                "severity": "med",
                "summary": (
                    f"{row.get('item')}: open work still claims stale paths "
                    f"{', '.join(paths or [row.get('area') or ''])}"
                ),
                "path": (paths[0] if paths else row.get("area")) or prefix,
                "link_kind": "item",
                "link_ref": row.get("item"),
            })
    for row in h.get("unresolvable") or []:
        area = row.get("area") or ""
        if _under(prefix, area) or any(_under(prefix, a) for a in [area]):
            findings.append({
                "severity": "low",
                "summary": (
                    f"Touchpoint {area!r} resolves to no described node "
                    f"(items: {', '.join(row.get('items') or [])})"
                ),
                "path": area or prefix,
                "link_kind": "item",
                "link_ref": (row.get("items") or [None])[0],
            })
    # Freshness of described nodes under this prefix.
    nodes = [n for n in code_svc.list_nodes(db, project_id) if _under(prefix, n.path)]
    if not nodes:
        return {
            "state": "never_scanned",
            "scanner": "code-graph health",
            "scanner_version": None,
            "scanned_sha": None,
            "scanned_at": None,
            "findings": [],
            "note": f"No code-graph nodes under {prefix}.",
        }
    # Synthetic scan sha = dominant revision among fresh nodes, else "".
    revs = [n.revision for n in nodes if n.revision]
    synth = CodeScan(
        id="derived",
        project_id=project_id,
        category="coverage",
        scanner_id="code-graph",
        scanner_version="",
        path=prefix,
        scanned_sha=revs[0] if revs and all(r == revs[0] for r in revs) else (head_sha or ""),
        scanned_at=_utcnow(),
        reported_by="derived",
        note="",
    )
    return _state_for_scan(synth, findings, head_sha)


def _touches_component(touchpoint: str, prefix: str) -> bool:
    """Does a free-text touchpoint area intersect this component prefix?"""
    tp = (touchpoint or "").strip().lstrip("./")
    if not tp:
        return False
    if _under(prefix, tp):
        return True
    # Glob / directory touchpoints: backend/app/** covers backend/
    pref = prefix.rstrip("/")
    if _match(tp, pref) or _match(tp, prefix):
        return True
    if tp.rstrip("/").startswith(pref) or pref.startswith(tp.rstrip("/")):
        return True
    return False


def _work_category(db: Session, project_id: str, prefix: str) -> dict:
    """Built-in: open CodeRefs + open items whose touchpoints hit this component."""
    findings: list[dict] = []
    seen: set[str] = set()
    refs = list(db.scalars(
        select(CodeRef).where(CodeRef.project_id == project_id)
    ).all())
    open_items = {
        it.key: it for it in items_list(db, project_id)
        if it.status not in ("done",)
    }
    for ref in refs:
        if not _under(prefix, ref.path):
            continue
        it = open_items.get(ref.ref_id) if ref.ref_type == "item" else None
        if ref.ref_type == "item" and ref.ref_id not in open_items:
            continue
        sev = "high" if ref.relation == "fixes" else "low"
        status = it.status if it else "open"
        findings.append({
            "severity": sev,
            "summary": (
                f"{'Defect' if ref.relation == 'fixes' else 'In flight'} "
                f"({ref.relation}): {ref.ref_id}"
                + (f" [{status}]" if it else "")
            ),
            "path": ref.path,
            "link_kind": "item" if ref.ref_type == "item" else "code",
            "link_ref": ref.ref_id,
        })
        seen.add(ref.ref_id)
    for it in open_items.values():
        if it.key in seen:
            continue
        hit = next((tp for tp in (it.touchpoints or []) if _touches_component(tp, prefix)), None)
        if not hit:
            continue
        findings.append({
            "severity": "low",
            "summary": f"In flight: {it.key} ({it.status}) touches {hit}",
            "path": hit,
            "link_kind": "item",
            "link_ref": it.key,
        })
    if not findings:
        return {
            "state": "clean_at_sha",
            "scanner": "tracker",
            "scanner_version": None,
            "scanned_sha": None,
            "scanned_at": None,
            "findings": [],
            "note": f"No open defects or in-flight items mapped to {prefix}.",
            "live": True,
        }
    return {
        "state": "has_findings",
        "scanner": "tracker",
        "scanner_version": None,
        "scanned_sha": None,
        "scanned_at": None,
        "findings": findings,
        "note": None,
        "live": True,
    }


def items_list(db: Session, project_id: str) -> list[Item]:
    from app.services import items as items_svc
    return items_svc.list_items(db, project_id=project_id)


def scan_status(
    db: Session,
    project_id: str,
    *,
    path: str | None = None,
    scanner_id: str | None = None,
    head_sha: str = "",
) -> dict:
    """Read scan state for one path or the whole project.

    When `path` is a component prefix (or omitted), returns the overlay-shaped payload.
    """
    head = (head_sha or "").strip()
    if path:
        path_n = _norm_path(path)
        q = select(CodeScan).where(
            CodeScan.project_id == project_id,
            CodeScan.path == path_n,
        )
        if scanner_id:
            q = q.where(CodeScan.scanner_id == scanner_id.strip())
        scans = list(db.scalars(q.order_by(CodeScan.scanned_at.desc())).all())
        out = []
        for s in scans:
            out.append(_state_for_scan(s, _findings_for(db, s.id), head) | {
                "path": s.path,
                "category": s.category,
                "scanner_id": s.scanner_id,
                "scan_id": s.id,
            })
        if scanner_id and not out:
            return {
                "project_id": project_id,
                "path": path_n,
                "head_sha": head or None,
                "scans": [_state_for_scan(None, [], head) | {
                    "path": path_n, "category": None, "scanner_id": scanner_id,
                }],
            }
        return {
            "project_id": project_id,
            "path": path_n,
            "head_sha": head or None,
            "scans": out,
        }

    return component_health(db, project_id, head_sha=head)


def scans_due(db: Session, project_id: str, *, head_sha: str = "") -> dict:
    """Which component × category pairs need a (re)scan.

    Due = never_scanned or stale for the security category (the one we ship producers for),
    plus coverage when the graph says something is wrong. Refactor/stub due only once a
    scanner has ever reported (otherwise they stay never_scanned without nagging).
    """
    health = component_health(db, project_id, head_sha=head_sha)
    due = []
    for comp in health["components"]:
        for cat_key, cat in (comp.get("categories") or {}).items():
            st = cat.get("state")
            if st in ("never_scanned", "stale"):
                # Only nag security (shipped) and coverage/work (derived, always meaningful).
                if cat_key in ("security", "coverage") or (
                    cat_key in ("refactor", "stub") and st == "stale"
                ):
                    due.append({
                        "component": comp["id"],
                        "path": comp["path"],
                        "category": cat_key,
                        "state": st,
                        "scanned_sha": cat.get("scanned_sha"),
                        "head_sha": health.get("head_sha"),
                    })
    return {
        "head_sha": health.get("head_sha"),
        "due": due,
        "due_count": len(due),
        # Absence of due work is not "all clean" — components with never_scanned
        # security still appear above. An empty list with every security never_scanned
        # would be a bug; the filter includes never_scanned for security.
    }


def _merge_category_scans(
    db: Session, scans: list[CodeScan], category: str, head_sha: str,
) -> dict:
    """Roll several scanner rows in one category into one overlay cell."""
    mine = [s for s in scans if s.category == category]
    if not mine:
        return _state_for_scan(None, [], head_sha)
    # Prefer the worst state: findings > stale > clean. Take newest per scanner, then merge.
    findings: list[dict] = []
    labels: list[str] = []
    shas: set[str] = set()
    latest = max(mine, key=lambda s: s.scanned_at or datetime.min.replace(tzinfo=timezone.utc))
    any_stale = False
    for s in mine:
        labels.append(s.scanner_id + (f" {s.scanner_version}" if s.scanner_version else ""))
        shas.add(s.scanned_sha)
        fs = _findings_for(db, s.id)
        findings.extend(fs)
        st = _state_for_scan(s, fs, head_sha)
        if st["state"] == "stale":
            any_stale = True
    if findings:
        label = " · ".join(dict.fromkeys(labels))
        return {
            "state": "has_findings",
            "scanner": label,
            "scanner_version": None,
            "scanned_sha": latest.scanned_sha,
            "scanned_at": latest.scanned_at.isoformat() if latest.scanned_at else None,
            "reported_by": latest.reported_by or None,
            "findings": findings,
            "note": latest.note or None,
        }
    if any_stale or (head_sha and any(s.scanned_sha != head_sha for s in mine)):
        return _state_for_scan(latest, [], head_sha) | {
            "scanner": " · ".join(dict.fromkeys(labels)),
        }
    return _state_for_scan(latest, [], head_sha) | {
        "scanner": " · ".join(dict.fromkeys(labels)),
    }


def _roll_component(categories: dict[str, dict], path: str, head_sha: str) -> dict:
    """Per-box footer: FINDINGS / STALE / NOT SCANNED / CLEAN — never invent a score."""
    states = {k: (categories[k].get("state") or "never_scanned") for k in CATEGORIES}
    findings = []
    for k in CATEGORIES:
        if states[k] == "has_findings":
            findings.extend(categories[k].get("findings") or [])
    sev_counts: dict[str, int] = {}
    for f in findings:
        sev_counts[f.get("severity") or "med"] = sev_counts.get(f.get("severity") or "med", 0) + 1
    sev_badges = [
        {"severity": s, "count": sev_counts[s]}
        for s in _SEV_ORDER if sev_counts.get(s)
    ]
    stale_n = sum(1 for s in states.values() if s == "stale")
    nr_n = sum(1 for s in states.values() if s == "never_scanned")
    gaps = " · ".join(filter(None, [
        f"{stale_n} stale" if stale_n else "",
        f"{nr_n} not run" if nr_n else "",
    ]))
    if findings:
        worst = next(s for s in _SEV_ORDER if sev_counts.get(s))
        return {
            "label": "FINDINGS",
            "state": "has_findings",
            "worst_severity": worst,
            "severity_badges": sev_badges,
            "meta": gaps or (f"@{head_sha[:7]}" if head_sha else ""),
            "finding_count": len(findings),
        }
    if stale_n:
        return {
            "label": "STALE",
            "state": "stale",
            "worst_severity": None,
            "severity_badges": [],
            "meta": gaps,
            "finding_count": 0,
        }
    if nr_n:
        return {
            "label": "NOT SCANNED",
            "state": "never_scanned",
            "worst_severity": None,
            "severity_badges": [],
            "meta": f"{nr_n} of {len(CATEGORIES)} never run",
            "finding_count": 0,
        }
    return {
        "label": "CLEAN",
        "state": "clean_at_sha",
        "worst_severity": None,
        "severity_badges": [],
        "meta": f"@{head_sha[:7]} = head" if head_sha else "@?",
        "finding_count": 0,
    }


def component_health(db: Session, project_id: str, *, head_sha: str = "") -> dict:
    """Stack overlay payload: one entry per deployable with scannable code."""
    head = (head_sha or "").strip()
    components = []
    for cid, prefix in COMPONENT_PATHS.items():
        scans = _latest_scans_for_prefix(db, project_id, prefix)
        cats: dict[str, dict] = {}
        for cat in CATEGORIES:
            if cat == "coverage":
                cats[cat] = _coverage_category(db, project_id, prefix, head)
            elif cat == "work":
                cats[cat] = _work_category(db, project_id, prefix)
            else:
                cats[cat] = _merge_category_scans(db, scans, cat, head)
        roll = _roll_component(cats, prefix, head)
        components.append({
            "id": cid,
            "path": prefix,
            "head_sha": head or None,
            "rollup": roll,
            "categories": {
                k: {**cats[k], "label": CATEGORY_LABELS[k]} for k in CATEGORIES
            },
        })
    return {
        "project_id": project_id,
        "head_sha": head or None,
        "generated_at": _utcnow().isoformat(),
        "components": components,
        # Explicit: no single health score is fabricated.
        "scored": False,
    }
