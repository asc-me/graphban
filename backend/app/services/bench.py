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

from app.errors import AppError
from app.services.harness import CAPABILITY_LEAVES, FAMILIES, AttemptRefused

#: P98 §5. `never_registered` and `harness_error` are plumbing: counted, never scored.
OUTCOMES = ("never_registered", "no_branch", "check_red", "check_green", "timed_out",
            "harness_error", "bounce", "sign_off")
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
    from app.services.fleet import canonical_vendor

    out = {f: _text(body, f) for f in ("vendor", "model", "suite_id", "suite_version")}
    # The same fold natural cells use (`claude` is `anthropic`), so a baseline posted by an
    # adapter name lands on the company's cell.
    out["vendor"] = canonical_vendor(out["vendor"])
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
        if t.get("kind") == "review":
            # A review row with a branch and no linked PR is withheld from `claim_review`
            # (`pr_unlinked`) and refused `pr_missing` at sign_off, so a review seat without
            # one could never be claimed. The run names the PR; the server does not invent one.
            if not isinstance(t.get("ref"), str) or not t["ref"].strip():
                raise AttemptRefused(f"review task {tid} needs a ref", status=422)
            if not isinstance(t.get("pr"), str) or not items_svc.is_pr_url(t["pr"]):
                raise AttemptRefused(
                    f"review task {tid} needs a pr: the pull request url its ref is on. "
                    "claim_review withholds a branch with no linked PR (pr_unlinked)",
                    status=422)
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
        is_review = t.get("kind") == "review"
        item = items_svc.create_item(
            db, title=str(t.get("title") or t["id"]), description=str(t.get("description") or ""),
            tags=["bench"], touchpoints=list(t.get("touchpoints") or []),
            project_id=scratch.id, status="review" if is_review else "next", commit=False)
        if is_review:
            # A review task is work someone else built: attributed to the run's planner agent,
            # branch = the task's ref, so `claim_review` hands it to the reviewer child.
            item.built_by = agent.id
            item.branch = t["ref"].strip()
            item.github_url = t["pr"].strip()
        db.flush()
        if is_review:
            _seat, code = fleet_svc.issue_enrolment(
                db, project_id=scratch.id, role="worker", minted_by=agent.id, review_only=True)
            seats.append({"task_id": t["id"], "item_id": item.id, "kind": "review",
                          "enrolment_code": code, "_seat_id": _seat.id})
            continue
        row, _withdrew, _code = delegation_svc.delegate(
            db, agent=agent, item=item, lane="backend", tier="cheap",
            note=f"bench {ident['suite_id']} {t['id']}", lease_seconds=600, seat=False)
        _seat, code = fleet_svc.issue_enrolment(
            db, project_id=scratch.id, role="worker", minted_by=agent.id,
            item_id=item.id, delegation_id=row.id, bound_only=True)
        seats.append({"task_id": t["id"], "item_id": item.id, "delegation_id": row.id,
                      "kind": "build", "enrolment_code": code, "_seat_id": _seat.id})
    run = BenchRun(
        id=f"bnr_{uuid.uuid4().hex[:12]}", source_project_id=project_id, project_id=scratch.id,
        tasks=[{"task_id": s["task_id"], "item_id": s["item_id"], "kind": s["kind"],
                "seat_id": s["_seat_id"]} for s in seats], **ident)
    db.add(run)
    db.flush()
    # The scratch project's own credential (GRPH-1869): a project-scoped key reaches only its
    # project, so the caller's source-project key cannot register children here. Plaintext is
    # returned once; it is revoked when the run's result is posted and expires in a day anyway.
    from app.security import apikey as apikey_svc
    _row, api_key = apikey_svc.generate_api_key(
        db, user_id, f"bench run {run.id}", scopes=["read", "write"], project_id=scratch.id,
        expires_in_days=1, origin="bench", commit=False)
    return {"id": run.id, "project_id": scratch.id, "source_project_id": project_id,
            **ident, "seats": [{k: v for k, v in s.items() if k != "_seat_id"} for s in seats],
            "api_key": api_key}


