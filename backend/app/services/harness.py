"""What a finished delegation says about the harness that ran it (PRD-38).

PRD-37 gave the supervisor a way to choose a harness and explain the choice, and gave the
ledger one bit per attempt to judge it by: signed off, or not. This module is the record that
makes that bit comparable — what kind of work the attempt was, who ran it, how it ended, and
**how it came to be sampled**, which is the field the rest of the design leans on. PRD-35
named the bias and PRD-37 sharpened it: the harness a profile prefers gets the samples, so a
rate over those samples measures the preference unless the record says where they came from.

Two writers, no ordering between them (D3):

- **The server derives** at the outcome event, from what the ledger already holds.
- **The supervisor posts** what only it can see — the resolution before the child starts, the
  binary version and turn count and tokens after it exits.

Either half may arrive first and neither waits for the other. What has arrived is legible from
`derived_at` and `reported_at`, so a number that nobody reported reads as "not reported"
instead of as a zero — the distinction this repository keeps having to relearn.

Only FINISHED delegations keep a row (D1). A launch post whose child never claimed anything is
swept: an attempt that never ran teaches nothing about the harness, and counting it would put
the supervisor's failures in the harness's column.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (Agent, AttemptTelemetry, Delegation, Enrolment,
                        HarnessRollup, Item)

logger = logging.getLogger(__name__)

#: D2. `other` is not a failure of the mapper, it is the mapper's honest output for a reason it
#: does not recognise, and the page shows its count so the coverage is a number rather than a
#: silent default.
BOUNCE_CATEGORIES = ("tests", "scope", "quality", "process", "other")

#: Matched against the LOWERCASED bounce reason, first hit wins. English only and deliberately
#: so: there is no language detection here, and a reason this misses is `other` rather than a
#: guess. Ordered by how SPECIFIC the word is to a category rather than by how common it is:
#: the artifact words go first, so "wrong branch" is process while a bare "wrong" is the
#: judgement word that quality catches. The order is a defensible reading of English, not a
#: measurement, which is the whole reason nothing downstream reads this field.
_BOUNCE_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("tests", ("test", "spec", "suite", "coverage", "ci ", "ci fail", "red build", "lint")),
    ("process", ("branch", "commit", "pr ", "pull request", "worktree", "merge", "conflict",
                 "evidence", "checklist", "docs", "migration")),
    ("scope", ("scope", "out of scope", "unrelated", "extra change", "not asked", "missing",
               "incomplete", "did not implement", "half")),
    ("quality", ("quality", "bug", "wrong", "incorrect", "broken", "regression", "unsafe",
                 "race", "leak", "naming", "readab")),
)

#: D2's size band. Touchpoints and description length are the only difficulty proxies the
#: server has; both are what the delegator wrote, not a judgement the server invented.
SIZE_S_TOUCHPOINTS, SIZE_S_CHARS = 2, 600
SIZE_L_TOUCHPOINTS, SIZE_L_CHARS = 6, 2400

#: D9. The resolver reads a trailing window so a stale cell ages out instead of anchoring a
#: choice forever. A constant, not a setting: every explanation would otherwise carry a
#: parameter, and the explanation is the feature.
WINDOW_DAYS = 90

#: D6. A constant, not a setting: every card that cites a miss says the same window.
#: Criterion 6's sabotage is widening this to 15 — a fixture at 14 days 12 hours must
#: stay a non-event.
REVIEW_WINDOW_DAYS = 14
#: Withdrawal recomputes F cells forward and back this far; beyond it the miss stands.
WITHDRAWAL_DAYS = 90

SAMPLED = ("first_choice", "fallback", "explicit", "unknown", "probe")
REVIEW_KINDS = ("miss", "false_bounce", "confirmed", "withdrawn")
PROBE_TRIGGERS = ("new_row", "version_change")
#: D11's contribution field set. Criterion 31 adds `probe` (the sampling count) and
#: nothing else — no path, item, reviewer or repository name.
CONTRIBUTION_KEYS = frozenset({
    "capability", "size_band", "vendor", "model", "binary_version", "week",
    "finished", "signed_off",
    "first_choice", "fallback", "explicit", "unknown", "probe",
})


class AttemptRefused(Exception):
    """A post the server will not take. `status` is the HTTP code the router raises."""

    def __init__(self, message: str, *, status: int = 404) -> None:
        super().__init__(message)
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def bounce_category(reason: str | None) -> str:
    """Total into `BOUNCE_CATEGORIES`. Never null for a bounce, never a guess.

    Nothing downstream reads this — `sampled` comes from the launch post, every rate is
    signed-off over finished, and no recommendation rule takes it as input. It is a breakdown
    for a person reading a cell, and it is stated here so that no threshold is later wired to
    a field whose input is free text a reviewer typed in a hurry.
    """
    text = (reason or "").strip().lower()
    if not text:
        return "other"
    for category, words in _BOUNCE_WORDS:
        if any(word in text for word in words):
            return category
    return "other"


def size_band(item: Item | None) -> str:
    """`S`, `M` or `L` from what the delegator wrote down. A proxy, and named as one."""
    if item is None:
        return "M"
    touchpoints = len(item.touchpoints or [])
    chars = len(item.description or "")
    if touchpoints >= SIZE_L_TOUCHPOINTS or chars >= SIZE_L_CHARS:
        return "L"
    if touchpoints <= SIZE_S_TOUCHPOINTS and chars <= SIZE_S_CHARS:
        return "S"
    return "M"


# ---- PRD-41 S1: the capability set (D1–D4) ---------------------------------------------------

#: Six families, twenty-four leaves. An id never changes meaning once shipped. Attempts that
#: match no leaf land in `other`, which is a family of its own — not a silent default.
CAPABILITY_LEAVES: tuple[str, ...] = (
    "A1", "A2", "A3", "A4", "A5",
    "B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8",
    "C1", "C2", "C3",
    "H1", "H2", "H3", "H4", "H5",
    "E1", "E2", "E3",
    "F1", "F2", "F3",
)
FAMILIES: dict[str, tuple[str, ...]] = {
    "A": ("A1", "A2", "A3", "A4", "A5"),
    "B": ("B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8"),
    "C": ("C1", "C2", "C3"),
    "H": ("H1", "H2", "H3", "H4", "H5"),
    "E": ("E1", "E2", "E3"),
    "F": ("F1", "F2", "F3"),
    "other": (),
}
CAPABILITY_LABELS: dict[str, str] = {
    "A": "Change shape", "A1": "Localised fix", "A2": "Feature slice",
    "A3": "Behaviour-preserving refactor", "A4": "Schema and migration",
    "A5": "Deletion and cleanup",
    "B": "Layer", "B1": "REST and contract surface", "B2": "MCP tool surface",
    "B3": "Service and domain logic", "B4": "Persistence and queries",
    "B5": "React component and state", "B6": "Visual fidelity",
    "B7": "CLI, process and git", "B8": "Infra, CI and config",
    "C": "Verification behaviour", "C1": "Discriminating tests",
    "C2": "Red-to-green loop", "C3": "Reproduce first",
    "H": "Reasoning demand", "H1": "Concurrency and idempotency",
    "H2": "Authorization boundaries", "H3": "Cross-engine semantics",
    "H4": "Long-context coherence", "H5": "Spec fidelity",
    "E": "Operating the loop", "E1": "Protocol compliance",
    "E2": "Commit hygiene", "E3": "Prose and docs",
    "F": "Review competence", "F1": "Bounce precision", "F2": "Bounce recall",
    "F3": "Reason quality",
    "other": "other",
}
#: PRD-41 D3: history is not dropped. `general` was the mapper saying "none of the four"
#: and becomes the family `other`, which is the same claim.
TASK_CLASS_TO_CAPABILITY = {
    "migration": "A4",
    "mcp_tool": "B2",
    "frontend": "B5",
    "docs": "E3",
    "general": "other",
}
FAMILY_OTHER = "other"

_TEST_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec)(/|$)|(^|/)test_[^/]+$|_test\.(py|ts|tsx|js)$|\.test\.(ts|tsx|js)$|\.spec\.(ts|tsx|js)$",
    re.I,
)
_H1_RE = re.compile(r"lease|claim|idempoten|ttl|heartbeat", re.I)
_STATE_FILES = (
    "agents.md", "claude.md", ".gbfleet-instruction", ".gitignore", ".cursor/mcp.json",
    ".grok/config.toml", ".grok/mcp.json",
)


def capability_catalog() -> dict:
    """The enum and family map served on GET /api/harness, so the page never hard-codes it."""
    return {
        "leaves": list(CAPABILITY_LEAVES),
        "families": {k: list(v) for k, v in FAMILIES.items()},
        "labels": dict(CAPABILITY_LABELS),
    }


def family_of(capability: str) -> str:
    if capability in FAMILIES:
        return capability
    if capability and capability[0] in FAMILIES and capability in CAPABILITY_LEAVES:
        return capability[0]
    return FAMILY_OTHER


def _norm_path(path: str) -> str:
    p = path.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _is_test_path(path: str) -> bool:
    return bool(_TEST_RE.search(_norm_path(path)))


def _is_bug(item) -> bool:
    tags = getattr(item, "tags", None) or []
    return any(isinstance(t, str) and t.strip().lower() == "bug" for t in tags)


def _paths_of(item, diff_shape: dict | None) -> list[str]:
    paths: list[str] = []
    if item is not None:
        paths.extend(t for t in (getattr(item, "touchpoints", None) or []) if isinstance(t, str))
    shape = diff_shape or {}
    for key in ("paths", "added", "modified", "deleted", "renamed"):
        vals = shape.get(key) or []
        if isinstance(vals, list):
            paths.extend(p for p in vals if isinstance(p, str))
    return [_norm_path(p) for p in paths if p and p.strip()]


def _layer_of(path: str) -> str | None:
    p = _norm_path(path)
    if "/routers/" in f"/{p}/" or p.endswith("docs/api-reference.md") or p == "docs/api-reference.md":
        return "B1"
    if p.endswith("mcp_server.py") or p.endswith("tool_tiers.py"):
        return "B2"
    if "/services/" in f"/{p}/" and p.endswith(".py"):
        return "B3"
    if "/models/" in f"/{p}/" or "models/__init__" in p or "alembic/versions" in p:
        return "B4"
    if p.startswith("web/src/features") or p.startswith("web/src/lib"):
        return "B5"
    if p.startswith("web/src/components/ui") or p.endswith(".css"):
        return "B6"
    if p.startswith("fleet/src/gbfleet/") or p.startswith("cli/") or p.startswith("fleet/"):
        return "B7"
    if p.startswith(".github/") or p.startswith("docker") or p.startswith("pyproject"):
        return "B8"
    return None


def _layers_of(paths: list[str]) -> list[str]:
    found: list[str] = []
    for path in paths:
        layer = _layer_of(path)
        if layer and layer not in found:
            found.append(layer)
    return found


def _outcome_dict(outcome) -> dict:
    if outcome is None:
        return {}
    if isinstance(outcome, str):
        return {"outcome": outcome}
    return dict(outcome)


def capabilities(item, diff_shape: dict | None = None, outcome=None) -> list[str]:
    """The set of §5 ids this attempt exercised (D1, D2).

    Beside `size_band` on purpose: both are derived from what the ledger already holds plus
    the supervisor's diff, with no LLM and no hand label. Returns a SET — a hybrid file or a
    slice that touches two layers tags every matching leaf, and a primary-label derivation
    would empty the others (criterion 1 sabotage). No match is `other`, never an empty list:
    an absence that read as "no cell" would be the quiet wrong answer.
    """
    shape = diff_shape if isinstance(diff_shape, dict) else {}
    outc = _outcome_dict(outcome)
    paths = _paths_of(item, shape)
    added = [_norm_path(p) for p in (shape.get("added") or []) if isinstance(p, str)]
    deleted = [_norm_path(p) for p in (shape.get("deleted") or []) if isinstance(p, str)]
    modified = [_norm_path(p) for p in (shape.get("modified") or []) if isinstance(p, str)]
    renamed = [_norm_path(p) for p in (shape.get("renamed") or []) if isinstance(p, str)]
    files_added = int(shape["files_added"]) if shape.get("files_added") is not None else len(added)
    files_deleted = int(shape["files_deleted"]) if shape.get("files_deleted") is not None else len(deleted)
    files_renamed = int(shape["files_renamed"]) if shape.get("files_renamed") is not None else len(renamed)
    test_files = int(shape["test_files"]) if shape.get("test_files") is not None else sum(
        1 for p in paths if _is_test_path(p))
    net_lines = shape.get("net_lines")
    layers = list(shape.get("layers") or []) or _layers_of(added or paths)
    evidence = list(outc.get("evidence") or (getattr(item, "evidence", None) or []))
    bounced = (outc.get("outcome") or "") == "bounced"
    bounce_cat = outc.get("bounce_category")
    hits: list[str] = []

    def add(cap: str) -> None:
        if cap not in hits:
            hits.append(cap)

    non_test = [p for p in (added + modified + deleted + renamed or paths) if not _is_test_path(p)]
    if _is_bug(item) and len(set(non_test)) <= 2 and test_files >= 1:
        add("A1")
    added_layers = _layers_of(added) if added else [L for L in layers if files_added]
    if len(set(added_layers)) >= 2:
        add("A2")
    tests_touched = any(_is_test_path(p) for p in (added + modified + deleted))
    if files_renamed >= 1 and not tests_touched:
        add("A3")
    if any("alembic/versions" in p or p.endswith("models/__init__.py") or "models/__init__" in p
           for p in paths):
        add("A4")
    if net_lines is not None and net_lines < 0 and files_deleted >= 1:
        add("A5")
    elif files_deleted >= 1 and (net_lines is None) and (files_added + len(modified)) == 0:
        add("A5")

    if any("/routers/" in f"/{p}/" or p.endswith("docs/api-reference.md") or p == "docs/api-reference.md"
           for p in paths):
        add("B1")
    if any(p.endswith("mcp_server.py") or p.endswith("tool_tiers.py") for p in paths):
        add("B2")
    service_py = [p for p in paths if "/services/" in f"/{p}/" and p.endswith(".py")]
    other_layers = {L for L in _layers_of(paths) if L != "B3"}
    if service_py and not other_layers:
        add("B3")
    if any("/models/" in f"/{p}/" or "models/__init__" in p for p in paths):
        add("B4")
    if any(p.startswith("web/src/features") or p.startswith("web/src/lib") for p in paths):
        add("B5")
    if any(p.startswith("web/src/components/ui") or p.endswith(".css") for p in paths):
        add("B6")
    if any(p.startswith("fleet/src/gbfleet/") or p.startswith("cli/") or p.startswith("fleet/")
           for p in paths):
        add("B7")
    if any(p.startswith(".github/") or p.startswith("docker") or p.startswith("pyproject")
           for p in paths):
        add("B8")

    for ev in evidence:
        if not isinstance(ev, dict):
            continue
        if ev.get("kind") == "sabotage" and int(ev.get("tests_failed") or 0) >= 1:
            add("C1")
        if ev.get("kind") == "test":
            add("C2")
    if outc.get("turns_used") is not None and test_files >= 1:
        add("C2")
    if _is_bug(item) and (any(_is_test_path(p) for p in added) or (
            files_added >= 1 and test_files >= 1)):
        add("C3")

    if any(_H1_RE.search(p) for p in paths):
        add("H1")
    if any(p.endswith("security/authz.py") or p.endswith("keys.py") or "authz" in p
           or ( _is_test_path(p) and "refus" in p.lower()) for p in paths):
        add("H2")
    has_pg = any("postgres" in p.lower() or "psycopg" in p.lower() for p in paths)
    has_sqlite = any("sqlite" in p.lower() for p in paths)
    if has_pg and has_sqlite:
        add("H3")
    if size_band(item) == "L":
        add("H4")
    # H5 is graded on every attempt (scope-bounce share) but tagged when a spec exists.
    # Tagging every attempt would make criterion 2's `other` cell unreachable.
    if item is not None and (getattr(item, "prd_id", None) or getattr(item, "prd_section", None)):
        add("H5")

    ending = outc.get("outcome") or ""
    tool_errors = outc.get("tool_errors")
    has_branch = outc.get("has_branch")
    # Signed-off/bounced attempts in tests often have no branch; that is not a protocol
    # failure. E1 is released/expired, a missing branch on a non-verdict ending, or
    # malformed tool calls the adapter counted.
    if ending in ("released", "expired"):
        add("E1")
    elif has_branch is False and ending not in ("signed_off", "bounced"):
        add("E1")
    if tool_errors is not None and int(tool_errors) > 0:
        add("E1")
    if any(_norm_path(p).lower() in _STATE_FILES or _norm_path(p).lower().endswith(s)
           for p in paths for s in _STATE_FILES):
        add("E2")
    if any(p.startswith("docs/") or p.endswith(".md") for p in paths):
        add("E3")

    if outc.get("role") == "reviewer":
        add("F1")
        if ending == "signed_off":
            add("F2")
        if bounced and bounce_cat and bounce_cat != "other":
            add("F3")

    return hits or [FAMILY_OTHER]


def _declared(db: Session, row: Delegation) -> tuple[str, str]:
    """The child's declared vendor and model, in the same terms `delegation.measured` uses."""
    from app.services.delegation import UNDECLARED

    agent = db.get(Agent, row.agent_id) if row.agent_id else None
    caps = (agent.capabilities or {}) if agent is not None else {}
    vendor = caps.get("vendor") if isinstance(caps.get("vendor"), str) and caps.get("vendor") else UNDECLARED
    model = row.declared_model or ("" if vendor != UNDECLARED else UNDECLARED)
    return vendor, model


