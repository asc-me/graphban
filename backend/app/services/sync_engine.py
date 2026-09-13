"""Sync engine (PRD-P10 / GRPH-188).

Tracker → hub mirror: idempotent and echo-safe. The engine owns three concerns:

1. **Fingerprint store** — every outbound write (status, assignee, comment) is
   fingerprinted by (link_id, issue_id, field, expected_version). When the tracker
   webhook echoes that change back, the fingerprint matches and the hub drops it.
   Do NOT reuse services/idempotency.py — that maps create-tool retries.

2. **Mirror** — the hub stores the latest snapshot of each mirrored issue in
   TrackerMirror. Reconcile diffs incoming state against this to detect external
   edits. Description is READ-from-tracker only — never written back.

3. **Conflict policy** — tracker wins on any field it owns (title, description,
   status, assignee, labels). AgentLedger-only fields (local links, provenance,
   memory) are never overwritten. Field mapping is explicitly lossy and configurable
   per TrackerLink.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select, and_
from sqlalchemy.orm import Session

from app.models import SyncFingerprint, TrackerMirror, TrackerLink, utcnow
from app.services.linear import LinearIssue, LINEAR_STATE_TYPE_TO_STATUS

logger = logging.getLogger("graphban.sync_engine")

# Tracker-owned fields: the tracker wins on these during reconcile.
TRACKER_OWNED_FIELDS = frozenset({
    "title", "description", "canonical_status", "assignee_id", "assignee_name", "labels",
})

# Default field mapping: Linear field → mirror field. Empty means identity.
DEFAULT_FIELD_MAPPING: dict[str, str] = {
    "title": "title",
    "description": "description",
    "state_type": "canonical_status",
    "assignee_id": "assignee_id",
    "assignee_name": "assignee_name",
    "labels": "labels",
    "identifier": "identifier",
    "url": "url",
    "updated_at": "tracker_updated_at",
}


@dataclass
class ReconcileDiff:
    """Result of comparing incoming tracker state against the mirror."""
    issue_id: str
    changed_fields: list[str] = field(default_factory=list)
    is_echo: bool = False
    external_edit: bool = False
    applied: dict = field(default_factory=dict)

    @property
    def has_changes(self) -> bool:
        return bool(self.changed_fields)


@dataclass
class FingerprintMatch:
    """A fingerprint that matches an incoming change."""
    fingerprint: SyncFingerprint
    field: str
    write_token: str


def _gen_fp_id() -> str:
    return f"sfp_{uuid.uuid4().hex[:12]}"


def _write_token(link_id: str, issue_id: str, field_name: str, version: str) -> str:
    """Deterministic write token from the fingerprint components.

    The tracker does not echo this token back; it is a hub-side secret that lets
    us distinguish our own write from an external edit that happens to set the
    same value. The fingerprint row is the durable record; the token is the
    short-lived proof.
    """
    raw = f"{link_id}:{issue_id}:{field_name}:{version}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ---- Fingerprint store ----

def fingerprint_write(
    db: Session,
    *,
    link_id: str,
    issue_id: str,
    field_name: str,
    expected_version: str,
) -> SyncFingerprint:
    """Record an outbound write fingerprint. Call BEFORE writing to the tracker.

    Returns the fingerprint row. The write_token is deterministic from the
    components, so a second call with the same inputs produces the same token.
    """
    token = _write_token(link_id, issue_id, field_name, expected_version)
    fp = SyncFingerprint(
        id=_gen_fp_id(),
        link_id=link_id,
        issue_id=issue_id,
        field=field_name,
        expected_version=expected_version,
        write_token=token,
    )
    db.add(fp)
    db.commit()
    db.refresh(fp)
    return fp


def check_echo(
    db: Session,
    *,
    link_id: str,
    issue_id: str,
    field_name: str,
    incoming_version: str,
) -> FingerprintMatch | None:
    """Check if an incoming change matches an outstanding fingerprint.

    Returns the match if found (echo — drop the change), or None (external edit
    — apply it). Does NOT consume the fingerprint; call consume_fingerprint after
    applying.
    """
    stmt = select(SyncFingerprint).where(
        and_(
            SyncFingerprint.link_id == link_id,
            SyncFingerprint.issue_id == issue_id,
            SyncFingerprint.field == field_name,
            SyncFingerprint.consumed == False,  # noqa: E712
        )
    ).order_by(SyncFingerprint.created_at.desc())
    fp = db.scalars(stmt).first()
    if fp is None:
        return None
    expected_token = _write_token(link_id, issue_id, field_name, incoming_version)
    if fp.write_token == expected_token:
        return FingerprintMatch(fingerprint=fp, field=field_name, write_token=expected_token)
    return None


def consume_fingerprint(db: Session, *, fingerprint_id: str) -> bool:
    """Mark a fingerprint as consumed (matched). Returns True if a row was updated."""
    fp = db.get(SyncFingerprint, fingerprint_id)
    if fp is None or fp.consumed:
        return False
    fp.consumed = True
    db.commit()
    return True


def prune_fingerprints(db: Session, *, link_id: str, older_than: datetime) -> int:
    """Remove consumed fingerprints older than the cutoff. Returns the count deleted."""
    stmt = select(SyncFingerprint).where(
        and_(
            SyncFingerprint.link_id == link_id,
            SyncFingerprint.consumed == True,  # noqa: E712
            SyncFingerprint.created_at < older_than,
        )
    )
    rows = db.scalars(stmt).all()
    count = len(rows)
    for row in rows:
        db.delete(row)
    db.commit()
    return count


# ---- Mirror ----

def _issue_to_mirror_fields(issue: LinearIssue) -> dict:
    """Map a LinearIssue to the fields stored in TrackerMirror."""
    return {
        "tracker_kind": "linear",
        "identifier": issue.identifier,
        "title": issue.title,
        "description": issue.description,
        "canonical_status": issue.canonical_status,
        "assignee_id": issue.assignee_id,
        "assignee_name": issue.assignee_name or "",
        "labels": issue.labels,
        "tracker_updated_at": issue.updated_at,
        "version": issue.updated_at,
        "url": issue.url,
    }


def mirror_issue(
    db: Session,
    *,
    link_id: str,
    issue: LinearIssue,
) -> TrackerMirror:
    """Upsert a mirrored issue from the tracker. Returns the mirror row."""
    existing = db.get(TrackerMirror, issue.id)
    if existing is None:
        mirror = TrackerMirror(
            issue_id=issue.id,
            link_id=link_id,
            **_issue_to_mirror_fields(issue),
        )
        db.add(mirror)
    else:
        for k, v in _issue_to_mirror_fields(issue).items():
            setattr(existing, k, v)
        existing.mirrored_at = utcnow()
    db.commit()
    if existing is None:
        db.refresh(db.query(TrackerMirror).filter_by(issue_id=issue.id).first())
        return db.get(TrackerMirror, issue.id)
    db.refresh(existing)
    return existing


def mirror_issues_bulk(
    db: Session,
    *,
    link_id: str,
    issues: list[LinearIssue],
) -> int:
    """Mirror a batch of issues. Returns the count upserted."""
    count = 0
    for issue in issues:
        mirror_issue(db, link_id=link_id, issue=issue)
        count += 1
    return count


def get_mirror(db: Session, *, issue_id: str) -> TrackerMirror | None:
    """Fetch the mirror row for an issue."""
    return db.get(TrackerMirror, issue_id)


def list_mirror_by_link(db: Session, *, link_id: str) -> list[TrackerMirror]:
    """List all mirrored issues for a link."""
    return list(
        db.scalars(
            select(TrackerMirror)
            .where(TrackerMirror.link_id == link_id)
            .order_by(TrackerMirror.mirrored_at)
        ).all()
    )


# ---- Reconcile ----

def reconcile_issue(
    db: Session,
    *,
    link: TrackerLink,
    incoming: LinearIssue,
) -> ReconcileDiff:
    """Compare incoming tracker state against the mirror and compute the diff.

    Echo suppression: if a field change matches an outstanding fingerprint, it is
    an echo (our own write coming back) — drop it and consume the fingerprint.

    Conflict policy: tracker wins on TRACKER_OWNED_FIELDS. The mirror is updated
    to reflect the incoming state. Returns the diff with applied changes.
    """
    diff = ReconcileDiff(issue_id=incoming.id)
    mirror = get_mirror(db, issue_id=incoming.id)

    incoming_fields = _issue_to_mirror_fields(incoming)

    if mirror is None:
        # First time seeing this issue — mirror it, report all fields as new.
        mirror_issue(db, link_id=link.id, issue=incoming)
        diff.changed_fields = list(incoming_fields.keys())
        diff.applied = incoming_fields
        return diff

    # Compare each tracker-owned field.
    for field_name, incoming_value in incoming_fields.items():
        if field_name not in TRACKER_OWNED_FIELDS:
            continue
        current_value = getattr(mirror, field_name, None)
        if _values_equal(current_value, incoming_value):
            continue

        # Check echo suppression.
        match = check_echo(
            db,
            link_id=link.id,
            issue_id=incoming.id,
            field_name=field_name,
            incoming_version=incoming.updated_at,
        )
        if match is not None:
            diff.is_echo = True
            consume_fingerprint(db, fingerprint_id=match.fingerprint.id)
            logger.debug("sync: echo suppressed field=%s issue=%s", field_name, incoming.id)
            continue

        # External edit — tracker wins.
        diff.changed_fields.append(field_name)
        diff.applied[field_name] = incoming_value
        diff.external_edit = True

    if diff.has_changes:
        # Apply the tracker-owned changes to the mirror.
        for field_name, value in diff.applied.items():
            setattr(mirror, field_name, value)
        mirror.mirrored_at = utcnow()
        db.commit()
        db.refresh(mirror)
        logger.info(
            "sync: applied external edit issue=%s fields=%s",
            incoming.id, diff.changed_fields,
        )

    return diff


def _values_equal(a, b) -> bool:
    """Compare two values for equality, handling lists and None."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, list) and isinstance(b, list):
        return sorted(a) == sorted(b)
    return a == b


# ---- Field mapping ----

def apply_field_mapping(
    link: TrackerLink,
    incoming: dict,
) -> dict:
    """Apply the per-link field mapping to incoming tracker data.

    The mapping is explicitly lossy: fields not in the mapping are dropped.
    Empty mapping = use DEFAULT_FIELD_MAPPING. Returns the mapped dict with
    mirror-field keys.
    """
    mapping = link.field_mapping or DEFAULT_FIELD_MAPPING
    result = {}
    for tracker_field, mirror_field in mapping.items():
        if tracker_field in incoming:
            result[mirror_field] = incoming[tracker_field]
    return result