def review_restriction(db: Session, agent_id: str | None) -> set[str] | None:
    """What `claim_review` may hand this agent on a bench scratch project (GRPH-1883).

    None: the agent is not on a bench run's seat, so nothing changes. Otherwise the set of
    item ids it may review: its own review item for a review seat, and nothing for a build
    seat. Without this a build child that polls first takes the run's review items, its own
    build never happens, and the model is charged `no_branch` for a build it never attempted.
    """
    from app.models import Agent, BenchRun

    if not agent_id:
        return None
    agent = db.get(Agent, agent_id)
    if agent is None or not agent.enrolment_id:
        return None
    run = db.scalar(select(BenchRun).where(BenchRun.project_id == agent.project_id))
    if run is None:
        return None
    for t in run.tasks or []:
        if t.get("seat_id") == agent.enrolment_id:
            return {t["item_id"]} if t.get("kind") == "review" else set()
    return None


LEAF_VERDICTS = ("pass", "fail", "excluded")


def _is_green(o: dict) -> bool:
    """A build outcome is green on `check_green`; a review outcome when its verdict matched
    `expect` (bounce for a defect, sign_off for clean work)."""
    if o.get("kind") == "review":
        return o.get("expect") in EXPECTS and o["outcome"] == o["expect"]
    return o["outcome"] == "check_green"


def _rollups(outcomes: list[dict]) -> dict:
    """Per capability: scored `n`, `green`, and the plumbing excluded. Server-computed.

    A task that posted per-leaf `leaves` is scored on each leaf's own verdict (GRPH-1877); a
    leaf it claims but has no verdict for, or whose verdict is `excluded`, leaves that leaf's
    n with the reason. A task that posted none (an older client) is scored on its check.
    """
    out: dict[str, dict] = {}
    for o in outcomes:
        leaves = o.get("leaves")
        for cap in o["capabilities"] or ["uncategorised"]:
            cell = out.setdefault(cap, {"n": 0, "green": 0, "excluded": 0})
            if o["outcome"] in PLUMBING:
                cell["excluded"] += 1
            elif leaves is None:
                cell["n"] += 1
                cell["green"] += _is_green(o)
            else:
                v = leaves.get(cap)
                if v is None or v["verdict"] == "excluded":
                    cell["excluded"] += 1
                    reason = (v or {}).get("reason") or "no verdict reported for this leaf"
                    cell.setdefault("excluded_reasons", []).append(
                        {"task_id": o["task_id"], "reason": reason})
                else:
                    cell["n"] += 1
                    cell["green"] += v["verdict"] == "pass"
    return out


def _leaves(task_id: str, raw) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise AttemptRefused(f"leaves for {task_id} must be an object", status=422)
    out: dict[str, dict] = {}
    for leaf, v in raw.items():
        if leaf not in CAPABILITY_LEAVES and leaf not in FAMILIES:
            raise AttemptRefused(f"unknown leaf {leaf!r} in leaves for {task_id}", status=422)
        if isinstance(v, str):
            v = {"verdict": v}
        if not isinstance(v, dict) or v.get("verdict") not in LEAF_VERDICTS:
            raise AttemptRefused(
                f"leaf {leaf} for {task_id} needs a verdict of {list(LEAF_VERDICTS)}", status=422)
        reason = v.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise AttemptRefused(f"leaf {leaf} reason must be a string", status=422)
        out[leaf] = {"verdict": v["verdict"], "reason": (reason or "")[:300]}
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
        entry = {"task_id": o["task_id"], "outcome": o["outcome"], "capabilities": caps,
                 "tokens": tokens, "wall_seconds": wall}
        leaves = _leaves(o["task_id"], o.get("leaves"))
        if leaves is not None:
            entry["leaves"] = leaves
        if o.get("kind") == "review":
            if o.get("expect") not in EXPECTS:
                raise AttemptRefused(
                    f"expect for review task {o['task_id']} must be one of {list(EXPECTS)}",
                    status=422)
            entry["kind"] = "review"
            entry["expect"] = o["expect"]
        outcomes.append(entry)
    run_id = body.get("run_id")
    if run_id is not None:
        run = db.get(BenchRun, run_id)
        if run is None or run.source_project_id != project_id:
            raise AttemptRefused("run not found", status=404)
    if run_id is not None:
        from app.models import ApiKey
        for k in db.scalars(select(ApiKey).where(
                ApiKey.project_id == run.project_id, ApiKey.revoked.is_(False))).all():
            k.revoked = True
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