def _vendor_of(chosen: str | None) -> str:
    return (chosen or "").split(":", 1)[0]


def sampled_from(*, declared_vendor: str, declared_model: str, winner: str | None,
                 runner_up: str | None, source: str | None) -> str:
    """How this attempt came to be sampled (D2).

    `unknown` when no launch post arrived, and that is a value the page shows rather than a
    gap it hides: a supervisor that could not reach the server has not turned every attempt
    into somebody's first choice.
    """
    if source == "probe":
        return "probe"
    if source == "explicit":
        return "explicit"
    if not winner:
        return "unknown"
    declared = f"{declared_vendor}:{declared_model}"
    if declared == winner:
        return "first_choice"
    if runner_up and declared == runner_up:
        return "fallback"
    return "unknown"


def _attempt_no(db: Session, row: Delegation) -> int:
    finished = _aware(row.finished_at) or _now()
    prior = db.scalars(select(Delegation).where(Delegation.item_id == row.item_id)).all()
    # Tie-broken on the id, because `on_outcome` finishes every linked row on the item in one
    # pass and gives them the same timestamp — two rows each counting the other would make
    # both of them attempt 2, and no attempt 1 would exist.
    here = (finished, row.id)
    earlier = [
        r for r in prior
        if r.id != row.id and r.outcome is not None
        and ((_aware(r.finished_at) or finished), r.id) < here
    ]
    return len(earlier) + 1


def _row_for(db: Session, *, delegation_id: str | None = None,
             enrolment_id: str | None = None) -> AttemptTelemetry | None:
    if delegation_id:
        found = db.scalar(select(AttemptTelemetry).where(
            AttemptTelemetry.delegation_id == delegation_id))
        if found is not None:
            return found
    if enrolment_id:
        return db.scalar(select(AttemptTelemetry).where(
            AttemptTelemetry.enrolment_id == enrolment_id))
    return None


def _merge(row: AttemptTelemetry, values: dict) -> bool:
    """D3's merge rule, in one place so no caller can forget half of it.

    A post merges non-null values in and NEVER writes a null over a value. A differing
    non-null value wins, because a supervisor that read the child's result record twice is
    likelier right the second time — and a repost that changes something must be visible in
    `report_count` rather than silently applied or silently dropped.
    """
    changed = False
    for field, value in values.items():
        if value is None:
            continue
        if getattr(row, field) != value:
            setattr(row, field, value)
            changed = True
    return changed


def derive(db: Session, row: Delegation) -> AttemptTelemetry | None:
    """Write this finished attempt's half of the record. Called at the outcome event.

    Flushes rather than commits: the caller's transaction owns the outcome this describes,
    and a telemetry row that survived a rolled-back sign-off would be a measurement of
    something that did not happen.
    """
    if row.outcome is None:
        return None
    item = db.get(Item, row.item_id) if row.item_id else None
    telemetry = _row_for(db, delegation_id=row.id,
                         enrolment_id=_enrolment_of(db, row))
    created = telemetry is None
    if created:
        telemetry = AttemptTelemetry(id=f"at_{uuid.uuid4().hex[:12]}")
        db.add(telemetry)
    vendor, model = _declared(db, row)
    claimed, finished = _aware(row.claimed_at), _aware(row.finished_at)
    telemetry.delegation_id = row.id
    telemetry.project_id = row.project_id
    telemetry.item_id = row.item_id
    telemetry.vendor = vendor
    telemetry.model = model
    telemetry.lane = row.lane
    telemetry.tier_requested = row.requested_tier
    telemetry.tier_declared = row.declared_tier
    telemetry.task_class = task_class(item)
    telemetry.size_band = size_band(item)
    telemetry.attempt_no = _attempt_no(db, row)
    telemetry.outcome = row.outcome
    telemetry.bounce_category = (bounce_category(getattr(item, "bounce_reason", None))
                                 if row.outcome == "bounced" else None)
    telemetry.capabilities = _capabilities_for_row(item, telemetry)
    if telemetry.capabilities_at_delegate is None:
        stored = getattr(row, "capabilities_at_delegate", None)
        telemetry.capabilities_at_delegate = (
            list(stored) if stored is not None else (
                list(capabilities(item)) if item is not None else None
            )
        )
    telemetry.claim_to_finish_s = (int((finished - claimed).total_seconds())
                                   if claimed and finished and finished >= claimed else None)
    telemetry.sampled = sampled_from(
        declared_vendor=vendor, declared_model=model, winner=telemetry.chosen_winner,
        runner_up=telemetry.chosen_runner_up, source=telemetry.chosen_source)
    # The child said one vendor and the supervisor launched another. Flagged on the row and
    # counted, never resolved: neither side is trusted over the other here, and quietly
    # preferring one would be a guess wearing a fact's clothes. An UNDECLARED child is not a
    # mismatch — it is GRPH-732's other failure, and it already has its own cell.
    from app.services.delegation import UNDECLARED

    launched = _vendor_of(telemetry.chosen_winner)
    telemetry.declaration_mismatch = bool(
        launched and vendor != UNDECLARED and vendor != launched)
    telemetry.derived_at = _now()
    db.flush()
    return telemetry


def task_class(item: Item | None) -> str:
    """The brief's checklist, which is the only task class the ledger already computes."""
    from app.services import delegation as delegation_svc

    if item is None:
        return "general"
    return delegation_svc.checklist_for(item.touchpoints) or "general"


def _capabilities_for_row(item: Item | None, row: AttemptTelemetry) -> list[str]:
    """Exit-time set: touchpoints + the supervisor's diff + the outcome (D2)."""
    return capabilities(item, row.diff_shape, {
        "outcome": row.outcome,
        "bounce_category": row.bounce_category,
        "tool_errors": row.tool_errors,
        "turns_used": row.turns_used,
        "evidence": list(item.evidence or []) if item is not None else [],
        "has_branch": bool(getattr(item, "branch", None)) if item is not None else None,
    })


def _enrolment_of(db: Session, row: Delegation) -> str | None:
    """The seat this delegation's child registered on, which is how a launch post addressed
    the row before any delegation was linked to it."""
    agent = db.get(Agent, row.agent_id) if row.agent_id else None
    if agent is not None and getattr(agent, "enrolment_id", None):
        return agent.enrolment_id
    seat = db.scalar(select(Enrolment).where(Enrolment.delegation_id == row.id))
    return seat.id if seat is not None else None


def purge_unfinished(db: Session, project_id: str | None) -> int:
    """Drop runtime-only rows whose delegation never finished (D1).

    A launch post creates a row before anything has happened; if the child never claims, or
    claims and is superseded, no outcome ever arrives and the row would sit forever as an
    attempt with no ending. `expired` is derived from the delegation and the clock rather than
    stored, so there is no sweep to hang this on — it runs at the outcome event instead, which
    is both bounded and the moment new rows appear.
    """
    from app.services import delegation as delegation_svc

    stmt = select(AttemptTelemetry).where(AttemptTelemetry.derived_at.is_(None))
    if project_id:
        stmt = stmt.where(AttemptTelemetry.project_id == project_id)
    dropped = 0
    for row in db.scalars(stmt).all():
        delegation = db.get(Delegation, row.delegation_id) if row.delegation_id else None
        if delegation is None:
            seat = db.get(Enrolment, row.enrolment_id) if row.enrolment_id else None
            expires = _aware(getattr(seat, "expires_at", None)) if seat is not None else None
            # A seat that expired a day ago is not about to produce a child. Nothing shorter:
            # a seat expires in half an hour and the child it minted may still be working.
            if seat is None or (expires and expires < _now() - timedelta(days=1)):
                db.delete(row)
                dropped += 1
            continue
        if delegation.outcome is None and delegation_svc.state(delegation) in ("expired", "closed"):
            db.delete(row)
            dropped += 1
    if dropped:
        db.flush()
    return dropped


