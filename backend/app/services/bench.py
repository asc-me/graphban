"""Baseline bench runs and their results (GRPH-P98 §4 and §6, GRPH-1856).

Two writes an agent key makes:

* `open_run` creates a scratch project for the run and mints one bound seat per task on it. It
  never creates an item or an agent on the source project (P98 D10).
* `post_result` stores one baseline record keyed by vendor, model, binary_version, suite id
  and suite version. It never writes `AttemptTelemetry` and is never read into a natural or
  probe rate: a baseline is shown beside the grid's cells under its own label (P98 D11).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.harness import CAPABILITY_LEAVES, FAMILIES, AttemptRefused

#: P98 §5. `never_registered` and `harness_error` are plumbing: counted, never scored.
OUTCOMES = ("never_registered", "no_branch", "check_red", "check_green", "timed_out",
            "harness_error")
PLUMBING = ("never_registered", "harness_error")
MAX_TASKS = 200
#: The column widths in `bench_runs` / `bench_baselines`: a longer value is refused by name
#: rather than reaching Postgres as a truncation error.
FIELD_MAX = {"vendor": 32, "model": 64, "binary_version": 32, "suite_id": 64,
             "suite_version": 32}


def _text(body: dict, field: str, *, required: bool = True) -> str:
    value = body.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise AttemptRefused(f"{field} is required", status=422)
        return ""
    if not isinstance(value, str):
        raise AttemptRefused(f"{field} must be a string", status=422)
    value = value.strip()
    limit = FIELD_MAX.get(field)
    if limit is not None and len(value) > limit:
        raise AttemptRefused(f"{field} must be at most {limit} characters", status=422)
    return value


def _identity(body: dict) -> dict:
    """The fields every bench write names. A missing one is refused by name."""
    out = {f: _text(body, f) for f in ("vendor", "model", "suite_id", "suite_version")}
    out["binary_version"] = _text(body, "binary_version", required=False)
    return out


def open_run(db: Session, *, project_id: str, user_id: str, body: dict) -> dict:
    """Create the scratch project and one bound seat per task. Source project untouched."""
    from app.models import Agent, BenchRun, Project
    from app.services import delegation as delegation_svc
    from app.services import fleet as fleet_svc
    from app.services import items as items_svc
    from app.services import keys as keys_svc
    from app.services import projects as projects_svc

    ident = _identity(body)
    tasks = body.get("tasks")
    if not isinstance(tasks, list) or not (1 <= len(tasks) <= MAX_TASKS):
        raise AttemptRefused(f"tasks must be a list of 1 to {MAX_TASKS}", status=422)
    seen: set[str] = set()
    for t in tasks:
        tid = t.get("id") if isinstance(t, dict) else None
        if not isinstance(tid, str) or not tid.strip():
            raise AttemptRefused("every task needs an id", status=422)
        if tid in seen:
            raise AttemptRefused(f"task id {tid!r} appears twice", status=422)
        seen.add(tid)
    if db.get(Project, project_id) is None:
        raise AttemptRefused("project not found", status=404)

    scratch = projects_svc.create_project(
        db, name=f"bench {ident['vendor']}:{ident['model']} {ident['suite_id']}",
        owner_user_id=user_id,
        description=f"scratch project for a baseline run of {ident['vendor']}:{ident['model']}")
    stored_id, number = keys_svc.mint(db, scratch.id, "agent")
    now = datetime.now(timezone.utc)
    agent = Agent(
        id=stored_id, number=number, project_id=scratch.id,
        label=f"bench:{ident['vendor']}:{ident['model']}",
        capabilities={"vendor": ident["vendor"], "model": ident["model"],
                      "binary_version": ident["binary_version"]},
        active_role="planner", role_assigned_at=now, role_acked_at=now,
        state="idle", registered_at=now, last_seen_at=now,
    )
    db.add(agent)
    db.flush()
    seats = []
    for t in tasks:
        item = items_svc.create_item(
            db, title=str(t.get("title") or t["id"]), description=str(t.get("description") or ""),
            tags=["bench"], touchpoints=list(t.get("touchpoints") or []),
            project_id=scratch.id, status="next", commit=False)
        db.flush()
        row, _withdrew, _code = delegation_svc.delegate(
            db, agent=agent, item=item, lane="backend", tier="cheap",
            note=f"bench {ident['suite_id']} {t['id']}", lease_seconds=600, seat=False)
        _seat, code = fleet_svc.issue_enrolment(
            db, project_id=scratch.id, role="worker", minted_by=agent.id,
            item_id=item.id, delegation_id=row.id)
        seats.append({"task_id": t["id"], "item_id": item.id, "delegation_id": row.id,
                      "enrolment_code": code})
    run = BenchRun(
        id=f"bnr_{uuid.uuid4().hex[:12]}", source_project_id=project_id, project_id=scratch.id,
        tasks=[{"task_id": s["task_id"], "item_id": s["item_id"]} for s in seats], **ident)
    db.add(run)
    db.flush()
    return {"id": run.id, "project_id": scratch.id, "source_project_id": project_id,
            **ident, "seats": seats}


def _rollups(outcomes: list[dict]) -> dict:
    """Per capability: scored `n`, `green`, and the plumbing excluded. Server-computed."""
    out: dict[str, dict] = {}
    for o in outcomes:
        for cap in o["capabilities"] or ["uncategorised"]:
            cell = out.setdefault(cap, {"n": 0, "green": 0, "excluded": 0})
            if o["outcome"] in PLUMBING:
                cell["excluded"] += 1
            else:
                cell["n"] += 1
                cell["green"] += o["outcome"] == "check_green"
    return out


def view(row) -> dict:
    return {"id": row.id, "run_id": row.run_id, "vendor": row.vendor, "model": row.model,
            "binary_version": row.binary_version, "suite_id": row.suite_id,
            "suite_version": row.suite_version, "outcomes": row.outcomes or [],
            "rollups": row.rollups or {}, "tokens": row.tokens,
            "wall_seconds": row.wall_seconds, "posted_at": row.posted_at.isoformat()}


def post_result(db: Session, *, project_id: str, body: dict) -> dict:
    """Store one baseline record. Writes `bench_baselines` and nothing else."""
    from app.models import BenchBaseline, BenchRun

    ident = _identity(body)
    raw = body.get("outcomes")
    if not isinstance(raw, list) or not raw:
        raise AttemptRefused("outcomes is required: one entry per task", status=422)
    outcomes: list[dict] = []
    for o in raw:
        if not isinstance(o, dict) or not isinstance(o.get("task_id"), str) or not o["task_id"]:
            raise AttemptRefused("every outcome needs a task_id", status=422)
        if o.get("outcome") not in OUTCOMES:
            raise AttemptRefused(
                f"outcome for {o['task_id']} must be one of {list(OUTCOMES)}", status=422)
        caps = o.get("capabilities") or []
        if not isinstance(caps, list):
            raise AttemptRefused("capabilities must be a list", status=422)
        for c in caps:
            if c not in CAPABILITY_LEAVES and c not in FAMILIES:
                raise AttemptRefused(f"unknown capability {c!r}", status=422)
        tokens, wall = o.get("tokens"), o.get("wall_seconds")
        for name, v in (("tokens", tokens), ("wall_seconds", wall)):
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
                raise AttemptRefused(f"{name} must be a non-negative number or null", status=422)
        outcomes.append({"task_id": o["task_id"], "outcome": o["outcome"], "capabilities": caps,
                         "tokens": tokens, "wall_seconds": wall})
    run_id = body.get("run_id")
    if run_id is not None:
        run = db.get(BenchRun, run_id)
        if run is None or run.source_project_id != project_id:
            raise AttemptRefused("run not found", status=404)
    reported = [o["tokens"] for o in outcomes if o["tokens"] is not None]
    walls = [o["wall_seconds"] for o in outcomes if o["wall_seconds"] is not None]
    row = BenchBaseline(
        id=f"bnb_{uuid.uuid4().hex[:12]}", source_project_id=project_id, run_id=run_id,
        outcomes=outcomes, rollups=_rollups(outcomes),
        tokens=int(sum(reported)) if reported else None,
        wall_seconds=float(sum(walls)) if walls else None, **ident)
    db.add(row)
    db.flush()
    return view(row)


def list_results(db: Session, project_id: str, *, vendor: str | None = None,
                 model: str | None = None) -> list[dict]:
    from app.models import BenchBaseline

    q = select(BenchBaseline).where(BenchBaseline.source_project_id == project_id)
    if vendor:
        q = q.where(BenchBaseline.vendor == vendor)
    if model:
        q = q.where(BenchBaseline.model == model)
    return [view(r) for r in db.scalars(q.order_by(BenchBaseline.posted_at.desc())).all()]
