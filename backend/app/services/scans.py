"""Code-health scans — agent-reported findings for the Stack overlay (GRPH-1035).

Producer = agent-with-repo (MCP `record_scan`), same shape as `describe_code`. The server
stores + serves; it never runs the scanner. A health badge NEVER defaults to green:

  clean_at_sha  — a scan exists, zero findings, scanned_sha matches head
  has_findings  — a scan exists with findings (at head, or head unknown)
  never_scanned — no scan for this category × component
  stale         — a scan exists but scanned_sha ≠ head (result does not cover what's running)

Ship security end-to-end first. Coverage and open-work are derived from the code graph /
tracker (built-in, no agent producer required). Refactor, stub and tests stay never_scanned
until their own scanners report.

Tests (GRPH-1061) is one result per component path at a commit from a test runner (pytest,
vitest, jest, any JUnit producer). It must carry `tests={passed, failed, skipped}`: without
counts an empty findings list cannot be told apart from a suite that collected nothing. Each
failing test is a finding; a run that executed zero tests is stored WITH a finding, never as
clean; skips are written into the note rather than folded into the passes. The counts live
in the note, not a column — a reader sees them where the scan's other context already is.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import CodeRef, CodeScan, Item, ScanFinding
from app.services import code_graph as code_svc
from app.services.clustering import _match

logger = logging.getLogger("graphban.scans")

# Overlay categories — match docs/design/Stack.dc.html CAT list.
CATEGORIES = ("security", "refactor", "stub", "deadcode", "coverage", "tests", "work")
CATEGORY_LABELS = {
    "security": "Security",
    "refactor": "Refactoring · complexity",
    "stub": "Partial implementation",
    "deadcode": "Dead code · unused",
    "coverage": "Coverage · graph staleness",
    "tests": "Tests",
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

# Unused functions, exports and files (GRPH-1060): vulture for Python, knip/ts-prune for TS/JS.
_DEADCODE_SCANNERS = frozenset({"deadcode", "vulture", "knip", "ts-prune"})
# Test runners that roll into the tests category when `category` is omitted.
_TEST_SCANNERS = frozenset({"pytest", "vitest", "jest", "junit", "tests"})
_REFACTOR_SCANNERS = frozenset({"radon", "complexity", "eslint-complexity", "refactor"})
_STUB_SCANNERS = frozenset({"stubscan", "stub", "partial"})
_COVERAGE_SCANNERS = frozenset({"graphcheck", "coverage", "code-graph"})

# scanner_id → category when `category` is omitted. There is no fallback: an unknown
# scanner with no category is refused, because any default category lets one empty row
# paint that cell clean at head (GRPH-1108).
_INFERRED_CATEGORY: tuple[tuple[str, frozenset[str]], ...] = (
    ("security", _SECURITY_SCANNERS),
    ("refactor", _REFACTOR_SCANNERS),
    ("stub", _STUB_SCANNERS),
    ("deadcode", _DEADCODE_SCANNERS),
    ("coverage", _COVERAGE_SCANNERS),
    ("tests", _TEST_SCANNERS),
)
_TEST_COUNTS = ("passed", "failed", "skipped")


# Tag prefix on an item filed from the overlay (a finding's Defrag, a stale/never-run CTA).
# The tag is how the overlay knows the work was already filed — without it a reload offers
# the same button again and the second click files a duplicate (GRPH-1044).
FILED_TAG_PREFIX = "stack-filed:"


def filed_key(*parts: str) -> str:
    """Stable tag for one overlay offer: same finding (or same gap) → same key."""
    raw = "|".join((p or "").strip() for p in parts)
    return FILED_TAG_PREFIX + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _norm_path(path: str) -> str:
    # removeprefix, not lstrip: lstrip("./") eats any leading '.' or '/' chars, so
    # '.github/workflows/ci.yml' becomes 'github/workflows/ci.yml'.
    p = (path or "").strip().removeprefix("./")
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
    for cat, scanners in _INFERRED_CATEGORY:
        if sid in scanners:
            return cat
    known = "; ".join(f"{cat}: {', '.join(sorted(ids))}" for cat, ids in _INFERRED_CATEGORY)
    raise ValueError(
        f"category is required for scanner_id {scanner_id!r} — it is not a known scanner, "
        f"so its category cannot be inferred. Pass category, one of {list(CATEGORIES)}. "
        f"Known scanners ({known})."
    )


def _under(prefix: str, path: str) -> bool:
    """Is `path` inside component prefix `prefix`?"""
    pref = prefix.rstrip("/")
    p = path.removeprefix("./")
    return p == pref or p.startswith(pref + "/") or _match(pref + "/**", p) or _match(prefix, p)


def _covers_component_root(scan_path: str, prefix: str) -> bool:
    """True when a scan covers the whole component, not only a nested file.

    `clean_at_sha` for a component requires a clean result at the component root
    (path == prefix) or a parent of it. A clean nested path (one file under backend/)
    says nothing about the rest of the tree — that must not roll up as CLEAN.
    """
    pref = prefix.rstrip("/")
    sp = (scan_path or "").strip().removeprefix("./").rstrip("/")
    if not sp:
        # Repo-root scan covers every component.
        return True
    if sp == pref:
        return True
    # Parent of the component root (e.g. scan path "" already handled; "backend" vs "backend/app").
    return pref.startswith(sp + "/")


def _test_counts(raw: object) -> dict[str, int]:
    """Validate a tests scan's {passed, failed, skipped}. Every count is required.

    A missing `skipped` defaulted to 0 would fold skips into a clean run, and a missing
    `passed` cannot be told from zero executed — so nothing here has a default.
    """
    if not isinstance(raw, dict):
        raise ValueError(
            "a tests scan requires tests={passed, failed, skipped} — without counts an "
            "empty findings list cannot be told apart from a suite that ran nothing"
        )
    out: dict[str, int] = {}
    for k in _TEST_COUNTS:
        v = raw.get(k)
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ValueError(f"tests.{k} must be a non-negative integer (got {v!r})")
        out[k] = v
    return out


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
    tests: dict | None = None,
) -> dict:
    """Upsert a scanner's report for `path` at `scanned_sha`.

    Empty `findings` is a deliberate clean result at that sha — not the same as never
    having scanned. Replaces any prior row for the same (project, scanner_id, path).
    A tests-category scan also needs `tests` counts (see the module docstring).
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

    # Validate every finding before any write. Silently skipping a summary-less
    # (or non-dict) entry and then counting the scan clean turns a mis-keyed
    # adapter payload into a green cell — absence must never read as clean.
    for i, raw in enumerate(rows):
        if not isinstance(raw, dict):
            raise ValueError(
                f"findings[{i}] must be an object with a non-empty summary; "
                f"got {type(raw).__name__}"
            )
        summary = str(raw.get("summary") or "").strip()
        if not summary:
            raise ValueError(
                f"findings[{i}] requires a non-empty summary "
                f"(got keys {sorted(raw.keys())})"
            )

    counts: dict[str, int] | None = None
    default_sev = "medium"
    if cat == "tests":
        counts = _test_counts(tests)
        if len(rows) < counts["failed"]:
            # A dropped failure would leave fewer findings than failures — at the limit,
            # failed=3 with no findings stores as a clean run.
            raise ValueError(
                f"tests.failed is {counts['failed']} but only {len(rows)} finding(s) were "
                "given — every failing test must be a finding (summary = test id, path = file)"
            )
        if counts["passed"] + counts["failed"] == 0:
            # Green with nothing in it is the absence-reads-as-clean defect: record the
            # empty run as a finding so the cell cannot render clean.
            rows.append({
                "severity": "high",
                "summary": (
                    f"0 tests executed ({counts['skipped']} skipped) — an empty run is not a pass"
                ),
                "path": path_n,
            })
        tally = (
            f"{counts['passed']} passed · {counts['failed']} failed · "
            f"{counts['skipped']} skipped"
        )
        note = f"{tally}. {note.strip()}" if (note or "").strip() else tally
        default_sev = "high"
    elif tests is not None:
        raise ValueError(f"tests counts are only for the tests category, not {cat}")

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
        summary = str(raw.get("summary") or "").strip()
        db.add(ScanFinding(
            scan_id=scan.id,
            project_id=project_id,
            path=str(raw.get("path") or path_n).strip() or path_n,
            severity=_norm_sev(str(raw.get("severity") or default_sev)),
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
        "tests": counts,
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


def _scans_for_prefix(db: Session, project_id: str, prefix: str) -> list[CodeScan]:
    """Every live scan row whose path is under or equal to the component prefix.

    Upsert already keeps one row per (scanner_id, path). Do not collapse to the latest
    per scanner: a clean nested-path scan must not hide findings recorded on a parent.
    """
    all_scans = list(db.scalars(
        select(CodeScan).where(CodeScan.project_id == project_id)
        .order_by(CodeScan.scanned_at.desc())
    ).all())
    pref = prefix.rstrip("/")
    out: list[CodeScan] = []
    for s in all_scans:
        if not (s.path == pref or s.path == prefix or s.path.startswith(pref + "/")
                or pref.startswith(s.path.rstrip("/") + "/") or s.path.rstrip("/") == pref):
            # Also accept a scan whose path is exactly the component root with/without slash.
            sp = s.path.rstrip("/")
            if sp != pref and not _under(prefix, s.path) and not _under(s.path, pref):
                continue
        out.append(s)
    return out


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
    # Synthetic scan sha = the shared revision when every described node agrees.
    # Mixed revisions are partly stale — must not substitute head_sha (that reads
    # as clean_at_sha while a uniformly-old graph correctly reads stale).
    revs = [n.revision for n in nodes if n.revision]
    if revs and all(r == revs[0] for r in revs):
        synth_sha = revs[0]
    elif revs:
        synth_sha = next((r for r in revs if r != (head_sha or "")), revs[0])
    else:
        synth_sha = ""
    synth = CodeScan(
        id="derived",
        project_id=project_id,
        category="coverage",
        scanner_id="code-graph",
        scanner_version="",
        path=prefix,
        scanned_sha=synth_sha,
        scanned_at=_utcnow(),
        reported_by="derived",
        note="",
    )
    return _state_for_scan(synth, findings, head_sha)


def _touches_component(touchpoint: str, prefix: str) -> bool:
    """Does a free-text touchpoint area intersect this component prefix?

    Path-boundary aware: 'backendish/x' does not touch 'backend/', and a bare
    'w' does not touch 'web/' — bare startswith would over-report both.
    """
    tp = (touchpoint or "").strip().removeprefix("./")
    if not tp:
        return False
    if _under(prefix, tp):
        return True
    # Glob / directory touchpoints: backend/app/** covers backend/
    pref = prefix.rstrip("/")
    if _match(tp, pref) or _match(tp, prefix):
        return True
    tp_n = tp.rstrip("/")
    # Touchpoint is the component, or a parent directory that covers it.
    if tp_n == pref or pref.startswith(tp_n + "/"):
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
    plus coverage when the graph says something is wrong. Refactor/stub/deadcode/tests due only once a
    scanner has ever reported (otherwise they stay never_scanned without nagging).
    """
    health = component_health(db, project_id, head_sha=head_sha)
    due = []
    for comp in health["components"]:
        for cat_key, cat in (comp.get("categories") or {}).items():
            st = cat.get("state")
            if st in ("never_scanned", "stale"):
                # Nag security/coverage whenever unscanned or stale; refactor/stub/deadcode only
                # once stale (a scanner has reported before).
                if cat_key in ("security", "coverage") or (
                    cat_key in ("refactor", "stub", "deadcode", "tests") and st == "stale"
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
    *, prefix: str,
) -> dict:
    """Roll several scanner rows in one category into one overlay cell.

    `clean_at_sha` only when at least one clean scan at head covers the component
    root (or a parent of it). Nested-only cleans are partial coverage → stale.
    """
    mine = [s for s in scans if s.category == category]
    if not mine:
        return _state_for_scan(None, [], head_sha)
    # Prefer the worst state: findings > stale > clean. Merge every live row under the prefix.
    findings: list[dict] = []
    labels: list[str] = []
    shas: set[str] = set()
    latest = max(mine, key=lambda s: s.scanned_at or datetime.min.replace(tzinfo=timezone.utc))
    any_stale = False
    root_clean_at_head = False
    for s in mine:
        labels.append(s.scanner_id + (f" {s.scanner_version}" if s.scanner_version else ""))
        shas.add(s.scanned_sha)
        fs = _findings_for(db, s.id)
        findings.extend(fs)
        st = _state_for_scan(s, fs, head_sha)
        if st["state"] == "stale":
            any_stale = True
        if (
            not fs
            and head_sha
            and s.scanned_sha == head_sha
            and _covers_component_root(s.path, prefix)
        ):
            root_clean_at_head = True
    label = " · ".join(dict.fromkeys(labels))
    if findings:
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
        return _state_for_scan(latest, [], head_sha) | {"scanner": label}
    if not root_clean_at_head:
        # Nested cleans at head without a root-covering scan: partial coverage ≠ CLEAN.
        note = (
            f"Scans under {prefix.rstrip('/')}/ do not cover the component root — "
            "partial coverage is not clean_at_sha."
        )
        return {
            "state": "stale",
            "scanner": label,
            "scanner_version": None,
            "scanned_sha": latest.scanned_sha,
            "scanned_at": latest.scanned_at.isoformat() if latest.scanned_at else None,
            "reported_by": latest.reported_by or None,
            "findings": [],
            "note": note,
        }
    return _state_for_scan(latest, [], head_sha) | {"scanner": label}


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


def _filed_items(db: Session, project_id: str) -> dict[str, str]:
    """filed-key tag → item key, for every item the overlay has already filed."""
    out: dict[str, str] = {}
    for it in items_list(db, project_id):
        for t in it.tags or []:
            if isinstance(t, str) and t.startswith(FILED_TAG_PREFIX):
                out.setdefault(t, it.key)
    return out


def _attach_offers(
    categories: dict[str, dict], component: str, prefix: str, filed: dict[str, str],
) -> None:
    """Give each finding and each stale/never-run cell the key its offer files under.

    A finding gets `filed_key` (+ `filed_item` once filed) so the UI can offer Defrag. A
    stale cell offers "re-run at head", a never-scanned one "add a scanner"; the stale
    key carries the scanned sha, so the next time the cell goes stale it is offered again.
    Open-work findings ARE tracker items already — filing an item about an item is noise,
    so they get no key.
    """
    for cat, cell in categories.items():
        if cat != "work":
            for f in cell.get("findings") or []:
                k = filed_key(component, cat, f.get("path") or "", f.get("summary") or "")
                f["filed_key"] = k
                f["filed_item"] = filed.get(k)
        st = cell.get("state")
        if cell.get("live") or st not in ("stale", "never_scanned"):
            cell["cta"] = None
            continue
        kind = "rerun" if st == "stale" else "add_scanner"
        k = filed_key(component, cat, kind, cell.get("scanned_sha") or "")
        cell["cta"] = {"kind": kind, "path": prefix, "filed_key": k, "filed_item": filed.get(k)}


def _component_paths(db: Session, project_id: str) -> dict[str, str]:
    """Graphban's fixed components, plus one per deployable the project's own code map
    describes (`app:<dir>` → `<dir>/`), so its scans land on its own boxes. A repo-root
    deployable gets none: an empty prefix would claim every scan in the project."""
    from app.services import stack as stack_svc

    paths = dict(COMPONENT_PATHS)
    try:
        owner = stack_svc.source_ownership(db, project_id)
        for dep in stack_svc.own_deployables(db, project_id, owner):
            if dep["dir"]:
                paths[stack_svc.own_node_id(dep["dir"])] = dep["dir"] + "/"
    except Exception:  # noqa: BLE001 — derived components are additive
        logger.debug("own deployables unavailable for health", exc_info=True)
    return paths


def component_health(db: Session, project_id: str, *, head_sha: str = "") -> dict:
    """Stack overlay payload: one entry per deployable with scannable code."""
    head = (head_sha or "").strip()
    filed = _filed_items(db, project_id)
    components = []
    for cid, prefix in _component_paths(db, project_id).items():
        scans = _scans_for_prefix(db, project_id, prefix)
        cats: dict[str, dict] = {}
        for cat in CATEGORIES:
            if cat == "coverage":
                cats[cat] = _coverage_category(db, project_id, prefix, head)
            elif cat == "work":
                cats[cat] = _work_category(db, project_id, prefix)
            else:
                cats[cat] = _merge_category_scans(db, scans, cat, head, prefix=prefix)
        _attach_offers(cats, cid, prefix, filed)
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