# ---- the supervisor's two posts (D3) ---------------------------------------------------------

@dataclass(frozen=True)
class Target:
    """What a post is about, resolved before anything is written.

    Exists so the ROUTER never touches `Delegation` or `Enrolment` itself — the layering
    `test_routers_do_not_touch_the_delegation_model` pins. It carries `project_id` because
    that is the only thing the router needs in order to decide whether this credential may
    write here, and `kind` because the two shapes of the post are not interchangeable.
    """

    kind: str  # seat | delegation
    project_id: str | None
    seat: Enrolment | None = None
    delegation: Delegation | None = None


def target_for(db: Session, *, enrolment_code: str | None = None,
               enrolment_id: str | None = None,
               delegation_id: str | None = None) -> Target | None:
    """Resolve a post's address, or None when it names nothing that exists.

    None is deliberately the same answer for "no such id" and "an id in a project you cannot
    see" once the caller applies its write check: a refusal that distinguished them would let
    a credential enumerate ids by the shape of the error.
    """
    from app.services.fleet import _hash_code

    if enrolment_code:
        seat = db.scalar(select(Enrolment).where(
            Enrolment.code_hash == _hash_code(enrolment_code)))
        return Target("seat", seat.project_id, seat=seat) if seat is not None else None
    if enrolment_id:
        seat = db.get(Enrolment, enrolment_id)
        return Target("seat", seat.project_id, seat=seat) if seat is not None else None
    if delegation_id:
        row = db.get(Delegation, delegation_id)
        return Target("delegation", row.project_id, delegation=row) if row is not None else None
    return None


def record_launch(db: Session, *, target: Target, winner: str | None,
                  runner_up: str | None = None, source: str | None = None,
                  adapter: str | None = None,
                  resolution: dict | None = None) -> AttemptTelemetry:
    """What the supervisor resolved, posted before the child starts.

    Keyed by the SEAT, because at launch there is no delegation to key on: the planner minted
    the seat, the supervisor resolved afterwards, and the child has not claimed anything yet.
    The row this creates is half a record until an outcome arrives, and `purge_unfinished`
    removes it if none ever does.
    """
    seat = target.seat
    if seat is None:
        raise AttemptRefused("a launch post names a seat", status=422)
    row = _row_for(db, enrolment_id=seat.id)
    if row is None and seat.delegation_id:
        row = _row_for(db, delegation_id=seat.delegation_id)
    if row is None:
        row = AttemptTelemetry(id=f"at_{uuid.uuid4().hex[:12]}", enrolment_id=seat.id,
                               project_id=seat.project_id, item_id=seat.item_id)
        db.add(row)
    row.enrolment_id = row.enrolment_id or seat.id
    _merge(row, {"chosen_winner": winner, "chosen_runner_up": runner_up,
                 "chosen_source": source, "adapter_launched": adapter,
                 "resolution": resolution,
                 "project_id": seat.project_id, "item_id": seat.item_id})
    row.report_count = (row.report_count or 0) + 1
    row.reported_at = _now()
    # A probe launch is sampled as probe from the moment it is posted, not only after
    # derive: the panel's rows have to be labelled before any outcome arrives (D7).
    if row.chosen_source == "probe":
        row.sampled = "probe"
    # A launch post that arrives after the outcome must not leave `sampled` at what it was
    # derived to be with no winner to compare against.
    if row.derived_at is not None and row.delegation_id:
        delegation = db.get(Delegation, row.delegation_id)
        if delegation is not None:
            vendor, model = _declared(db, delegation)
            row.sampled = sampled_from(declared_vendor=vendor, declared_model=model,
                                       winner=row.chosen_winner, runner_up=row.chosen_runner_up,
                                       source=row.chosen_source)
    db.flush()
    return row


def record_exit(db: Session, *, target: Target, values: dict) -> AttemptTelemetry:
    """The runtime facts only the supervisor saw, posted at child exit.

    Addressed by the delegation when the caller knows it and by the SEAT when it does not —
    which is the ordinary case, because the supervisor holds the seat's row id from the
    roster and never the delegation's. Both find the same row: the launch post created it
    under the seat, and the outcome derivation later binds the delegation to it.

    Idempotent by the merge rule rather than by refusing a second post: a supervisor that
    restarts and re-reports is doing the right thing, and a route that answered it with an
    error would train it to stop.
    """
    delegation, seat = target.delegation, target.seat
    if delegation is None and seat is None:
        raise AttemptRefused("an exit post names a delegation or a seat", status=422)
    if delegation is None and seat is not None and seat.delegation_id:
        delegation = db.get(Delegation, seat.delegation_id)
    row = _row_for(db,
                   delegation_id=delegation.id if delegation is not None else None,
                   enrolment_id=(seat.id if seat is not None
                                 else (_enrolment_of(db, delegation) if delegation else None)))
    if row is None:
        row = AttemptTelemetry(
            id=f"at_{uuid.uuid4().hex[:12]}",
            delegation_id=delegation.id if delegation is not None else None,
            enrolment_id=seat.id if seat is not None else None,
            project_id=(delegation.project_id if delegation is not None
                        else seat.project_id if seat is not None else None),
            item_id=(delegation.item_id if delegation is not None
                     else seat.item_id if seat is not None else None))
        db.add(row)
    if delegation is not None:
        row.delegation_id = delegation.id
    if seat is not None and row.enrolment_id is None:
        row.enrolment_id = seat.id
    _merge(row, values)
    row.report_count = (row.report_count or 0) + 1
    row.reported_at = _now()
    # Diff shape arriving after the outcome must re-tag: the set at derive-time had no
    # diff, and leaving it would make criterion 1's A2 unreachable whenever the supervisor
    # posted second (the ordinary order).
    if row.derived_at is not None:
        item = db.get(Item, row.item_id) if row.item_id else None
        row.capabilities = _capabilities_for_row(item, row)
    db.flush()
    return row


#: How long an item in review waits for its branch to be published before the server hands it
#: out anyway. The reap follows the child's exit by one watch tick, so this is generous; it is a
#: BACKSTOP, not a schedule. Never hiding work outranks never showing an unpublished branch: a
#: supervisor that dies must not take the review with it.
PUBLISH_GRACE_SECONDS = 180


def published_now() -> datetime:
    """The moment a supervisor reported publishing. A function so the route never has to
    reach for a clock, and `_merge`'s never-write-a-null rule keeps a later post from
    un-publishing what an earlier one recorded."""
    return _now()


def publish_pending(db: Session, item: Item) -> bool:
    """Is a supervisor about to publish this item's branch, and hasn't yet? (GRPH-754)

    Three conditions, and each one is there to stop this from hiding work:

    - a launch post exists for the item's latest attempt, so we KNOW a supervisor launched it
      and is going to report. An item nobody supervised — a human's branch, an agent running
      standalone — is never withheld, because nothing will ever arrive to release it.
    - that attempt has no `branch_published_at` yet.
    - the item entered review less than `PUBLISH_GRACE_SECONDS` ago. Past that the server hands
      it out regardless: a supervisor that died mid-reap must not make the work unreviewable.

    `updated_at` stands in for "entered review", which is what stamps it in the ordinary case.
    An evidence append moves it too, and the cost of that is a few more seconds of waiting.
    """
    if not item.branch or item.status != "review":
        return False
    row = db.scalar(select(AttemptTelemetry)
                    .where(AttemptTelemetry.item_id == item.id,
                           AttemptTelemetry.chosen_source.is_not(None))
                    .order_by(AttemptTelemetry.id.desc()))
    if row is None or row.branch_published_at is not None:
        return False
    since = _aware(item.updated_at)
    return bool(since and (_now() - since).total_seconds() < PUBLISH_GRACE_SECONDS)


def row_dict(row: AttemptTelemetry) -> dict:
    """What the route echoes back. Nulls stay null: a token count nobody reported is not zero."""
    return {
        "id": row.id,
        "delegation_id": row.delegation_id,
        "enrolment_id": row.enrolment_id,
        "project_id": row.project_id,
        "item_id": row.item_id,
        "vendor": row.vendor,
        "model": row.model,
        "binary_version": row.binary_version,
        "lane": row.lane,
        "tier_requested": row.tier_requested,
        "task_class": row.task_class,
        "size_band": row.size_band,
        "capabilities": list(row.capabilities or []),
        "diff_shape": row.diff_shape,
        "tool_errors": row.tool_errors,
        "attempt_no": row.attempt_no,
        "sampled": row.sampled,
        "declaration_mismatch": row.declaration_mismatch,
        "outcome": row.outcome,
        "bounce_category": row.bounce_category,
        "claim_to_finish_s": row.claim_to_finish_s,
        "turns_used": row.turns_used,
        "turn_budget": row.turn_budget,
        "wall_seconds": row.wall_seconds,
        "tokens_in": row.tokens_in,
        "tokens_out": row.tokens_out,
        "exit_meaning": row.exit_meaning,
        "derived": row.derived_at is not None,
        "reported": row.reported_at is not None,
        "branch_published": row.branch_published_at is not None,
        "report_count": row.report_count,
    }


# ---- rollups and the page's read (D11, D6, D5) ------------------------------------------------

#: A rate needs this many finished attempts before it counts (PRD-37 `MIN_SAMPLE`). The floor is
#: a rule about READING a number, never about whether a week happened — which is why the rollup
#: below writes thin weeks and this constant is applied at the read.
FLOOR = 5

#: Above this share of one sampling reason, a rate carries the "sampled by preference" badge.
#: Visual only: it never alters a denominator, and the rules read the same undivided rate.
SKEW_SHARE = 0.8

#: Below this share of attempts reporting tokens, the cost proxy is not shown at all. A partial
#: numerator over a full denominator makes a vendor that prints nothing look cheap.
COST_COVERAGE = 0.8

CELL_KEYS = ("vendor", "model", "binary_version", "capability", "size_band")


def week_of(when: datetime) -> str:
    """ISO year-week, e.g. `2026-W37`. The week a rollup row is a fact about."""
    year, week, _ = _aware(when).isocalendar()
    return f"{year}-W{week:02d}"


def _caps_of(row: AttemptTelemetry) -> list[str]:
    """The set this attempt contributes to. Falls back through the D3 map so a row
    derived before S1 still lands somewhere rather than vanishing from the grid."""
    caps = [c for c in (row.capabilities or []) if isinstance(c, str) and c]
    if caps:
        return caps
    mapped = TASK_CLASS_TO_CAPABILITY.get(row.task_class or "", FAMILY_OTHER)
    return [mapped]