def attach_baselines(db: Session, project_id: str, cells: list[dict], *,
                     versions: str = "all") -> None:
    """Give every measured grid cell a `baseline` block, or None when no run posted one.

    Matches on vendor, model, binary_version and capability, newest posted record first; the
    block names the binary_version it was measured at. It is its own object: it is never added
    to a cell's natural or probe counts (P98 D11).

    A family rollup gets no `baseline` key at all. Baseline rollups are keyed by function, so a
    family has no number of its own, and None there would read as "no run" — a false "no
    baseline". Its leaves carry theirs; read those.

    A baseline with no natural cell gets a row of its own. It honours `versions` the way the
    natural cells do: under `current`, a baseline at a version older than the newest natural
    one is dropped unless natural cells at that version are also being shown, and a kept row is
    current only when no natural version of its vendor+model is newer.
    """
    from app.models import BenchBaseline
    from app.services.fleet import canonical_vendor
    from app.services.harness import FLOOR, SAMPLED, _cost, _version_key, family_of

    rows = db.scalars(select(BenchBaseline).where(
        BenchBaseline.source_project_id == project_id)
        .order_by(BenchBaseline.posted_at.desc())).all()
    latest: dict[tuple, dict] = {}
    for r in rows:
        for cap, roll in (r.rollups or {}).items():
            latest.setdefault((canonical_vendor(r.vendor), r.model, r.binary_version, cap), {
                "suite_id": r.suite_id, "suite_version": r.suite_version,
                "binary_version": r.binary_version, "n": roll["n"], "green": roll["green"],
                "rate": round(roll["green"] / roll["n"], 3) if roll["n"] else None,
                "below_floor": roll["n"] < FLOOR,
                "excluded": roll.get("excluded", 0),
                "excluded_reasons": (roll.get("excluded_reasons") or [])[:3]})

    def block(cell: dict) -> dict | None:
        k = cell["key"]
        found = latest.get((k["vendor"], k["model"], k["binary_version"], k["capability"]))
        return dict(found) if found else None

    natural: set[tuple] = set()
    shown: dict[tuple, set[str]] = {}  # (vendor, model) -> natural versions in the payload
    newest: dict[tuple, str] = {}  # (vendor, model) -> the current natural version
    for cell in cells:
        if cell.get("kind") == "family":
            cell.pop("baseline", None)
        else:
            cell["baseline"] = block(cell)
        for c in [cell, *(cell.get("leaves") or [])]:
            if "key" in c:
                k = c["key"]
                natural.add((k["vendor"], k["model"], k["binary_version"], k["capability"]))
                shown.setdefault((k["vendor"], k["model"]), set()).add(k["binary_version"])
                if c.get("is_current_version"):
                    newest[(k["vendor"], k["model"])] = k["binary_version"]
        for leaf in cell.get("leaves") or []:
            leaf["baseline"] = block(leaf)

    # A baseline covers a cell natural traffic may never have made: the cold start it exists
    # for. Those get a row of their own, layer `baseline`, natural n 0.
    for key, found in sorted(latest.items()):
        if key in natural:
            continue
        vendor, model, version, cap = key
        current = newest.get((vendor, model))
        is_current = current is None or _version_key(version) >= _version_key(current)
        if (versions == "current" and not is_current
                and version not in shown.get((vendor, model), set())):
            continue
        seen = sorted(shown.get((vendor, model), set()) | {version}, key=_version_key)
        cells.append({
            "key": {"vendor": vendor, "model": model, "binary_version": version,
                    "capability": cap, "size_band": ""},
            "kind": "leaf", "family": family_of(cap), "label": cap,
            "layer": "baseline", "finished": 0, "signed_off": 0, "bounced": 0, "rate": None,
            "below_floor": True, "sampling": {r: 0 for r in SAMPLED}, "skew": None,
            "median_seconds": None, "cost": _cost(0, 0, 0, 0, 0), "series": [], "versions_seen": seen,
            "is_current_version": is_current,
            "by_project": [], "baseline": dict(found)})


