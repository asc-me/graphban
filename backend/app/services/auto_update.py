"""Opt-in auto-update for this box (GRPH-1299).

A check window and an install window, each a set of weekdays plus a local start/end time in
the configured timezone. The leader's loop calls `tick` every `AUTO_UPDATE_TICK_SECONDS`.

* A check runs at most once per window occurrence (`last_check_at` is claimed with a
  conditional UPDATE before the GitHub call).
* An install is attempted at most once per tag: `last_auto_install_tag` is claimed with a
  conditional UPDATE BEFORE `apply` is called, because apply recreates this process. A tag
  whose attempt failed therefore waits for a newer tag or a manual Install.

The window math is pure (`open_occurrence`, `next_start`) so tests drive it with fixed clocks.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AutoUpdateConfig
from app.services import instance_update

logger = logging.getLogger("graphban.auto_update")

SINGLETON_ID = "deployment"
ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

#: What the panel may write. The `last_*` runtime columns are deliberately absent.
WRITABLE = ("enabled", "timezone", "check_days", "check_start", "check_end",
            "install_days", "install_start", "install_end")


# ── pure window math ─────────────────────────────────────────────────────────────────────

def parse_hhmm(value: str) -> dtime:
    m = _HHMM.match(value or "")
    if not m:
        raise ValueError(f"{value!r} is not a time of day (HH:MM)")
    return dtime(int(m.group(1)), int(m.group(2)))


def get_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as e:
        raise ValueError(f"{name!r} is not a known timezone") from e


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _occurrence(day, days, start: dtime, tz: ZoneInfo) -> datetime | None:
    """Start of the occurrence that begins on local date `day`, or None if not a window day."""
    if day.weekday() not in days:
        return None
    return datetime.combine(day, start, tzinfo=tz)


def open_occurrence(now: datetime, days, start: str, end: str, tz_name: str
                    ) -> datetime | None:
    """UTC start of the window occurrence `now` falls inside, else None.

    Wall-clock comparison in the local zone, so a DST change day shifts the window with the
    clock. A window with start > end crosses midnight and belongs to the day it starts on.
    """
    tz = get_zone(tz_name)
    s, e = parse_hhmm(start), parse_hhmm(end)
    local = _utc(now).astimezone(tz)
    today, t = local.date(), local.time().replace(tzinfo=None)
    if s < e:
        if s <= t < e:
            occ = _occurrence(today, days, s, tz)
            return occ.astimezone(timezone.utc) if occ else None
        return None
    # Crosses midnight (s > e); s == e is refused at the API and never matches here.
    if s == e:
        return None
    if t >= s:
        occ = _occurrence(today, days, s, tz)
    elif t < e:
        occ = _occurrence(today - timedelta(days=1), days, s, tz)
    else:
        occ = None
    return occ.astimezone(timezone.utc) if occ else None


def next_start(now: datetime, days, start: str, tz_name: str) -> datetime | None:
    """UTC start of the first occurrence strictly after `now`, or None with no days."""
    if not days:
        return None
    tz = get_zone(tz_name)
    s = parse_hhmm(start)
    local = _utc(now).astimezone(tz)
    for offset in range(0, 9):
        occ = _occurrence(local.date() + timedelta(days=offset), days, s, tz)
        if occ is not None and occ.astimezone(timezone.utc) > _utc(now):
            return occ.astimezone(timezone.utc)
    return None


def validate_patch(values: dict) -> None:
    """Raise ValueError for a bad timezone, time or day set. The router maps it to 422."""
    if "timezone" in values:
        get_zone(values["timezone"])
    for key in ("check_start", "check_end", "install_start", "install_end"):
        if key in values:
            parse_hhmm(values[key])
    for key in ("check_days", "install_days"):
        if key in values:
            d = values[key]
            if (not isinstance(d, list) or not d
                    or any(isinstance(x, bool) or not isinstance(x, int) or not 0 <= x <= 6
                           for x in d)):
                raise ValueError(f"{key} must be a non-empty list of weekdays 0 (Mon) to 6 (Sun)")


# ── config ───────────────────────────────────────────────────────────────────────────────

def get_config(db: Session) -> AutoUpdateConfig:
    row = db.get(AutoUpdateConfig, SINGLETON_ID)
    if row is None:
        row = AutoUpdateConfig(id=SINGLETON_ID)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def update_config(db: Session, values: dict) -> AutoUpdateConfig:
    """Apply a PATCH. Only `WRITABLE` keys are read; validation is on the merged result."""
    values = {k: v for k, v in values.items() if k in WRITABLE and v is not None}
    validate_patch(values)
    row = get_config(db)
    merged = {k: values.get(k, getattr(row, k)) for k in WRITABLE}
    for pre in ("check", "install"):
        if merged[f"{pre}_start"] == merged[f"{pre}_end"]:
            raise ValueError(f"{pre} window start and end must differ")
    for k, v in values.items():
        setattr(row, k, sorted(set(v)) if k.endswith("_days") else v)
    db.commit()
    db.refresh(row)
    return row


def unavailable_reason(*, hosted: bool | None = None, can_apply: bool | None = None) -> str:
    """Why auto-update cannot be offered on this box, or '' when it can."""
    h = settings.hosted_mode if hosted is None else hosted
    if h:
        return "hosted instances are updated by the operator"
    ok = (instance_update.helper_present() or instance_update.native_present()
          if can_apply is None else can_apply)
    if not ok:
        return ("no apply path — the compose helper is not running and this is not a "
                "native install")
    return ""


def _iso(dt: datetime | None) -> str | None:
    return _utc(dt).isoformat() if dt else None


def status(db: Session, *, now: datetime | None = None, hosted: bool | None = None,
           can_apply: bool | None = None) -> dict:
    now = _utc(now or datetime.now(timezone.utc))
    c = get_config(db)
    check_occ = open_occurrence(now, c.check_days, c.check_start, c.check_end, c.timezone)
    last = _utc(c.last_check_at) if c.last_check_at else None
    if check_occ is not None and (last is None or last < check_occ):
        next_check = now  # due: inside the window and not yet checked in it
    else:
        next_check = next_start(now, c.check_days, c.check_start, c.timezone)
    reason = unavailable_reason(hosted=hosted, can_apply=can_apply)
    return {
        "available": not reason, "unavailable_reason": reason,
        "enabled": c.enabled, "timezone": c.timezone,
        "check": {"days": list(c.check_days), "start": c.check_start, "end": c.check_end},
        "install": {"days": list(c.install_days), "start": c.install_start,
                    "end": c.install_end},
        "next_check_at": _iso(next_check) if c.enabled else None,
        "last_check_at": _iso(c.last_check_at), "last_check_state": c.last_check_state,
        "last_auto_install_tag": c.last_auto_install_tag,
        "last_auto_install_at": _iso(c.last_auto_install_at),
        "last_auto_install_outcome": c.last_auto_install_outcome,
        "last_auto_install_error": c.last_auto_install_error,
    }


# ── the pass ─────────────────────────────────────────────────────────────────────────────

def _claim_check(db: Session, occ_start: datetime, now: datetime) -> bool:
    """Conditional UPDATE: only one caller per window occurrence wins."""
    res = db.execute(
        update(AutoUpdateConfig)
        .where(AutoUpdateConfig.id == SINGLETON_ID,
               or_(AutoUpdateConfig.last_check_at.is_(None),
                   AutoUpdateConfig.last_check_at < occ_start))
        .values(last_check_at=now), execution_options={"synchronize_session": False})
    db.commit()
    return res.rowcount == 1


def _claim_install(db: Session, tag: str, now: datetime) -> bool:
    """Conditional UPDATE, committed BEFORE apply: a tag is attempted once, ever."""
    res = db.execute(
        update(AutoUpdateConfig)
        .where(AutoUpdateConfig.id == SINGLETON_ID,
               AutoUpdateConfig.last_auto_install_tag != tag)
        .values(last_auto_install_tag=tag, last_auto_install_at=now,
                last_auto_install_outcome="started", last_auto_install_error=""),
        execution_options={"synchronize_session": False})
    db.commit()
    return res.rowcount == 1


def _settle(db: Session, c: AutoUpdateConfig, progress_fn) -> None:
    """Resolve a `started` attempt from what the box now says. The process that called
    apply is usually gone, so the outcome is read back on a later tick."""
    if c.last_auto_install_outcome != "started":
        return
    if instance_update.running()["version"] == c.last_auto_install_tag:
        c.last_auto_install_outcome = "succeeded"
    else:
        p = progress_fn()
        if p.get("tag") == c.last_auto_install_tag and p.get("state") == "failed":
            c.last_auto_install_outcome = "failed"
            c.last_auto_install_error = (p.get("error") or "install failed")[:400]
        elif p.get("tag") == c.last_auto_install_tag and p.get("state") == "succeeded":
            c.last_auto_install_outcome = "succeeded"
    db.commit()


def tick(db: Session, *, now: datetime | None = None, check_fn=None, apply_fn=None,
         progress_fn=None, hosted: bool | None = None, can_apply: bool | None = None
         ) -> dict:
    """One pass. Returns what it did: {"checked": bool, "install": "" | tag | reason}."""
    now = _utc(now or datetime.now(timezone.utc))
    out = {"checked": False, "installed": "", "skipped": ""}
    c = get_config(db)
    if not c.enabled:
        out["skipped"] = "disabled"
        return out
    reason = unavailable_reason(hosted=hosted, can_apply=can_apply)
    if reason:
        out["skipped"] = reason
        return out
    check_fn = check_fn or (lambda: instance_update.check_cached(refresh=True))
    apply_fn = apply_fn or instance_update.apply
    progress_fn = progress_fn or instance_update.progress

    occ = open_occurrence(now, c.check_days, c.check_start, c.check_end, c.timezone)
    if occ is not None and _claim_check(db, occ, now):
        try:
            payload = check_fn()
            state = payload.get("state") or "unknown"
            tag = ((payload.get("latest") or {}).get("tag") or "") if state == "available" else ""
        except Exception as e:  # noqa: BLE001 — a failed check is unknown, not a crashed loop
            logger.warning("auto-update check failed: %s", type(e).__name__)
            state, tag = "unknown", ""
        db.execute(update(AutoUpdateConfig).where(AutoUpdateConfig.id == SINGLETON_ID)
                   .values(last_check_state=state, last_check_tag=tag))
        db.commit()
        out["checked"] = True
    db.expire_all()
    c = get_config(db)
    _settle(db, c, progress_fn)

    if open_occurrence(now, c.install_days, c.install_start, c.install_end,
                       c.timezone) is None:
        out["skipped"] = out["skipped"] or "outside the install window"
        return out
    if c.last_check_state != "available" or not c.last_check_tag:
        out["skipped"] = f"last check is {c.last_check_state or 'missing'}, not available"
        return out
    if progress_fn().get("state") == "running":
        out["skipped"] = "an install is already running"
        return out
    tag = c.last_check_tag
    if not _claim_install(db, tag, now):
        out["skipped"] = f"{tag} was already auto-installed or attempted"
        return out
    try:
        got = apply_fn(tag)
    except Exception as e:  # noqa: BLE001 — the attempt is already recorded
        got = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if not got.get("ok"):
        db.execute(update(AutoUpdateConfig).where(AutoUpdateConfig.id == SINGLETON_ID)
                   .values(last_auto_install_outcome="failed",
                           last_auto_install_error=str(got.get("error") or "apply refused")[:400]))
        db.commit()
        out["skipped"] = "apply failed"
        return out
    out["installed"] = tag
    return out