def _cell_keys_of(row: AttemptTelemetry) -> list[tuple]:
    base = (row.vendor or "", row.model or "", row.binary_version or "", row.size_band or "")
    return [(*base[:3], cap, base[3]) for cap in _caps_of(row)]


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def roll(db: Session, project_id: str, *, weeks: set[str] | None = None) -> int:
    """Recompute `harness_rollups` from the raw rows. Returns the number of rows written.

    Recomputed, never folded into: a rollup that cannot be reproduced from what it summarises
    is a number nobody can check, and that is the whole reason `attempt_telemetry` is kept far
    longer than any chart needs it. Deleting the weeks first is what makes a re-roll idempotent
    rather than cumulative.

    Weeks with attempts are written even below the floor. A week with NO attempts is left
    absent, because no attempts and a zero rate are different claims and the chart draws the
    absence as a gap.
    """
    rows = db.scalars(select(AttemptTelemetry).where(
        AttemptTelemetry.project_id == project_id,
        AttemptTelemetry.derived_at.is_not(None))).all()
    buckets: dict[tuple, dict] = {}
    for row in rows:
        week = week_of(row.derived_at)
        if weeks is not None and week not in weeks:
            continue
        for key in _cell_keys_of(row):
            cell = buckets.setdefault((week, *key), {
                "finished": 0, "signed_off": 0, "bounced": 0, "seconds": [],
                "tokens_in": 0, "tokens_out": 0, "tokens_reported": 0, "signed_off_reported": 0,
                "first_choice": 0, "fallback": 0, "explicit": 0, "unknown": 0, "probe": 0,
                "turns_used": 0, "turns_reported": 0, "budget_hits": 0})
            cell["finished"] += 1
            cell["signed_off"] += 1 if row.outcome == "signed_off" else 0
            cell["bounced"] += 1 if row.outcome == "bounced" else 0
            if row.claim_to_finish_s is not None:
                cell["seconds"].append(float(row.claim_to_finish_s))
            # Tokens are SUMMED over the attempts that reported, with the count of those attempts
            # beside them. No average is stored, which is what keeps "not reported" from becoming
            # a zero the moment a week is aggregated.
            if row.tokens_in is not None or row.tokens_out is not None:
                cell["tokens_in"] += row.tokens_in or 0
                cell["tokens_out"] += row.tokens_out or 0
                cell["tokens_reported"] += 1
                cell["signed_off_reported"] += 1 if row.outcome == "signed_off" else 0
            if row.turns_used is not None:
                cell["turns_used"] += int(row.turns_used)
                cell["turns_reported"] += 1
            if _is_budget_hit(row.exit_meaning):
                cell["budget_hits"] += 1
            cell[row.sampled if row.sampled in SAMPLED else "unknown"] += 1

    touched = weeks if weeks is not None else {k[0] for k in buckets}
    existing = db.scalars(select(HarnessRollup).where(
        HarnessRollup.project_id == project_id)).all()
    for old in existing:
        if weeks is None or old.week in touched:
            db.delete(old)
    db.flush()
    now = _now()
    for (week, vendor, model, version, capability, band), cell in buckets.items():
        db.add(HarnessRollup(
            project_id=project_id, week=week, vendor=vendor, model=model,
            binary_version=version, capability=capability, size_band=band,
            finished=cell["finished"], signed_off=cell["signed_off"],
            bounced=cell["bounced"],
            median_seconds=(int(_median(cell["seconds"])) if cell["seconds"] else None),
            tokens_in=cell["tokens_in"] or None, tokens_out=cell["tokens_out"] or None,
            tokens_reported=cell["tokens_reported"],
            signed_off_reported=cell["signed_off_reported"],
            first_choice=cell["first_choice"], fallback=cell["fallback"],
            explicit=cell["explicit"], unknown=cell["unknown"], probe=cell["probe"],
            turns_used=cell["turns_used"], turns_reported=cell["turns_reported"],
            budget_hits=cell["budget_hits"],
            rolled_at=now))
    db.flush()
    return len(buckets)


def roll_if_stale(db: Session, project_id: str) -> int:
    """Re-roll only the weeks whose raw rows have moved since they were last rolled.

    The nightly job is the ordinary path; this is what keeps a page honest between runs
    without recomputing a year of weeks to answer one request.
    """
    rolled: dict[str, datetime] = {
        r.week: _aware(r.rolled_at) for r in db.scalars(select(HarnessRollup).where(
            HarnessRollup.project_id == project_id)).all()}
    stale: set[str] = set()
    for row in db.scalars(select(AttemptTelemetry).where(
            AttemptTelemetry.project_id == project_id,
            AttemptTelemetry.derived_at.is_not(None))).all():
        week = week_of(row.derived_at)
        seen = max(t for t in (_aware(row.derived_at), _aware(row.reported_at)) if t)
        if week not in rolled or rolled[week] is None or rolled[week] < seen:
            stale.add(week)
    # A week whose raw rows are all gone (retention) leaves a rollup nothing refreshes; that is
    # correct — the rollup is the surviving fact — so `stale` only ever names weeks with rows.
    return roll(db, project_id, weeks=stale) if stale else 0


def _version_key(version: str) -> tuple:
    """Order versions numerically where they look numeric, alphabetically where they do not.

    `0.23.0` before `0.100.0` is the whole point; a string sort puts them the other way round
    and the page would then default to a version that is not the current one.
    """
    parts = re.split(r"[.\-+]", version or "")
    out: list = []
    for part in parts:
        out.append((0, int(part), "") if part.isdigit() else (1, 0, part))
    return (len(out) > 0, tuple(out))


def _skew(sampling: dict) -> dict | None:
    """The lopsidedness of a cell's sampling, or None when nothing dominates.

    A badge, not a correction: the rate beside it counts every attempt in the cell, and the
    counts are shown so a reader can do their own arithmetic. What it says is "this number is
    honest about what happened and is not a fair comparison against a cell chosen differently".
    """
    total = sum(sampling.values())
    if not total:
        return None
    reason, count = max(sampling.items(), key=lambda kv: kv[1])
    share = count / total
    return {"reason": reason, "share": round(share, 3)} if share > SKEW_SHARE else None


def _cost(tokens_in: int, tokens_out: int, reported: int, signed_off_reported: int,
          finished: int) -> dict:
    """Tokens per signed-off item, or a stated refusal to compare.

    Suppressed below `COST_COVERAGE`, and the refusal carries the two counts rather than a
    shrug, because "3 of 11 attempts reported tokens" is a fact a reader can act on and
    "unavailable" is not.
    """
    coverage = (reported / finished) if finished else 0.0
    if not reported or coverage < COST_COVERAGE:
        return {"comparable": False, "reported": reported, "finished": finished,
                "reason": f"not comparable: {reported} of {finished} attempts reported tokens"}
    if not signed_off_reported:
        return {"comparable": False, "reported": reported, "finished": finished,
                "reason": "no signed-off attempt reported tokens"}
    return {"comparable": True, "reported": reported, "finished": finished,
            "tokens_per_signed_off": round((tokens_in + tokens_out) / signed_off_reported, 1),
            "tokens_in": tokens_in, "tokens_out": tokens_out}


def _is_budget_hit(exit_meaning: str | None) -> bool:
    """The share §7.3 reads. Matched on the gbagent wording, not a loose 'budget'."""
    text = (exit_meaning or "").lower()
    return "turn budget spent" in text or "budget exhaust" in text


def _side(finished: int, signed_off: int) -> dict:
    """One sampling side of a cell. Never added to the other side's rate."""
    return {
        "n": finished,
        "finished": finished,
        "signed_off": signed_off,
        "rate": round(signed_off / finished, 3) if finished else None,
        "below_floor": finished < FLOOR,
    }


def report(db: Session, project_id: str, *, window_days: int | None = None,
           versions: str = "current", overlay: bool = False) -> dict:
    """The Harness page's whole read: one entry per cell, each with its weekly series.

    `versions="current"` keeps the newest `binary_version` per vendor+model AND the previous
    one when both exist in the window (PRD-41 D13: version cells sit side by side rather than
    drawing one trend through two versions). `versions=all` returns every version. Every cell
    carries `n`, `below_floor`, its sampling counts and skew badge, and a cost proxy that
    says when it will not compare.
    """
    roll_if_stale(db, project_id)
    window = WINDOW_DAYS if window_days is None else window_days
    cutoff = week_of(_now() - timedelta(days=window))
    rows = [r for r in db.scalars(select(HarnessRollup).where(
        HarnessRollup.project_id == project_id)).all() if r.week >= cutoff]
    facts = cell_facts(db, [project_id], cutoff)
    out = _shape(rows, window=window, versions=versions, facts=facts)
    out["project_id"] = project_id
    out["scope"] = "project"
    out["capability_set"] = capability_catalog()
    out["coverage"] = _coverage(db, [project_id], cutoff)
    out["review_cells"] = review_cells(db, [project_id])
    out["probe_suggestions"] = probe_suggestions(db, project_id)
    if overlay:
        attach_platform(db, out)
    return out


def _coverage(db: Session, project_ids: list[str], cutoff: str) -> dict:
    """Attempts that derived to ≥1 leaf over attempts. The number the page shows so a
    rotting heuristic is a fact, not a silent reclassification (criterion 2)."""
    if not project_ids:
        return {"attempts": 0, "with_leaf": 0, "rate": None}
    rows = [r for r in db.scalars(select(AttemptTelemetry).where(
        AttemptTelemetry.project_id.in_(project_ids),
        AttemptTelemetry.derived_at.is_not(None))).all()
            if week_of(r.derived_at) >= cutoff]
    attempts = len(rows)
    with_leaf = sum(1 for r in rows if any(c in CAPABILITY_LEAVES for c in _caps_of(r)))
    return {"attempts": attempts, "with_leaf": with_leaf,
            "rate": round(with_leaf / attempts, 3) if attempts else None}


def _shape(rows: list, *, window: int, versions: str, per_project: bool = False,
           facts: dict | None = None) -> dict:
    """Turn rollup rows into the page's cells. Shared by the project and org reads, because
    an org view that aggregated differently from the project view would be a second
    definition of the same number."""
    cells: dict[tuple, dict] = {}
    for row in rows:
        key = (row.vendor, row.model, row.binary_version, row.capability, row.size_band)
        cell = cells.setdefault(key, {
            "finished": 0, "signed_off": 0, "bounced": 0, "tokens_in": 0, "tokens_out": 0,
            "tokens_reported": 0, "signed_off_reported": 0,
            "sampling": {r: 0 for r in SAMPLED}, "series": [], "medians": [],
            "by_project": {}})
        if per_project:
            seen = cell["by_project"].setdefault(row.project_id,
                                                 {"finished": 0, "signed_off": 0})
            seen["finished"] += row.finished
            seen["signed_off"] += row.signed_off
        cell["finished"] += row.finished
        cell["signed_off"] += row.signed_off
        cell["bounced"] += row.bounced
        cell["tokens_in"] += row.tokens_in or 0
        cell["tokens_out"] += row.tokens_out or 0
        cell["tokens_reported"] += row.tokens_reported
        cell["signed_off_reported"] += row.signed_off_reported or 0
        for reason in SAMPLED:
            cell["sampling"][reason] += getattr(row, reason, 0) or 0
        if row.median_seconds is not None:
            cell["medians"].append(float(row.median_seconds))
        cell["series"].append({
            "week": row.week, "finished": row.finished, "signed_off": row.signed_off,
            "rate": round(row.signed_off / row.finished, 3) if row.finished else None,
            # Every point carries its own floor verdict. A thin week is drawn grey and left
            # unconnected rather than dropped, because dropping it would let a reader join two
            # solid points across a gap that was never measured.
            "below_floor": row.finished < FLOOR,
            "median_seconds": row.median_seconds,
        })

    newest: dict[tuple, str] = {}
    previous: dict[tuple, str] = {}
    for (vendor, model, version, *_rest) in cells:
        pair = (vendor, model)
        seen = newest.get(pair)
        if seen is None or _version_key(version) > _version_key(seen):
            if seen is not None:
                previous[pair] = seen
            newest[pair] = version
        elif version != seen and (pair not in previous
                                  or _version_key(version) > _version_key(previous[pair])):
            previous[pair] = version
    versions_seen: dict[tuple, list[str]] = {}
    for (vendor, model, version, *_rest) in cells:
        versions_seen.setdefault((vendor, model), [])
        if version not in versions_seen[(vendor, model)]:
            versions_seen[(vendor, model)].append(version)

    leaves = []
    for key, cell in sorted(cells.items()):
        vendor, model, version, capability, band = key
        if versions == "current" and version != newest[(vendor, model)] and version != previous.get(
                (vendor, model)):
            continue
        leaf = {
            "key": dict(zip(CELL_KEYS, key)),
            "finished": cell["finished"],
            "signed_off": cell["signed_off"],
            "bounced": cell["bounced"],
            "rate": round(cell["signed_off"] / cell["finished"], 3) if cell["finished"] else None,
            "below_floor": cell["finished"] < FLOOR,
            "sampling": cell["sampling"],
            "skew": _skew(cell["sampling"]),
            "median_seconds": (int(_median(cell["medians"])) if cell["medians"] else None),
            "cost": _cost(cell["tokens_in"], cell["tokens_out"], cell["tokens_reported"],
                          cell["signed_off_reported"], cell["finished"]),
            "versions_seen": sorted(versions_seen[(vendor, model)], key=_version_key),
            "is_current_version": version == newest[(vendor, model)],
            "series": sorted(cell["series"], key=lambda p: p["week"]),
            "by_project": [{"project_id": pid, **counts}
                           for pid, counts in sorted(cell["by_project"].items())],
        }
        fact = (facts or {}).get(key)
        if fact is not None:
            _apply_fact(leaf, fact)
        leaves.append(leaf)
    out = _as_grid(leaves)
    return {
        "window_days": window,
        "versions": versions,
        "floor": FLOOR,
        "skew_share": SKEW_SHARE,
        "generated_at": _now().isoformat(),
        "capability_set": capability_catalog(),
        "cells": out,
        # Named rather than left to be counted off a list the page may have filtered. A fleet
        # whose every cell is thin is the ordinary state of a small instance, and the page has
        # to be able to say so instead of looking empty.
        "below_floor_count": sum(1 for c in out if c["below_floor"]),
    }