# --- the repo suite (GRPH-P98 §3, GRPH-1857) -------------------------------------------------

KINDS = ("build", "review")
EXPECTS = ("bounce", "sign_off")
_STR_FIELDS = {"title": 256, "description": None, "size_band": 32, "reference": None,
               "expect": 16, "ref": 128}


class SuiteConflict(AppError):
    """`expected_version` did not match. Carries the current version for the 409 body.

    Not an `errors.Conflict`: a stale read is a retryable state conflict, not a policy refusal
    (the same call as `BlobPending`), so it stays out of the refusal ratchet. REST returns 409
    with `current_version`; MCP returns code `stale_version`. The caller re-reads and retries.
    """

    code = "stale_version"

    def __init__(self, current_version: int) -> None:
        super().__init__(f"expected_version is stale; the suite is at version {current_version}",
                         hint="read the suite again and retry with its version")
        self.current_version = current_version


def _task_view(r) -> dict:
    return {"id": r.task_id, "kind": r.kind, "base_sha": r.base_sha, "title": r.title,
            "description": r.description, "acceptance": r.acceptance or [],
            "touchpoints": r.touchpoints or [], "capabilities": r.capabilities or [],
            "size_band": r.size_band, "check": r.check, "reference": r.reference,
            "expect": r.expect, "ref": r.ref}


def get_suite(db: Session, project_id: str) -> dict:
    from app.models import BenchSuite, BenchSuiteTask

    row = db.get(BenchSuite, project_id)
    tasks = db.scalars(select(BenchSuiteTask).where(BenchSuiteTask.project_id == project_id)
                       .order_by(BenchSuiteTask.adopted_at, BenchSuiteTask.task_id)).all()
    return {"version": row.version if row else 0, "tasks": [_task_view(t) for t in tasks]}


def _expected(body: dict) -> int:
    v = body.get("expected_version")
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise AttemptRefused("expected_version is required: a non-negative integer", status=422)
    return v


def _strs(task: dict, field: str) -> list[str]:
    v = task.get(field) or []
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise AttemptRefused(f"{field} must be a list of strings", status=422)
    return v


