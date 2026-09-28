"""The skill and MCP lists a fleet child loads, and where harnesses differ.

The fleet scans the operator's machine (`gbfleet surface`) and posts names here. This
module stores that report and is the copy of the comparison the Harness page reads.
The comparison is the same one `fleet/src/gbfleet/surface.py` prints — a name is a
discrepancy only when a *checked* list lacks it. A missing report is
`reported: false`, never an empty difference list, because "they match" and "nobody
looked" are the two readings an empty list has, and the quiet one is the wrong one.

Names only. `record` rebuilds each harness from an allowlist, so a URL, a header or a
path that arrived in the payload is not stored.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import HarnessSurface, utcnow

CHECKED = "checked"
PARTIAL = "partial"
UNKNOWN = "unknown"
_STATUSES = {CHECKED, PARTIAL, UNKNOWN}
SKILL_CAP = 400
MCP_CAP = 80

_UNREPORTED = (
    "no machine has reported what its harnesses load. "
    "Run `gbfleet surface` or `gban doctor` on the machine that spawns children."
)


class SurfaceRefused(ValueError):
    """The post was not a report. The caller gets 422, not a stored empty list."""


def record(db: Session, *, project_id: str, host: str, harnesses) -> dict:
    host = _host(host)
    clean = _harnesses(harnesses)
    row = db.get(HarnessSurface, (project_id, host))
    if row is None:
        row = HarnessSurface(project_id=project_id, host=host, reported_at=utcnow(),
                             payload={"harnesses": clean})
        db.add(row)
    else:
        row.reported_at = utcnow()
        row.payload = {"harnesses": clean}
    db.flush()
    return for_project(db, project_id)


def for_project(db: Session, project_id: str) -> dict:
    rows = list(db.scalars(
        select(HarnessSurface).where(HarnessSurface.project_id == project_id)).all())
    if not rows:
        return _none(_UNREPORTED)
    rows.sort(key=lambda r: _stamp(r.reported_at), reverse=True)
    newest = rows[0]
    harnesses = (newest.payload or {}).get("harnesses") or []
    gaps = discrepancies(harnesses)
    return {
        "reported": True,
        "reason": "",
        "host": newest.host,
        "reported_at": newest.reported_at.isoformat() if newest.reported_at else None,
        "other_hosts": [
            {"host": row.host, "reported_at": row.reported_at.isoformat() if row.reported_at else None}
            for row in rows[1:]
        ],
        "harnesses": harnesses,
        "skills": gaps["skills"],
        "mcps": gaps["mcps"],
        "notes": gaps["notes"],
    }


def for_org() -> dict:
    """An org read spans projects and machines. Showing one project's laptop as the org's
    answer would be a match that is really one host."""
    return _none(
        "surface reports are per project. Open a project to see whether its harnesses "
        "load the same skills and MCP servers."
    )


def discrepancies(harnesses: list[dict]) -> dict:
    """Kept in step with `fleet/src/gbfleet/surface.py`."""
    return {
        "skills": _gaps(harnesses, "skills"),
        "mcps": _gaps(harnesses, "mcps"),
        "notes": _notes(harnesses),
    }


def _stamp(value: datetime | None) -> datetime:
    """SQLite hands back naive timestamps; a row still in this session is aware."""
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _none(reason: str) -> dict:
    return {
        "reported": False,
        "reason": reason,
        "harnesses": [],
        "skills": None,
        "mcps": None,
        "notes": [],
    }


def _host(host: str) -> str:
    if not isinstance(host, str):
        raise SurfaceRefused("host must be the machine name")
    clean = host.strip().replace("\n", "").replace("\r", "")
    if not clean:
        raise SurfaceRefused("host must be the machine name")
    return clean[:128]


def _harnesses(raw) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        raise SurfaceRefused("name the harnesses that were checked")
    if len(raw) > 12:
        raise SurfaceRefused("at most 12 harnesses")
    out = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        vendor = entry.get("vendor")
        if not isinstance(vendor, str) or not vendor.strip() or any(c in vendor for c in "\n\r"):
            continue
        out.append(_one(entry, vendor.strip()[:32]))
    if not out:
        raise SurfaceRefused("name the harnesses that were checked")
    return out


def _one(entry: dict, vendor: str) -> dict:
    skills_status, skills_reason, skills, _skills_omitted = _side(entry, "skills")
    disabled = _items(entry.get("disabled"))[:SKILL_CAP] if skills_status == CHECKED else []
    mcp_status, mcp_reason, mcps, _mcp_omitted = _side(entry, "mcps")
    return {
        "vendor": vendor,
        "installed": bool(entry.get("installed")),
        "skills_status": skills_status,
        "skills_reason": skills_reason,
        "skills": skills,
        "disabled": disabled,
        "mcps_status": mcp_status,
        "mcps_reason": mcp_reason,
        "mcps": mcps,
    }


def _side(entry: dict, field: str) -> tuple[str, str, list[dict], bool]:
    """A checked list that was omitted is unknown. Storing it as `[]` would say the
    child loads nothing, which is the reading this report exists to avoid."""
    status = entry.get(f"{field}_status")
    if status not in _STATUSES:
        status = UNKNOWN
    reason = entry.get(f"{field}_reason")
    reason = reason.strip()[:400] if isinstance(reason, str) else ""
    omitted = field not in entry or not isinstance(entry.get(field), list)
    cap = SKILL_CAP if field == "skills" else MCP_CAP
    if omitted:
        # A status with no list is not a scan that found nothing. `checked` + missing
        # would be stored as a child that loads nothing; `partial` + missing would be
        # stored as a scan that ran and found zero.
        if status == CHECKED:
            reason = "the report said this list was complete and then omitted it"
        elif status == PARTIAL and not reason:
            reason = "the report said this scan was partial and then omitted the names it found"
        if status != UNKNOWN:
            status = UNKNOWN
        return status, reason, [], True
    items = _items(entry.get(field))
    if status == CHECKED and len(items) > cap:
        status = PARTIAL
        reason = (reason + " " if reason else "") + (
            f"list truncated at {cap}; names past the cap are not evidence of absence"
        )
        items = items[:cap]
    else:
        items = items[:cap]
    return status, reason, items, False


def _items(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str):
            continue
        name = name.strip()
        if not name or any(c in name for c in "\n\r") or name in seen:
            continue
        seen.add(name)
        source = entry.get("source")
        if not isinstance(source, str) or not source.strip():
            source = "unreported"
        out.append({"name": name[:80], "source": source.strip()[:32]})
    return out


def _names(harness: dict, field: str) -> set[str]:
    return {
        item["name"] for item in harness.get(field) or []
        if isinstance(item, dict) and item.get("name")
    }


def _gaps(harnesses: list[dict], field: str) -> dict:
    status_field = f"{field}_status"
    checked = [h for h in harnesses if h.get(status_field) == CHECKED]
    partial = [h for h in harnesses if h.get(status_field) == PARTIAL]
    compared = [h["vendor"] for h in checked]
    partial_vendors = [h["vendor"] for h in partial]
    if not checked:
        return {
            "compared": [],
            "partial": partial_vendors,
            "rows": [],
            "reason": "no harness was fully checked, so a missing name is not a discrepancy",
        }
    enabled = {h["vendor"]: _names(h, field) for h in checked}
    disabled = {h["vendor"]: _names(h, "disabled") for h in checked} if field == "skills" else {}
    partial_names = {h["vendor"]: _names(h, field) for h in partial}
    universe: set[str] = set()
    for names in (*enabled.values(), *disabled.values(), *partial_names.values()):
        universe |= names
    rows = []
    for name in sorted(universe):
        present = [v for v in compared if name in enabled.get(v, set())]
        disabled_on = [v for v in compared if name in disabled.get(v, set()) and v not in present]
        absent = [v for v in compared if v not in present and v not in disabled_on]
        partial_present = [v for v, names in partial_names.items() if name in names]
        if not present and not partial_present:
            continue
        if not absent and not disabled_on:
            continue
        rows.append({
            "name": name,
            "present": present,
            "absent": absent,
            "disabled_on": disabled_on,
            "partial_present": partial_present,
        })
    reason = ""
    if len(checked) == 1 and not rows:
        reason = (
            f"only {compared[0]} has a complete list, so there is no second list to compare it with"
        )
    return {"compared": compared, "partial": partial_vendors, "rows": rows, "reason": reason}


def _notes(harnesses: list[dict]) -> list[dict]:
    notes = []
    for harness in harnesses:
        for kind in ("skills", "mcps"):
            status = harness.get(f"{kind}_status")
            if status == CHECKED:
                continue
            notes.append({
                "vendor": harness["vendor"],
                "kind": kind,
                "status": status or UNKNOWN,
                "reason": harness.get(f"{kind}_reason") or "",
            })
    return notes