def _sum_sampling(members: list[dict]) -> dict:
    out = {r: 0 for r in SAMPLED}
    for m in members:
        for reason in SAMPLED:
            out[reason] += (m.get("sampling") or {}).get(reason, 0)
    return out


def _as_grid(leaves: list[dict]) -> list[dict]:
    """Family rollups with leaves nested. A leaf under the floor stays in the payload
    greyed; serving the rollup *as* the leaf is the criterion-3 sabotage."""
    groups: dict[tuple, list[dict]] = {}
    for cell in leaves:
        cap = cell["key"]["capability"]
        family = family_of(cap)
        key = (cell["key"]["vendor"], cell["key"]["model"], cell["key"]["binary_version"],
               family, cell["key"]["size_band"])
        groups.setdefault(key, []).append(cell)
    out: list[dict] = []
    for (vendor, model, version, family, band), members in sorted(groups.items()):
        if family == FAMILY_OTHER:
            cell = members[0]
            out.append({**cell, "kind": "other", "family": FAMILY_OTHER, "label": "other",
                        "leaves": []})
            continue
        finished = sum(m["finished"] for m in members)
        signed_off = sum(m["signed_off"] for m in members)
        bounced = sum(m["bounced"] for m in members)
        sampling = _sum_sampling(members)
        series_by_week: dict[str, dict] = {}
        for m in members:
            for point in m["series"]:
                seen = series_by_week.setdefault(point["week"], {
                    "week": point["week"], "finished": 0, "signed_off": 0,
                    "median_seconds": point.get("median_seconds")})
                seen["finished"] += point["finished"]
                seen["signed_off"] += point["signed_off"]
        series = []
        for week, point in sorted(series_by_week.items()):
            series.append({
                **point,
                "rate": round(point["signed_off"] / point["finished"], 3) if point["finished"] else None,
                "below_floor": point["finished"] < FLOOR,
            })
        by_project: dict[str, dict] = {}
        for m in members:
            for row in m.get("by_project") or []:
                seen = by_project.setdefault(row["project_id"],
                                             {"finished": 0, "signed_off": 0})
                seen["finished"] += row["finished"]
                seen["signed_off"] += row["signed_off"]
        # Cost of a family with several leaves is on the leaves; a single-leaf family
        # can carry that leaf's cost without pretending the two are different numbers.
        cost = members[0]["cost"] if len(members) == 1 else {
            "comparable": False, "reported": 0, "finished": finished,
            "reason": "family rollup: cost is on the leaves"}
        family_row = {
            "kind": "family",
            "family": family,
            "label": "family rollup",
            "key": {"vendor": vendor, "model": model, "binary_version": version,
                    "capability": family, "size_band": band},
            "finished": finished,
            "signed_off": signed_off,
            "bounced": bounced,
            "rate": round(signed_off / finished, 3) if finished else None,
            "below_floor": finished < FLOOR,
            "sampling": sampling,
            "skew": _skew(sampling),
            "median_seconds": members[0]["median_seconds"] if len(members) == 1 else None,
            "cost": cost,
            "versions_seen": members[0]["versions_seen"],
            "is_current_version": members[0]["is_current_version"],
            "series": series,
            "by_project": [{"project_id": pid, **counts}
                           for pid, counts in sorted(by_project.items())],
            "leaves": [{**m, "kind": "leaf", "family": family,
                        "label": m["key"]["capability"]} for m in members],
        }
        if any("samples" in m for m in members):
            nat_f = sum((m.get("samples") or {}).get("natural", {}).get("finished", 0)
                        for m in members)
            nat_s = sum((m.get("samples") or {}).get("natural", {}).get("signed_off", 0)
                        for m in members)
            prb_f = sum((m.get("samples") or {}).get("probe", {}).get("finished", 0)
                        for m in members)
            prb_s = sum((m.get("samples") or {}).get("probe", {}).get("signed_off", 0)
                        for m in members)
            family_row["samples"] = {"natural": _side(nat_f, nat_s), "probe": _side(prb_f, prb_s)}
            family_row["finished"] = nat_f
            family_row["signed_off"] = nat_s
            family_row["rate"] = family_row["samples"]["natural"]["rate"]
            family_row["below_floor"] = nat_f < FLOOR
            family_row["utilization"] = members[0].get("utilization") if len(members) == 1 else {
                "tokens": cost,
                "turns": {"median": None, "budget_median": None, "reported": 0, "finished": nat_f,
                          "reason": "family rollup: turns are on the leaves"},
                "budget_hits": {"hits": 0, "reported": 0, "share": None,
                                "reason": "family rollup: budget hits are on the leaves"},
            }
            family_row["build_cost"] = members[0].get("build_cost", cost) if len(members) == 1 else {
                "comparable": False, "reported": 0, "finished": nat_f,
                "reason": "family rollup: cost is on the leaves"}
            family_row["review_cost"] = members[0].get("review_cost") if len(members) == 1 else {
                "comparable": False, "reported": 0, "finished": 0,
                "reason": "family rollup: review cost is on the leaves"}
        out.append(family_row)
    return out


# ---- recommendations: what a person has seen, and what a cell teaches (D7, D8) ---------------

#: PRD-16 calls this out on the shard; the Lessons page scores it like any other lesson.
LESSON_SOURCE = "harness-telemetry"


def _lesson_key(cell: dict) -> str:
    """The dedup key: the cell WITHOUT its binary version.

    A point release is not a new thing to learn, so 0.23.0 and 0.100.0 of one harness share a
    mark. Crossing the floor is an event in a cell's life, not a level it can re-enter.
    """
    k = cell["key"]
    return ":".join([k["vendor"], k["model"], k["capability"], k["size_band"]])


def marks_for(db: Session, *, user_id: str, scope: str, scope_id: str) -> dict[str, "RecommendationMark"]:
    from app.models import RecommendationMark

    rows = db.scalars(select(RecommendationMark).where(
        RecommendationMark.user_id == user_id,
        RecommendationMark.scope == scope,
        RecommendationMark.scope_id == scope_id)).all()
    return {r.card_key: r for r in rows}


def mark_card(db: Session, *, user_id: str, scope: str, scope_id: str, card_key: str,
              evidence_hash: str, action: str) -> "RecommendationMark":
    """Record that this person has seen this card at this evidence (D7).

    Accept and dismiss share the row deliberately. Both mean "stay quiet until the numbers
    move", and two tables would let one of them forget the hash rule — which is the half that
    makes an accepted recommendation come back when its evidence reverses.
    """
    from app.models import RecommendationMark

    if action not in ("accept", "dismiss"):
        raise AttemptRefused(f"unknown action {action!r}", status=422)
    row = db.scalar(select(RecommendationMark).where(
        RecommendationMark.user_id == user_id,
        RecommendationMark.scope == scope,
        RecommendationMark.scope_id == scope_id,
        RecommendationMark.card_key == card_key))
    if row is None:
        row = RecommendationMark(id=f"rm_{uuid.uuid4().hex[:12]}", user_id=user_id, scope=scope,
                                 scope_id=scope_id, card_key=card_key,
                                 evidence_hash=evidence_hash)
        db.add(row)
    row.evidence_hash = evidence_hash
    if action == "accept":
        row.accepted_at = _now()
        row.dismissed_at = None
    else:
        row.dismissed_at = _now()
        row.accepted_at = None
    db.flush()
    return row


def draft_lesson(db: Session, project_id: str, *, text: str, provenance: dict,
                 origin: str) -> str | None:
    """Write a lesson CANDIDATE into the PRD-16 review inbox. Never publishes.

    `auto_triage=False` on purpose: the scorer that publishes candidates on similarity has no
    business acting on a number this PRD produced, and "nothing is published by this PRD" is
    a claim that has to be enforced at the call rather than asserted in a docstring.
    """
    from app.services import memory as memory_svc

    try:
        shard = memory_svc.add_memory(
            db, text_body=text, scope="global", source=LESSON_SOURCE, project_id=project_id,
            status="candidate", origin=origin, auto_triage=False, fresh=False)
    except Exception:  # noqa: BLE001 — a lesson draft must never fail the read that produced it
        logger.exception("harness: lesson draft failed for %s", project_id)
        return None
    logger.info("harness: drafted lesson %s for %s (%s)", shard.id, project_id, provenance)
    return shard.id


def lessons_for_crossings(db: Session, project_id: str, report: dict) -> list[str]:
    """A cell crossing the floor for the first time drafts one candidate (D8).

    The mark is permanent, which is what makes "first" mean first: a cell that dips back under
    the floor and returns drafts nothing, a new binary version inherits the mark, and a
    rejected candidate does not come back the next night. A lesson that re-drafts itself after
    a human said no is nagging dressed as learning.
    """
    from app.models import HarnessLessonMark

    existing = {m.cell_key for m in db.scalars(select(HarnessLessonMark).where(
        HarnessLessonMark.project_id == project_id)).all()}
    drafted: list[str] = []
    for cell in report["cells"]:
        if cell["below_floor"] or cell["rate"] is None:
            continue
        key = _lesson_key(cell)
        if key in existing:
            continue
        k = cell["key"]
        runner = _runner_up_cell(report, cell)
        text = (f"For {k['capability']} items of size {k['size_band']} "
                f"in the last {report['window_days']} days, "
                f"{k['vendor']}:{k['model']} signed off {cell['signed_off']}/{cell['finished']}"
                + (f"; {_cell_label(runner)} signed off "
                   f"{runner['signed_off']}/{runner['finished']}." if runner else "."))
        shard_id = draft_lesson(db, project_id, text=text,
                                provenance={"cell": k, "why": "crossed the sample floor"},
                                origin="agent:harness-telemetry")
        db.add(HarnessLessonMark(id=f"hlm_{uuid.uuid4().hex[:12]}", project_id=project_id,
                                 cell_key=key, first_crossed_at=_now(), shard_id=shard_id))
        existing.add(key)
        if shard_id:
            drafted.append(shard_id)
    db.flush()
    return drafted


def _cell_label(cell: dict) -> str:
    return f"{cell['key']['vendor']}:{cell['key']['model']}"


def _runner_up_cell(report: dict, cell: dict) -> dict | None:
    """The best OTHER vendor:model measured in the same lane, tier, class and band.

    A lesson that named only the winner would be a recommendation without an alternative, and
    the reader could not tell whether the number was good or merely the only one there is.
    """
    k = cell["key"]
    rivals = [c for c in report["cells"]
              if c is not cell and not c["below_floor"] and c["rate"] is not None
              and (c["key"]["capability"], c["key"]["size_band"]) == (
                  k["capability"], k["size_band"])
              and _cell_label(c) != _cell_label(cell)]
    return max(rivals, key=lambda c: c["rate"]) if rivals else None


# ---- org scope and the platform overlay (D12, D13) --------------------------------------------

#: D13's floor, all three required before a platform cell is served. `k >= 3` alone is not
#: anonymity: three orgs where one holds most of the attempts is one org with two witnesses,
#: and a contributor who knows its own numbers exactly could subtract itself out of a small
#: average. The share cap and the banded counts are what answer that.
PLATFORM_MIN_ORGS = 3
PLATFORM_MIN_N = 20
PLATFORM_MAX_ORG_SHARE = 0.6
#: An org counts toward `orgs_contributing` only with this many of its own in the cell, so a
#: dominant pair cannot be laundered by a third org with one attempt.
PLATFORM_MIN_ORG_N = 5

#: `n` leaves the platform as a band, never a count. This is the part that matters: an org
#: knows its own contribution exactly, and an exact total would let it recover the rest.
_N_BANDS = ((20, 49, "20–49"), (50, 199, "50–199"), (200, None, "200+"))


def _band_n(n: int) -> str:
    for low, high, label in _N_BANDS:
        if n >= low and (high is None or n <= high):
            return label
    return f"<{PLATFORM_MIN_N}"