def _validate_task(t) -> dict:
    if not isinstance(t, dict):
        raise AttemptRefused("every task must be an object", status=422)
    tid = t.get("id")
    if not isinstance(tid, str) or not tid.strip() or len(tid) > 128:
        raise AttemptRefused("every task needs an id", status=422)
    where = f"task {tid}: "
    kind = t.get("kind", "build")
    if kind not in KINDS:
        raise AttemptRefused(f"{where}kind must be one of {list(KINDS)}", status=422)
    base_sha = t.get("base_sha")
    if not isinstance(base_sha, str) or not base_sha.strip():
        raise AttemptRefused(f"{where}base_sha is required", status=422)
    if len(base_sha) > 64:
        raise AttemptRefused(f"{where}base_sha must be at most 64 characters", status=422)
    caps = _strs(t, "capabilities")
    for c in caps:
        if c not in CAPABILITY_LEAVES:
            raise AttemptRefused(f"{where}capabilities: unknown capability {c!r}", status=422)
    out: dict = {"task_id": tid, "kind": kind, "base_sha": base_sha.strip(),
                 "acceptance": _strs(t, "acceptance"), "touchpoints": _strs(t, "touchpoints"),
                 "capabilities": caps}
    for f, limit in _STR_FIELDS.items():
        v = t.get(f) or ""
        if not isinstance(v, str):
            raise AttemptRefused(f"{where}{f} must be a string", status=422)
        if limit is not None and len(v) > limit:
            raise AttemptRefused(f"{where}{f} must be at most {limit} characters", status=422)
        out[f] = v
    if kind == "review" and out["expect"] not in EXPECTS:
        raise AttemptRefused(f"{where}expect must be one of {list(EXPECTS)}", status=422)
    check = t.get("check")
    if check is not None:
        if (not isinstance(check, dict) or not isinstance(check.get("command"), str)
                or not check["command"].strip()):
            raise AttemptRefused(f"{where}check must be an object with a command", status=422)
        check = {"command": check["command"], "tests": _strs(check, "tests")}
    if kind == "build" and check is None:
        raise AttemptRefused(f"{where}check is required for a build task", status=422)
    out["check"] = check
    return out


def _bump(db: Session, project_id: str, expected: int) -> int:
    """Compare-and-set the version. A mismatch raises `SuiteConflict` with the current one."""
    from sqlalchemy import update

    from app.models import BenchSuite

    if db.get(BenchSuite, project_id) is None:
        db.add(BenchSuite(project_id=project_id, version=0))
        db.flush()
    res = db.execute(
        update(BenchSuite).where(BenchSuite.project_id == project_id,
                                 BenchSuite.version == expected)
        .values(version=expected + 1, updated_at=datetime.now(timezone.utc)))
    if res.rowcount != 1:
        db.expire_all()
        raise SuiteConflict(db.get(BenchSuite, project_id).version)
    db.expire_all()
    return expected + 1


def adopt_tasks(db: Session, *, project_id: str, body: dict) -> dict:
    """Adopt one or more tasks (an id already in the suite is replaced). Bumps version by one."""
    from app.models import BenchSuiteTask

    expected = _expected(body)
    raw = body.get("tasks")
    if not isinstance(raw, list) or not (1 <= len(raw) <= MAX_TASKS):
        raise AttemptRefused(f"tasks must be a list of 1 to {MAX_TASKS}", status=422)
    tasks = [_validate_task(t) for t in raw]
    ids = [t["task_id"] for t in tasks]
    if len(set(ids)) != len(ids):
        raise AttemptRefused("a task id appears twice", status=422)
    _bump(db, project_id, expected)
    for t in tasks:
        existing = db.scalar(select(BenchSuiteTask).where(
            BenchSuiteTask.project_id == project_id, BenchSuiteTask.task_id == t["task_id"]))
        if existing is not None:
            db.delete(existing)
    db.flush()
    for t in tasks:
        db.add(BenchSuiteTask(id=f"bst_{uuid.uuid4().hex[:12]}", project_id=project_id, **t))
    db.flush()
    return get_suite(db, project_id)


def remove_tasks(db: Session, *, project_id: str, body: dict) -> dict:
    """Remove tasks by id. Bumps version by one; an id not in the suite is refused."""
    from app.models import BenchSuiteTask

    expected = _expected(body)
    ids = body.get("task_ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
        raise AttemptRefused("task_ids must be a non-empty list of task ids", status=422)
    rows = db.scalars(select(BenchSuiteTask).where(
        BenchSuiteTask.project_id == project_id, BenchSuiteTask.task_id.in_(ids))).all()
    missing = sorted(set(ids) - {r.task_id for r in rows})
    if missing:
        raise AttemptRefused(f"task {missing[0]!r} is not in the suite", status=404)
    _bump(db, project_id, expected)
    for r in rows:
        db.delete(r)
    db.flush()
    return get_suite(db, project_id)
