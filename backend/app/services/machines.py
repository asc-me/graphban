"""Dev machines that reported from `gban setup` / `gban doctor` (GRPH-1442, GRPH-P67 §6).

A machine is `(user, machine_id)`. The label is for display and never merges two machines. A
report proves only what its lines say: it is `clean` only when it has lines and EVERY line is
`PASS` (§6 "all-PASS"). Any `FAIL` line makes it `failed`; no FAIL but an `UNKNOWN` (a check
that could not run) makes it `unknown` — never a pass. A report that never arrived leaves
nothing here, so "never reported" stays its own state, and only `clean` is proof for step ③.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Machine, utcnow

STATUSES = ("PASS", "FAIL", "UNKNOWN")
MAX_LINES = 200
MAX_FAILING_KEPT = 10
_FIELD_MAX = 200


def _clip(value, n: int = _FIELD_MAX) -> str:
    return str(value or "").strip()[:n]


def report(db: Session, user, *, machine_id: str, label: str = "", os: str = "",
           gban_version: str = "", gbfleet_version: str = "", harnesses: list[str] | None = None,
           project: str = "", lines: list[dict]) -> Machine:
    """Upsert the machine and record this report against `project` ('' = no project)."""
    machine_id = _clip(machine_id, 80)
    if not machine_id:
        raise ValueError("machine_id is required")
    if len(lines) > MAX_LINES:
        raise ValueError(f"a report has at most {MAX_LINES} lines")
    clean_lines = []
    for ln in lines:
        status = str(ln.get("status", "")).upper()
        if status not in STATUSES:
            raise ValueError(f"line status must be one of: {', '.join(STATUSES)}")
        clean_lines.append({"status": status, "name": _clip(ln.get("name")),
                            "detail": _clip(ln.get("detail"), 400)})
    failing = [l for l in clean_lines if l["status"] == "FAIL"]
    unknown = [l for l in clean_lines if l["status"] == "UNKNOWN"]
    state = _state(clean_lines)
    now = utcnow()
    m = db.scalar(select(Machine).where(Machine.user_id == user.id, Machine.machine_id == machine_id))
    if m is None:
        m = Machine(id="mch_" + uuid.uuid4().hex[:12], user_id=user.id, machine_id=machine_id,
                    first_reported_at=now)
        db.add(m)
    m.label = _clip(label, 80) or m.label or ""
    m.os, m.gban_version, m.gbfleet_version = _clip(os, 80), _clip(gban_version, 40), _clip(gbfleet_version, 40)
    m.harnesses = [_clip(h, 40) for h in (harnesses or [])][:10]
    projects = dict(m.projects or {})
    projects[project or ""] = {
        "state": state, "fails": len(failing), "unknowns": len(unknown),
        "failing": (failing + unknown)[:MAX_FAILING_KEPT], "at": now.isoformat()}
    m.projects = projects  # reassign: a mutated JSON dict is not change-tracked
    m.state, m.fail_count, m.last_reported_at = state, len(failing), now
    db.commit()
    return m


def _state(lines: list[dict]) -> str:
    if any(l["status"] == "FAIL" for l in lines):
        return "failed"
    if not lines or any(l["status"] != "PASS" for l in lines):
        return "unknown"  # nothing checked, or something could not be checked: not a pass
    return "clean"


def view(m: Machine) -> dict:
    return {"id": m.id, "machine_id": m.machine_id, "label": m.label, "os": m.os,
            "gban_version": m.gban_version, "gbfleet_version": m.gbfleet_version,
            "harnesses": m.harnesses or [], "state": m.state, "fail_count": m.fail_count,
            "projects": m.projects or {},
            "first_reported_at": m.first_reported_at.isoformat(),
            "last_reported_at": m.last_reported_at.isoformat()}


def list_for(db: Session, user) -> list[dict]:
    rows = db.scalars(select(Machine).where(Machine.user_id == user.id)
                      .order_by(Machine.last_reported_at.desc())).all()
    return [view(m) for m in rows]


def remove(db: Session, user, row_id: str) -> bool:
    m = db.get(Machine, row_id)
    if m is None or m.user_id != user.id:
        return False
    db.delete(m)
    db.commit()
    return True


def project_proof(db: Session, project_id: str, user_id: str | None = None) -> dict:
    """What machine reports say about `project_id`: the freshest clean machine (the proof),
    the freshest unproven one, and every machine's latest report there (`machines`, freshest
    first — the card lists each separately). `user_id` limits it to that user's machines."""
    q = select(Machine).execution_options(populate_existing=True)
    if user_id:
        q = q.where(Machine.user_id == user_id)
    best_clean = best_failed = None
    rows = []
    for m in db.scalars(q).all():
        entry = (m.projects or {}).get(project_id)
        if not entry:
            continue
        pick = (m.label or m.machine_id, entry)
        rows.append({"id": m.id, "machine": pick[0], "state": entry.get("state"),
                     "fails": entry.get("fails", 0), "unknowns": entry.get("unknowns", 0),
                     "failing": entry.get("failing", []), "at": entry["at"]})
        if entry.get("state") == "clean":
            if best_clean is None or entry["at"] > best_clean[1]["at"]:
                best_clean = pick
        elif best_failed is None or entry["at"] > best_failed[1]["at"]:
            best_failed = pick
    rows.sort(key=lambda r: r["at"], reverse=True)
    return {"clean": best_clean, "failed": best_failed, "machines": rows}