def org_projects(db: Session, org_id: str) -> list[str]:
    from app.models import Project

    return [p.id for p in db.scalars(select(Project).where(Project.org_id == org_id)).all()]


def org_report(db: Session, org_id: str, *, window_days: int | None = None,
               versions: str = "current", overlay: bool = True) -> dict:
    """The project view over an org's projects (D12).

    The same aggregation, the same floor, the same rules — an org view that summed differently
    would be a second definition of a number the project view already has. What it adds is
    `by_project` on every cell, because "which of our five projects is this?" is the question
    an org admin opens the page with.
    """
    projects = org_projects(db, org_id)
    for project_id in projects:
        roll_if_stale(db, project_id)
    window = WINDOW_DAYS if window_days is None else window_days
    cutoff = week_of(_now() - timedelta(days=window))
    rows = [r for r in db.scalars(select(HarnessRollup).where(
        HarnessRollup.project_id.in_(projects))).all() if r.week >= cutoff] if projects else []
    facts = cell_facts(db, projects, cutoff) if projects else {}
    out = _shape(rows, window=window, versions=versions, per_project=True, facts=facts)
    out["org_id"] = org_id
    out["scope"] = "org"
    out["projects"] = projects
    out["capability_set"] = capability_catalog()
    out["coverage"] = _coverage(db, projects, cutoff)
    out["review_cells"] = review_cells(db, projects)
    out["probe_suggestions"] = []
    if overlay:
        attach_platform(db, out)
    return out


def _platform_key(cell_key: dict) -> tuple:
    return (cell_key["vendor"], cell_key["model"], cell_key["binary_version"],
            cell_key["capability"], cell_key["size_band"])


def attach_platform(db: Session, report_out: dict) -> None:
    """Hang the platform average on each cell that has one, or say why it does not.

    Self-hosted has no overlay at all and the payload says so rather than leaving a null for
    a reader to interpret as "no data yet". On the hosted service a cell is served only when
    all three of D13's conditions hold, and the refusal names which one failed — "fewer than
    three orgs contribute here" is actionable; "unavailable" is not.
    """
    from app.config import settings
    from app.models import PlatformRollup

    if not settings.hosted_mode:
        report_out["platform"] = None
        report_out["platform_reason"] = (
            "no platform average on a self-hosted instance: it is built from other "
            "organisations' rollups, and there are none here")
        return
    rows = db.scalars(select(PlatformRollup)).all()
    by_key: dict[tuple, dict] = {}
    for row in rows:
        key = (row.vendor, row.model, row.binary_version, row.capability, row.size_band)
        seen = by_key.setdefault(key, {"orgs": 0, "finished": 0, "signed_off": 0,
                                       "top_share": 0.0})
        seen["orgs"] = max(seen["orgs"], row.orgs_contributing)
        seen["finished"] += row.finished
        seen["signed_off"] += row.signed_off
        seen["top_share"] = max(seen["top_share"], row.top_org_share or 0.0)
    served = 0
    for cell in report_out["cells"]:
        agg = by_key.get(_platform_key(cell["key"]))
        cell["platform"] = _platform_cell(agg)
        served += 1 if cell["platform"] and cell["platform"].get("rate") is not None else 0
    report_out["platform"] = {"cells_with_overlay": served,
                              "min_orgs": PLATFORM_MIN_ORGS, "min_n": PLATFORM_MIN_N,
                              "max_org_share": PLATFORM_MAX_ORG_SHARE}
    report_out["platform_reason"] = ""


def _platform_cell(agg: dict | None) -> dict | None:
    if agg is None:
        return None
    if agg["orgs"] < PLATFORM_MIN_ORGS:
        return {"rate": None, "reason": "no platform average: fewer than three organisations "
                                        "contribute here"}
    if agg["finished"] < PLATFORM_MIN_N:
        return {"rate": None, "reason": f"no platform average: fewer than {PLATFORM_MIN_N} "
                                        "attempts behind it"}
    if agg["top_share"] > PLATFORM_MAX_ORG_SHARE:
        return {"rate": None, "reason": "no platform average: one organisation holds most of "
                                        "the attempts, so an average would be about them"}
    return {"rate": round(agg["signed_off"] / agg["finished"], 2),
            "n": _band_n(agg["finished"]), "orgs": agg["orgs"]}


def platform_roll(db: Session) -> int:
    """Recompute `platform_rollups` from the orgs that opted in (D13). Returns rows written.

    Contribution is the ROLLUP, never a raw row and never an org id: what crosses the tenancy
    boundary is a count per cell per week, and even the count leaves as a band at read time.
    An org that has opted out is simply not in the input, which is why turning the toggle off
    and recomputing is the whole of the purge — there is nothing of theirs left to delete.
    """
    from app.models import Organization, PlatformRollup, Project

    orgs = [o.id for o in db.scalars(select(Organization).where(
        Organization.telemetry_share.is_(True))).all()]
    projects: dict[str, str] = {}
    if orgs:
        for project in db.scalars(select(Project).where(Project.org_id.in_(orgs))).all():
            projects[project.id] = project.org_id
    buckets: dict[tuple, dict] = {}
    if projects:
        for row in db.scalars(select(HarnessRollup).where(
                HarnessRollup.project_id.in_(list(projects)))).all():
            key = (row.week, row.vendor, row.model, row.binary_version, row.capability,
                   row.size_band)
            cell = buckets.setdefault(key, {"per_org": {}})
            org = projects[row.project_id]
            seen = cell["per_org"].setdefault(org, {"finished": 0, "signed_off": 0})
            seen["finished"] += row.finished
            seen["signed_off"] += row.signed_off

    for old in db.scalars(select(PlatformRollup)).all():
        db.delete(old)
    db.flush()
    written = 0
    now = _now()
    for key, cell in buckets.items():
        # An org with too few of its own in this cell does not COUNT as a contributor, but its
        # attempts still sum: it is real work, and dropping it would bias the average toward
        # whoever runs the most. What it may not do is make a two-org cell look like three.
        counting = {o: v for o, v in cell["per_org"].items()
                    if v["finished"] >= PLATFORM_MIN_ORG_N}
        finished = sum(v["finished"] for v in cell["per_org"].values())
        signed_off = sum(v["signed_off"] for v in cell["per_org"].values())
        top = max((v["finished"] for v in cell["per_org"].values()), default=0)
        week, vendor, model, version, capability, band = key
        db.add(PlatformRollup(
            week=week, vendor=vendor, model=model, binary_version=version,
            capability=capability, size_band=band,
            orgs_contributing=len(counting), finished=finished, signed_off=signed_off,
            top_org_share=(top / finished) if finished else None, rolled_at=now))
        written += 1
    db.flush()
    return written


# ---- PRD-41 S3: sampling split, utilization, review checks, probes --------------------------

def _is_bug_item(item) -> bool:
    tags = [t.strip().lower() for t in (getattr(item, "tags", None) or []) if isinstance(t, str)]
    return "bug" in tags


def _is_not_a_bug(item) -> bool:
    tags = [t.strip().lower().replace("_", "-") for t in (getattr(item, "tags", None) or [])
            if isinstance(t, str)]
    return "not-a-bug" in tags


def _touch_overlap(a, b) -> bool:
    left = {_norm_path(p) for p in (a or []) if isinstance(p, str) and p.strip()}
    right = {_norm_path(p) for p in (b or []) if isinstance(p, str) and p.strip()}
    return bool(left & right)


def _red_sabotage(item) -> bool:
    for ev in getattr(item, "evidence", None) or []:
        if isinstance(ev, dict) and ev.get("kind") == "sabotage" and int(ev.get("tests_failed") or 0) >= 1:
            return True
    return False


def _suite_green_on(item, head: str | None) -> bool:
    from app.services import items as items_svc

    for att in items_svc.attestation_receipts(getattr(item, "evidence", None)):
        if head and att.get("commit") and att.get("commit") != head:
            continue
        for pred in att.get("predicates") or []:
            if isinstance(pred, dict) and pred.get("name") == "suite_green" and pred.get("passed") is True:
                return True
    return False


def _reviewer_declared(db: Session, agent_id: str | None) -> tuple[str, str]:
    from app.models import Agent
    from app.services.delegation import UNDECLARED

    agent = db.get(Agent, agent_id) if agent_id else None
    caps = (agent.capabilities or {}) if agent is not None else {}
    vendor = caps.get("vendor") if isinstance(caps.get("vendor"), str) and caps.get("vendor") else UNDECLARED
    model = caps.get("model") if isinstance(caps.get("model"), str) and caps.get("model") else (
        "" if vendor != UNDECLARED else UNDECLARED)
    return vendor, model


def _apply_fact(leaf: dict, fact: dict) -> None:
    """Overwrite the pooled totals with the natural side; keep probe beside it, never summed."""
    natural = fact["natural"]
    probe = fact["probe"]
    leaf["samples"] = {"natural": _side(natural["finished"], natural["signed_off"]),
                       "probe": _side(probe["finished"], probe["signed_off"])}
    # The number on the cell is the natural rate. Summing the two is criterion 8's sabotage.
    leaf["finished"] = natural["finished"]
    leaf["signed_off"] = natural["signed_off"]
    leaf["rate"] = leaf["samples"]["natural"]["rate"]
    leaf["below_floor"] = natural["finished"] < FLOOR
    leaf["utilization"] = fact["utilization"]
    leaf["build_cost"] = fact["build_cost"]
    leaf["review_cost"] = fact["review_cost"]
    leaf["cost"] = fact["build_cost"]


def cell_facts(db: Session, project_ids: list[str], cutoff: str) -> dict[tuple, dict]:
    """Natural vs probe, utilization, build vs review cost — from raw rows, per cell.

    Rollups pool sampling into one finished count. The page must not. Computing the split
    here, from the attempts, is what keeps a probe n and a natural n from becoming one
    rate (criterion 8).
    """
    if not project_ids:
        return {}
    rows = [r for r in db.scalars(select(AttemptTelemetry).where(
        AttemptTelemetry.project_id.in_(project_ids),
        AttemptTelemetry.derived_at.is_not(None))).all()
            if week_of(r.derived_at) >= cutoff]
    facts: dict[tuple, dict] = {}
    for row in rows:
        side = "probe" if row.sampled == "probe" else "natural"
        for key in _cell_keys_of(row):
            cell = facts.setdefault(key, {
                "natural": {"finished": 0, "signed_off": 0, "tokens_in": 0, "tokens_out": 0,
                            "tokens_reported": 0, "signed_off_reported": 0},
                "probe": {"finished": 0, "signed_off": 0, "tokens_in": 0, "tokens_out": 0,
                          "tokens_reported": 0, "signed_off_reported": 0},
                "turns": [], "budgets": [], "turns_reported": 0,
                "budget_hits": 0, "exit_reported": 0,
            })
            bucket = cell[side]
            bucket["finished"] += 1
            if row.outcome == "signed_off":
                bucket["signed_off"] += 1
            if row.tokens_in is not None or row.tokens_out is not None:
                bucket["tokens_in"] += row.tokens_in or 0
                bucket["tokens_out"] += row.tokens_out or 0
                bucket["tokens_reported"] += 1
                bucket["signed_off_reported"] += 1 if row.outcome == "signed_off" else 0
            if side == "natural":
                if row.turns_used is not None:
                    cell["turns"].append(float(row.turns_used))
                    cell["turns_reported"] += 1
                    if row.turn_budget is not None:
                        cell["budgets"].append(float(row.turn_budget))
                if row.exit_meaning is not None:
                    cell["exit_reported"] += 1
                    if _is_budget_hit(row.exit_meaning):
                        cell["budget_hits"] += 1
    review_costs = _review_costs_by_work_cap(db, project_ids)
    for key, cell in facts.items():
        nat, probe = cell["natural"], cell["probe"]
        turns_med = int(_median(cell["turns"])) if cell["turns"] else None
        budget_med = int(_median(cell["budgets"])) if cell["budgets"] else None
        hits = cell["budget_hits"]
        exit_n = cell["exit_reported"]
        if not cell["turns_reported"]:
            turns = {"median": None, "budget_median": None, "reported": 0,
                     "finished": nat["finished"],
                     "reason": f"not comparable: 0 of {nat['finished']} attempts reported turns"}
        else:
            turns = {"median": turns_med, "budget_median": budget_med,
                     "reported": cell["turns_reported"], "finished": nat["finished"],
                     "reason": None}
        if not exit_n:
            budget = {"hits": 0, "reported": 0, "share": None,
                      "reason": f"not comparable: 0 of {nat['finished']} attempts reported an exit"}
        else:
            budget = {"hits": hits, "reported": exit_n,
                      "share": round(hits / exit_n, 3), "reason": None}
        build = _cost(nat["tokens_in"], nat["tokens_out"], nat["tokens_reported"],
                      nat["signed_off_reported"], nat["finished"])
        cap = key[3]
        review = review_costs.get((key[0], key[1], cap, key[4])) or {
            "comparable": False, "reported": 0, "finished": 0,
            "reason": "no checked reviews for this capability"}
        cell["utilization"] = {"tokens": build, "turns": turns, "budget_hits": budget}
        cell["build_cost"] = build
        cell["review_cost"] = review
    return facts


def _review_costs_by_work_cap(db: Session, project_ids: list[str]) -> dict[tuple, dict]:
    """Reviewer tokens keyed on the WORK's capability, never summed with build cost."""
    from app.models import HarnessReviewCheck

    if not project_ids:
        return {}
    checks = db.scalars(select(HarnessReviewCheck).where(
        HarnessReviewCheck.project_id.in_(project_ids),
        HarnessReviewCheck.kind != "withdrawn")).all()
    if not checks:
        return {}
    # Reviewer attempts are not their own telemetry rows. Cost of review is therefore
    # "not reported" until a later slice posts reviewer tokens; the two numbers still
    # exist as two numbers, which is the load-bearing claim (criterion 17).
    out: dict[tuple, dict] = {}
    for check in checks:
        for cap in (check.capabilities or []):
            key = (check.reviewer_vendor or "", check.reviewer_model or "", cap,
                   check.size_band or "")
            seen = out.setdefault(key, {"finished": 0})
            seen["finished"] += 1
    return {k: {"comparable": False, "reported": 0, "finished": v["finished"],
                "reason": f"not comparable: 0 of {v['finished']} reviews reported tokens"}
            for k, v in out.items()}


def contribution_row(rollup) -> dict:
    """D11 field set plus the probe sampling count. Criterion 31's key-set."""
    return {
        "capability": rollup.capability,
        "size_band": rollup.size_band,
        "vendor": rollup.vendor,
        "model": rollup.model,
        "binary_version": rollup.binary_version,
        "week": rollup.week,
        "finished": rollup.finished,
        "signed_off": rollup.signed_off,
        "first_choice": rollup.first_choice,
        "fallback": rollup.fallback,
        "explicit": rollup.explicit,
        "unknown": rollup.unknown,
        "probe": getattr(rollup, "probe", 0) or 0,
    }


def contribution_rows_for(db: Session, project_id: str) -> list[dict]:
    rows = db.scalars(select(HarnessRollup).where(
        HarnessRollup.project_id == project_id)).all()
    return [contribution_row(r) for r in rows]


# ---- D6: review competence ------------------------------------------------------------------

def record_review_verdict(db: Session, item, reviewer_agent_id: str, verdict: str) -> None:
    """Write the check at the verdict. The nightly pass then confirms, misses, or withdraws it.

    Swallowed at the call: a telemetry row must never fail the sign-off or bounce it describes.
    """
    try:
        _record_review_verdict(db, item, reviewer_agent_id, verdict)
    except Exception:  # noqa: BLE001
        logger.exception("harness: record_review_verdict failed for %s", getattr(item, "id", None))


def _record_review_verdict(db: Session, item, reviewer_agent_id: str, verdict: str) -> None:
    from app.models import AttemptTelemetry, HarnessReviewCheck

    if verdict not in ("signed_off", "bounced") or not reviewer_agent_id:
        return
    tel = db.scalar(select(AttemptTelemetry).where(
        AttemptTelemetry.item_id == item.id).order_by(AttemptTelemetry.derived_at.desc()))
    if tel is None or not tel.delegation_id:
        return
    existing = db.scalar(select(HarnessReviewCheck).where(
        HarnessReviewCheck.delegation_id == tel.delegation_id,
        HarnessReviewCheck.reviewer_agent_id == reviewer_agent_id))
    vendor, model = _reviewer_declared(db, reviewer_agent_id)
    caps = list(tel.capabilities or [])
    now = _now()
    if existing is None:
        existing = HarnessReviewCheck(
            id=f"hrc_{uuid.uuid4().hex[:12]}",
            project_id=item.project_id,
            delegation_id=tel.delegation_id,
            item_id=item.id,
            reviewer_agent_id=reviewer_agent_id,
            reviewer_vendor=vendor,
            reviewer_model=model,
            verdict=verdict,
            kind="confirmed",
            unconfirmed=False,
            capabilities=caps,
            size_band=tel.size_band,
            bounce_category=tel.bounce_category if verdict == "bounced" else None,
            head_commit=getattr(item, "head_commit", None) or None,
            checked_at=now,
            verdict_at=tel.derived_at or now,
        )
        db.add(existing)
    else:
        existing.verdict = verdict
        existing.reviewer_vendor = vendor
        existing.reviewer_model = model
        existing.capabilities = caps
        existing.size_band = tel.size_band
        existing.bounce_category = tel.bounce_category if verdict == "bounced" else None
        existing.head_commit = getattr(item, "head_commit", None) or existing.head_commit
        existing.checked_at = now
        if existing.verdict_at is None:
            existing.verdict_at = tel.derived_at or now
    _recompute_check(db, existing)
    db.flush()


def check_reviews(db: Session, project_id: str | None = None) -> int:
    """Nightly pass over the 14-day window. Recomputes every check; returns rows written."""
    from app.models import HarnessReviewCheck

    stmt = select(HarnessReviewCheck)
    if project_id:
        stmt = stmt.where(HarnessReviewCheck.project_id == project_id)
    rows = list(db.scalars(stmt).all())
    for row in rows:
        _recompute_check(db, row)
    db.flush()
    return len(rows)


def on_bug_filed(db: Session, bug) -> None:
    """A filed bug on overlapping touchpoints is a miss (unconfirmed), inside 14 days."""
    try:
        _on_bug_event(db, bug)
    except Exception:  # noqa: BLE001
        logger.exception("harness: on_bug_filed failed for %s", getattr(bug, "id", None))


def on_bug_updated(db: Session, bug) -> None:
    try:
        _on_bug_event(db, bug)
    except Exception:  # noqa: BLE001
        logger.exception("harness: on_bug_updated failed for %s", getattr(bug, "id", None))


def _on_bug_event(db: Session, bug) -> None:
    from app.models import HarnessReviewCheck, Item, WorkClassification

    if not _is_bug_item(bug):
        return
    checks = db.scalars(select(HarnessReviewCheck).where(
        HarnessReviewCheck.project_id == bug.project_id,
        HarnessReviewCheck.verdict == "signed_off",
        HarnessReviewCheck.kind != "false_bounce")).all()
    classification = db.scalar(select(WorkClassification).where(
        WorkClassification.item_id == bug.id))
    unrelated = classification is not None and classification.outcome == "unrelated"
    for check in checks:
        item = db.get(Item, check.item_id) if check.item_id else None
        if item is None or item.id == bug.id:
            continue
        if not _touch_overlap(item.touchpoints, bug.touchpoints):
            continue
        verdict_at = _aware(check.verdict_at)
        filed_at = _aware(getattr(bug, "created_at", None))
        if verdict_at is None or filed_at is None:
            continue
        delta = filed_at - verdict_at
        if delta < timedelta(0) or delta > timedelta(days=REVIEW_WINDOW_DAYS):
            continue
        # Withdrawal recomputes within 90 days of the verdict (criterion 30).
        if (_now() - verdict_at) > timedelta(days=WITHDRAWAL_DAYS) and check.kind == "miss":
            continue
        check.contradicted_by = bug.id
        if _is_not_a_bug(bug) or unrelated:
            check.kind = "withdrawn"
            check.unconfirmed = False
        elif bug.status == "done":
            check.kind = "miss"
            check.unconfirmed = False
        else:
            check.kind = "miss"
            check.unconfirmed = True
        check.checked_at = _now()
    db.flush()


def _recompute_check(db: Session, check) -> None:
    """Confirm, miss, false-bounce or withdraw from later events. Idempotent."""
    from app.models import Item, WorkClassification

    item = db.get(Item, check.item_id) if check.item_id else None
    if item is None:
        return
    verdict_at = _aware(check.verdict_at) or _aware(check.checked_at) or _now()
    now = _now()

    if check.verdict == "bounced":
        head = check.head_commit or (item.head_commit or None)
        if _suite_green_on(item, head) and item.status == "done":
            check.kind = "false_bounce"
            check.unconfirmed = False
            check.contradicted_by = check.contradicted_by or item.id
        elif now - verdict_at > timedelta(days=REVIEW_WINDOW_DAYS) and check.kind != "false_bounce":
            check.kind = "confirmed"
            check.unconfirmed = False
        check.checked_at = now
        return

    # signed_off: look for a later bug on overlapping touchpoints.
    bugs = [b for b in db.scalars(select(Item).where(
        Item.project_id == check.project_id, Item.id != item.id)).all() if _is_bug_item(b)]
    matched = None
    for bug in bugs:
        if not _touch_overlap(item.touchpoints, bug.touchpoints):
            continue
        filed_at = _aware(bug.created_at)
        if filed_at is None:
            continue
        delta = filed_at - verdict_at
        if delta < timedelta(0) or delta > timedelta(days=REVIEW_WINDOW_DAYS):
            continue
        matched = bug
        break
    if matched is None:
        if now - verdict_at > timedelta(days=REVIEW_WINDOW_DAYS) and check.kind not in (
                "miss", "withdrawn"):
            check.kind = "confirmed"
            check.unconfirmed = False
        check.checked_at = now
        return
    if (now - verdict_at) > timedelta(days=WITHDRAWAL_DAYS) and check.kind == "miss":
        check.checked_at = now
        return
    classification = db.scalar(select(WorkClassification).where(
        WorkClassification.item_id == matched.id))
    unrelated = classification is not None and classification.outcome == "unrelated"
    check.contradicted_by = matched.id
    if _is_not_a_bug(matched) or unrelated:
        check.kind = "withdrawn"
        check.unconfirmed = False
    elif matched.status == "done":
        check.kind = "miss"
        check.unconfirmed = False
    else:
        check.kind = "miss"
        check.unconfirmed = True
    check.checked_at = now


def review_cells(db: Session, project_ids: list[str]) -> list[dict]:
    """F1–F3 cells keyed on the reviewed work's capabilities, grey below five checks.

    Separate from `cells` so a builder cell and a review cell for the same vendor cannot
    be mistaken for one number. The F2 label is 'by touchpoint overlap' because that is
    the whole of the attribution (criterion 30).
    """
    from app.models import HarnessReviewCheck

    if not project_ids:
        return []
    checks = [c for c in db.scalars(select(HarnessReviewCheck).where(
        HarnessReviewCheck.project_id.in_(project_ids))).all()
              if c.kind != "withdrawn"]
    buckets: dict[tuple, dict] = {}
    for check in checks:
        caps = [c for c in (check.capabilities or []) if c in CAPABILITY_LEAVES] or [FAMILY_OTHER]
        for cap in caps:
            key = (check.reviewer_vendor or "", check.reviewer_model or "", cap,
                   check.size_band or "")
            cell = buckets.setdefault(key, {
                "checked": 0, "bounced": 0, "signed_off": 0,
                "false_bounce": 0, "bounce_confirmed": 0, "unclassified": 0,
                "miss": 0, "miss_unconfirmed": 0, "signoff_confirmed": 0,
            })
            cell["checked"] += 1
            if check.verdict == "bounced":
                cell["bounced"] += 1
                if check.kind == "false_bounce":
                    cell["false_bounce"] += 1
                elif check.kind == "confirmed":
                    cell["bounce_confirmed"] += 1
                if (check.bounce_category or "other") == "other":
                    cell["unclassified"] += 1
            else:
                cell["signed_off"] += 1
                if check.kind == "miss" and check.unconfirmed:
                    cell["miss_unconfirmed"] += 1
                elif check.kind == "miss":
                    cell["miss"] += 1
                elif check.kind == "confirmed":
                    cell["signoff_confirmed"] += 1

    out = []
    for (vendor, model, cap, band), cell in sorted(buckets.items()):
        n = cell["checked"]
        below = n < FLOOR
        bounced = cell["bounced"]
        precision_den = cell["false_bounce"] + cell["bounce_confirmed"]
        f1 = round(cell["bounce_confirmed"] / precision_den, 3) if precision_den else None
        recall_den = cell["miss"] + cell["miss_unconfirmed"] + cell["signoff_confirmed"]
        miss_rate = round((cell["miss"] + cell["miss_unconfirmed"]) / recall_den, 3) if recall_den else None
        unclassified = round(cell["unclassified"] / bounced, 3) if bounced else None
        out.append({
            "kind": "review",
            "family": "F",
            "label": "by touchpoint overlap",
            "key": {"vendor": vendor, "model": model, "binary_version": "",
                    "capability": cap, "size_band": band},
            "checked": n,
            "below_floor": below,
            "f1": {"rate": f1, "n": precision_den, "false_bounce": cell["false_bounce"],
                   "confirmed": cell["bounce_confirmed"]},
            "f2": {"rate": miss_rate, "n": recall_den,
                   "miss": cell["miss"], "miss_unconfirmed": cell["miss_unconfirmed"],
                   "confirmed": cell["signoff_confirmed"],
                   "label": "by touchpoint overlap"},
            "f3": {"unclassified": unclassified, "n": bounced,
                   "other": cell["unclassified"]},
        })
    return out


# ---- D7 / D8: probes ------------------------------------------------------------------------

def probe_candidates(db: Session, project_id: str) -> dict:
    """Closed items with a red sabotage, grouped by leaf, family fallback (criterion 8)."""
    from app.models import Item

    closed = db.scalars(select(Item).where(
        Item.project_id == project_id, Item.status == "done")).all()
    by_leaf: dict[str, list[dict]] = {leaf: [] for leaf in CAPABILITY_LEAVES}
    for item in closed:
        if not _red_sabotage(item):
            continue
        caps = [c for c in capabilities(item, None, {
            "outcome": "signed_off", "evidence": item.evidence or []})
                if c in CAPABILITY_LEAVES]
        payload = {
            "id": item.id,
            "key": item.key if hasattr(item, "key") else item.id,
            "title": item.title,
            "capabilities": caps,
            "touchpoints": list(item.touchpoints or []),
        }
        if not caps:
            continue
        for cap in caps:
            by_leaf[cap].append(payload)
    # Family fallback: a leaf with too few red-sabotage items is not a panel of its
    # own. The page groups at family instead so a small instance still gets a cell.
    families: dict[str, dict] = {}
    for fam, leaves in FAMILIES.items():
        if fam == FAMILY_OTHER:
            continue
        items = []
        seen: set[str] = set()
        thin = []
        for leaf in leaves:
            group = by_leaf[leaf]
            if len(group) < FLOOR:
                thin.append(leaf)
            for it in group:
                if it["id"] not in seen:
                    seen.add(it["id"])
                    items.append(it)
        families[fam] = {
            "leaf_ready": [leaf for leaf in leaves if len(by_leaf[leaf]) >= 1],
            "fallback": len(items) > 0 and all(len(by_leaf[leaf]) < FLOOR for leaf in leaves),
            "n": len(items),
            "items": items,
            "thin_leaves": thin,
        }
    estimate = _probe_cost_estimate(db, project_id)
    return {
        "project_id": project_id,
        "by_leaf": {leaf: by_leaf[leaf] for leaf in CAPABILITY_LEAVES if by_leaf[leaf]},
        "by_family": families,
        "floor": FLOOR,
        "estimated_tokens": estimate,
        "suggestions": probe_suggestions(db, project_id),
    }


def _probe_cost_estimate(db: Session, project_id: str, *, vendor: str | None = None,
                         model: str | None = None, capability: str | None = None) -> dict:
    """Tokens the panel's history would lead a person to expect, shown BEFORE start."""
    stmt = select(AttemptTelemetry).where(
        AttemptTelemetry.project_id == project_id,
        AttemptTelemetry.sampled == "probe",
        AttemptTelemetry.derived_at.is_not(None))
    rows = list(db.scalars(stmt).all())
    if vendor:
        rows = [r for r in rows if r.vendor == vendor]
    if model:
        rows = [r for r in rows if r.model == model]
    if capability:
        rows = [r for r in rows if capability in (r.capabilities or [])]
    reported = [r for r in rows if r.tokens_in is not None or r.tokens_out is not None]
    if not reported:
        return {"comparable": False, "reported": 0, "finished": len(rows),
                "reason": "no probe history reported tokens"}
    total = sum((r.tokens_in or 0) + (r.tokens_out or 0) for r in reported)
    per = round(total / len(reported), 1)
    return {"comparable": True, "reported": len(reported), "finished": len(rows),
            "tokens_per_attempt": per, "tokens_in": sum(r.tokens_in or 0 for r in reported),
            "tokens_out": sum(r.tokens_out or 0 for r in reported)}


def probe_suggestions(db: Session, project_id: str) -> list[dict]:
    """A newly declared vendor/model/version with no natural cell. Never on a schedule."""
    from app.models import Agent, CapabilityProbeRun

    suggestions = []
    seen_declared: set[tuple[str, str, str]] = set()
    for agent in db.scalars(select(Agent).where(Agent.project_id == project_id)).all():
        caps = agent.capabilities or {}
        vendor = caps.get("vendor") if isinstance(caps.get("vendor"), str) else None
        model = caps.get("model") if isinstance(caps.get("model"), str) else None
        version = caps.get("binary_version") if isinstance(caps.get("binary_version"), str) else ""
        if not vendor or not model:
            continue
        seen_declared.add((vendor, model, version or ""))
    natural: set[tuple[str, str, str]] = set()
    for row in db.scalars(select(AttemptTelemetry).where(
            AttemptTelemetry.project_id == project_id,
            AttemptTelemetry.derived_at.is_not(None))).all():
        if row.sampled == "probe":
            continue
        if row.vendor and row.model:
            natural.add((row.vendor, row.model, row.binary_version or ""))
    started = {(r.vendor, r.model, r.binary_version or "", r.capability)
               for r in db.scalars(select(CapabilityProbeRun).where(
                   CapabilityProbeRun.source_project_id == project_id)).all()}
    for vendor, model, version in sorted(seen_declared):
        has_natural = any(n[0] == vendor and n[1] == model and (not version or n[2] == version)
                          for n in natural)
        has_any_model = any(n[0] == vendor and n[1] == model for n in natural)
        if has_natural:
            continue
        trigger = "version_change" if has_any_model else "new_row"
        if any(s[0] == vendor and s[1] == model and s[2] == (version or "") for s in started):
            continue
        estimate = _probe_cost_estimate(db, project_id, vendor=vendor, model=model)
        suggestions.append({
            "trigger": trigger,
            "vendor": vendor,
            "model": model,
            "binary_version": version,
            "estimated_tokens": estimate,
            "reason": ("a declared version has no natural cell yet" if trigger == "version_change"
                       else "a harness first resolved with no cell for it"),
        })
    return suggestions


def start_probe_run(db: Session, *, project_id: str, user_id: str, vendor: str, model: str,
                    capability: str, item_ids: list[str], trigger: str = "new_row",
                    binary_version: str = "", api_key=None) -> dict:
    """Create the scratch project, the run row, and delegations with sampled=probe.

    One model and one leaf (or family) at a time, under the source project's caps.
    The estimate is computed before anything is written so a person can see it.
    """
    from datetime import datetime, timezone

    from app.models import Agent, CapabilityProbeRun, Item, Project
    from app.services import delegation as delegation_svc
    from app.services import fleet as fleet_svc
    from app.services import items as items_svc
    from app.services import keys as keys_svc
    from app.services import projects as projects_svc

    if trigger not in PROBE_TRIGGERS:
        raise AttemptRefused(f"trigger must be one of {PROBE_TRIGGERS}", status=422)
    if not (1 <= len(item_ids) <= 3):
        raise AttemptRefused("choose one to three items", status=422)
    cap = capability.strip()
    if cap not in CAPABILITY_LEAVES and cap not in FAMILIES:
        raise AttemptRefused(f"unknown capability {capability!r}", status=422)
    open_run = db.scalar(select(CapabilityProbeRun).where(
        CapabilityProbeRun.source_project_id == project_id,
        CapabilityProbeRun.finished_at.is_(None),
        CapabilityProbeRun.vendor == vendor,
        CapabilityProbeRun.model == model))
    if open_run is not None:
        raise AttemptRefused(
            "a probe for this model is already running; one model and one leaf at a time",
            status=409)
    items = []
    for iid in item_ids:
        item = db.get(Item, iid)
        if item is None or item.project_id != project_id:
            raise AttemptRefused(f"item not in project: {iid}", status=404)
        if item.status != "done" or not _red_sabotage(item):
            raise AttemptRefused(f"{iid} is not a closed item with a red sabotage", status=422)
        items.append(item)
    estimate = _probe_cost_estimate(db, project_id, vendor=vendor, model=model,
                                    capability=cap if cap in CAPABILITY_LEAVES else None)
    project = db.get(Project, project_id)
    caps = ((project.fleet_policy or {}) if project is not None else {}).get("caps") or {}
    per_attempt = caps.get("per_attempt_tokens")
    if per_attempt and estimate.get("comparable") and estimate["tokens_per_attempt"] > per_attempt:
        raise AttemptRefused(
            f"per_attempt_tokens {per_attempt} is below the panel's estimated "
            f"{estimate['tokens_per_attempt']} tokens", status=422)

    scratch = projects_svc.create_project(
        db, name=f"probe {vendor}:{model} {cap}", owner_user_id=user_id,
        description=f"scratch project for a {cap} probe of {vendor}:{model}")
    clones = []
    for src in items:
        clone = items_svc.create_item(
            db, title=src.title, description=src.description or "",
            tags=list(src.tags or []), touchpoints=list(src.touchpoints or []),
            project_id=scratch.id, status="next", commit=False)
        clone.evidence = list(src.evidence or [])
        clones.append(clone)
    db.flush()

    # A JWT operator is not an API-key agent, so we mint a planner row on the scratch
    # project rather than calling register_agent (which needs a key) or delegate(seat=True)
    # (which needs mint_enrolment). The seat is still a real bound Enrolment and the
    # launch post still sets sampled=probe — that is the path the cells read.
    stored_id, number = keys_svc.mint(db, scratch.id, "agent")
    now = datetime.now(timezone.utc)
    agent = Agent(
        id=stored_id, number=number, project_id=scratch.id,
        label=f"probe:{vendor}:{model}",
        capabilities={"vendor": vendor, "model": model, "binary_version": binary_version},
        active_role="planner", role_assigned_at=now, role_acked_at=now,
        state="idle", registered_at=now, last_seen_at=now,
    )
    db.add(agent)
    db.flush()
    run = CapabilityProbeRun(
        id=f"cpr_{uuid.uuid4().hex[:12]}",
        source_project_id=project_id,
        project_id=scratch.id,
        trigger=trigger,
        vendor=vendor, model=model, binary_version=binary_version or "",
        capability=cap,
        item_ids=[c.id for c in clones],
        estimated_tokens=(int(estimate["tokens_per_attempt"] * len(clones))
                          if estimate.get("comparable") else None),
        started_at=_now(),
        summary={"estimated_tokens": estimate, "source_item_ids": item_ids},
    )
    db.add(run)
    db.flush()
    launched = []
    for clone in clones:
        row, _withdrew, _code = delegation_svc.delegate(
            db, agent=agent, item=clone, lane="backend", tier="cheap",
            note=f"probe {cap}", lease_seconds=600, seat=False, api_key=api_key)
        seat, code = fleet_svc.issue_enrolment(
            db, project_id=scratch.id, role="worker", minted_by=agent.id,
            item_id=clone.id, delegation_id=row.id)
        target = Target("seat", scratch.id, seat=seat)
        record_launch(db, target=target, winner=f"{vendor}:{model}",
                      source="probe", adapter="probe")
        launched.append({"item_id": clone.id, "delegation_id": row.id,
                         "enrolment_code": code})
    db.flush()
    return {
        "id": run.id,
        "project_id": scratch.id,
        "source_project_id": project_id,
        "trigger": trigger,
        "vendor": vendor,
        "model": model,
        "binary_version": binary_version,
        "capability": cap,
        "item_ids": [c.id for c in clones],
        "estimated_tokens": estimate,
        "delegations": launched,
        "sampled": "probe",
    }

