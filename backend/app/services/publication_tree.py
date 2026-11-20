"""The publication tree (GRPH-1763 / GRPH-P90 "The tree").

A render is the deterministic file tree of the earned rows this deployment can
read. Two calls with the same rows return the same bytes. This module does not
fetch, commit, or push — that is The push — and it does not copy a previous
tree. A failure returns no files, so nothing is written and a remote is untouched.

Rule-pull states, and only these:

- ``pulled`` — a published version. ``rules/decisions.json`` is written.
- ``not_published`` — the pull returned no version. No decisions file. The
  manifest says ``rules: not_published``. That is a record, not an empty list.
- ``not_pulled`` — this box has not pulled. No decisions file. The manifest
  says ``rules: not_pulled`` when something else earned a file. Alone, with
  nothing else earned, the result is ``nothing_earned`` and no manifest: an
  empty manifest is not a published "no policy."
- ``unknown`` — the pull failed. Nothing is rendered. The reason is named.
  Any other state is this one too: a blank must not read as ``not_pulled``.

The version on a pulled record is copied and compared for equality only. It is
not recomputed from these bytes — the ledger hashes a different shape.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hashing import content_hash

RENDERED = "rendered"
NOTHING_EARNED = "nothing_earned"
RULES_UNKNOWN = "rules_unknown"
FAILED = "failed"

RULES_PULLED = "pulled"
RULES_NOT_PUBLISHED = "not_published"
RULES_NOT_PULLED = "not_pulled"
RULES_UNKNOWN_STATE = "unknown"

_FILE_STATES = (RULES_PULLED, RULES_NOT_PUBLISHED, RULES_NOT_PULLED)
GRILL_HEADING = "Decisions from grilling"
#: The why-file body when that section is not in the baseline. A fixed sentence,
#: so two renders agree, and so the file cannot be read as an empty decision list.
ABSENT_WHY = "The Decisions from grilling section is absent.\n"

_HASH_FIELD = {
    "lesson": "content_hash",
    "spec": "baseline_hash",
    "why": "content_hash",
    "preamble": "content_hash",
    "constraints": "accepted_hash",
}


@dataclass(frozen=True)
class RulesPull:
    """What this box knows about the org rule pull.

    ``toggles`` entries are ``{rule_id, on, params, locked}``. ``profile`` is the
    applied profile (``{id, version}``) or None. ``changed_at`` is already an
    ISO-8601 UTC string; the render does not format clocks.
    """

    state: str
    reason: str = ""
    version: str = ""
    changed_at: str = ""
    toggles: tuple = ()
    profile: dict | None = None


@dataclass(frozen=True)
class Lesson:
    shard_id: str
    reach: str
    text: str
    content_hash: str
    eligibility_state: str | None = None
    caught_state: str = "unknown"
    effectiveness_score: float | None = None
    source: str = ""
    item_id: str | None = None
    status: str = "published"


@dataclass(frozen=True)
class Spec:
    project_id: str
    prd_id: str
    status: str
    local_only: bool = False
    has_baseline: bool = False
    version: str = ""
    baseline_body: str = ""
    baseline_hash: str = ""
    draft_body: str = ""


@dataclass(frozen=True)
class Preamble:
    project_id: str
    version: int
    body: str
    content_hash: str
    local_only: bool = False


@dataclass(frozen=True)
class Constraint:
    project_id: str
    set_id: str
    body: str
    accepted_hash: str
    accepted: bool = False
    local_only: bool = False


@dataclass(frozen=True)
class Earned:
    rules: RulesPull
    lessons: tuple[Lesson, ...] = ()
    specs: tuple[Spec, ...] = ()
    preambles: tuple[Preamble, ...] = ()
    constraints: tuple[Constraint, ...] = ()
    #: Rows this deployment can see but cannot read faithfully. Rendering them as
    #: absent would publish a hole. Each entry is a short reason.
    unreadable: tuple[str, ...] = ()


@dataclass(frozen=True)
class RenderResult:
    result: str
    reason: str
    files: dict[str, str]
    manifest_hash: str | None


@dataclass(frozen=True)
class _Art:
    kind: str
    id: str
    path: str | None
    content_hash: str | None
    version: object
    grill_decisions: str | None = None
    fields: tuple = ()
    body: str = ""


def render(earned: Earned, *, previous: Mapping[str, str] | None = None) -> RenderResult:
    """Render ``earned`` to file text.

    ``previous`` is the last tree a caller still holds. It is deliberately not
    read: a failed pull must not copy the last ``rules/decisions.json`` forward.
    """
    del previous
    if earned.rules.state not in _FILE_STATES:
        reason = (earned.rules.reason or "").strip() or "unspecified"
        return RenderResult(RULES_UNKNOWN, reason, {}, None)
    if earned.unreadable:
        return RenderResult(FAILED, earned.unreadable[0], {}, None)

    planned = _plan(earned)
    if not planned:
        return RenderResult(NOTHING_EARNED, "", {}, None)
    files = _write(planned)
    problem = _audit(earned, planned, files)
    if problem:
        return RenderResult(FAILED, problem, {}, None)
    manifest = _canonical(_manifest_doc(earned, planned, files))
    files = {**files, "manifest.json": manifest}
    return RenderResult(RENDERED, "", files, content_hash(manifest))


def render_deployment(
    db: Session,
    *,
    rules: RulesPull | None = None,
    previous: Mapping[str, str] | None = None,
) -> RenderResult:
    """Load the rows this deployment can read, then render them.

    ``rules`` overrides the pull outcome. The ledger has no column for a pull
    that failed or for a pull that returned no published version: a missing
    ``rule_decision_pull`` row is ``not_pulled``, which is a different fact.
    The push passes ``unknown`` or ``not_published`` when it knows which.
    """
    return render(load_earned(db, rules=rules), previous=previous)


def load_earned(db: Session, *, rules: RulesPull | None = None) -> Earned:
    """The earned rows this deployment can read.

    A project with no ``sync_project`` row is local-only (that is the ledger's
    own word for it) and contributes no ``projects/<id>`` directory. Org rules
    and org-reach lessons do not go through that gate.
    """
    from app.models import DesignSet, MemoryShard, Prd, SyncProject
    from app.services import memory as memory_svc
    from app.services import prds as prd_svc
    from app.services import specs as spec_svc

    publishing = set(db.scalars(select(SyncProject.local_project_id)).all())
    unreadable: list[str] = []

    lessons = []
    shards = db.scalars(
        select(MemoryShard).where(MemoryShard.reach == "org", MemoryShard.retired_at.is_(None))
        .order_by(MemoryShard.id)
    ).all()
    for shard in shards:
        if shard.status != "published":
            continue
        outcomes = _outcomes(db, shard.id)
        eff = memory_svc.lesson_effectiveness(
            shard, outcomes, origin_path=memory_svc.origin_path_state(db, shard))
        elig = memory_svc.org_eligibility(shard, [shard], scan="scanned")
        lessons.append(Lesson(
            shard_id=shard.id,
            reach=shard.reach,
            text=shard.text or "",
            content_hash=content_hash(shard.text or ""),
            eligibility_state=elig.get("state"),
            caught_state=eff.get("caught_state") or "unknown",
            effectiveness_score=eff.get("score"),
            source=shard.source or "",
            item_id=shard.item_id,
            status=shard.status,
        ))

    specs = []
    for prd in db.scalars(select(Prd).order_by(Prd.id)).all():
        local_only = prd.project_id not in publishing
        base = prd_svc.baseline_of(db, prd.id)
        specs.append(Spec(
            project_id=prd.project_id,
            prd_id=prd.id,
            status=prd.status,
            local_only=local_only,
            has_baseline=base is not None,
            version=base.version if base is not None else "",
            baseline_body=base.body if base is not None else "",
            baseline_hash=content_hash(base.body) if base is not None else "",
            draft_body=prd.body or "",
        ))

    preambles = []
    seen_projects = sorted({s.project_id for s in specs} | publishing)
    for project_id in seen_projects:
        # A local-only project contributes no preamble. Its document is not
        # read: an unreadable body there is not a row this tree would publish.
        if project_id not in publishing:
            continue
        view = spec_svc.preamble_view(db, project_id)
        if view is None or view.get("body_state") == "absent":
            continue
        if view.get("body_state") != "stored":
            # Pending or unreadable is not "no preamble". Publishing past it
            # would omit a row this deployment can see.
            unreadable.append(f"preamble:{project_id}:{view.get('body_state')}")
            continue
        preambles.append(Preamble(
            project_id=project_id,
            version=int(view["version"]),
            body=view["body"] or "",
            content_hash=view["content_hash"],
            local_only=False,
        ))

    constraints = []
    sets = db.scalars(select(DesignSet).order_by(DesignSet.id)).all()
    for ds in sets:
        local_only = ds.project_id not in publishing
        constraints.append(Constraint(
            project_id=ds.project_id,
            set_id=ds.id,
            body=ds.constraints_body or "",
            accepted_hash=ds.constraints_hash or "",
            accepted=bool(ds.constraints_accepted),
            local_only=local_only,
        ))

    return Earned(
        rules=rules if rules is not None else _rules_from_ledger(db),
        lessons=tuple(lessons),
        specs=tuple(specs),
        preambles=tuple(preambles),
        constraints=tuple(constraints),
        unreadable=tuple(unreadable),
    )


def _outcomes(db: Session, shard_id: str):
    from app.models import LessonOutcome

    return list(db.scalars(
        select(LessonOutcome).where(LessonOutcome.shard_id == shard_id)
        .order_by(LessonOutcome.id)
    ).all())


def _rules_from_ledger(db: Session) -> RulesPull:
    """``not_pulled`` when this box has no applied version. A failed pull and a
    pull that returned no version are not stored here; the caller passes those."""
    from app.models import OrgRuleVersion, RuleDecisionPull, RuleToggle

    pull = db.get(RuleDecisionPull, "instance")
    if pull is None or not pull.org_id or not pull.applied_version:
        return RulesPull(state=RULES_NOT_PULLED)
    toggles = tuple(
        {"rule_id": row.rule_id, "on": row.on, "params": row.params, "locked": bool(row.locked)}
        for row in db.scalars(
            select(RuleToggle).where(RuleToggle.org_id == pull.org_id).order_by(RuleToggle.rule_id)
        ).all()
    )
    profile = None
    if pull.profile:
        profile = {"id": pull.profile, "version": pull.profile_version or ""}
    stamp = db.get(OrgRuleVersion, pull.org_id)
    changed = stamp.changed_at if stamp is not None else pull.applied_at
    return RulesPull(
        state=RULES_PULLED,
        version=pull.applied_version,
        changed_at=_iso(changed),
        toggles=toggles,
        profile=profile,
    )


def _iso(value) -> str:
    if value is None:
        return ""
    from datetime import timezone

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _plan(earned: Earned) -> list[_Art]:
    planned: list[_Art] = []
    if earned.rules.state == RULES_PULLED:
        planned.append(_Art(
            kind="rules", id="decisions", path="rules/decisions.json",
            content_hash=None, version=earned.rules.version, body=_rules_document(earned.rules),
        ))
    for lesson in sorted(
        (ls for ls in earned.lessons if ls.reach == "org" and ls.status == "published"),
        key=lambda ls: ls.shard_id,
    ):
        planned.append(_Art(
            kind="lesson", id=lesson.shard_id, path=f"lessons/{lesson.shard_id}.md",
            content_hash=lesson.content_hash, version=None,
            fields=tuple(_lesson_fields(lesson)), body=lesson.text,
        ))
    publishable = [
        sp for sp in earned.specs
        if sp.status == "approved" and sp.has_baseline and not sp.local_only
    ]
    for sp in sorted(publishable, key=lambda s: (s.project_id, s.prd_id)):
        planned.append(_Art(
            kind="spec", id=sp.prd_id,
            path=f"projects/{sp.project_id}/specs/{sp.prd_id}.md",
            content_hash=sp.baseline_hash, version=sp.version,
            fields=(
                ("baseline_hash", sp.baseline_hash),
                ("version", sp.version),
                ("status", sp.status),
            ),
            body=sp.baseline_body,
        ))
        section, grill = _grill(sp.baseline_body)
        planned.append(_Art(
            kind="why", id=sp.prd_id,
            path=f"projects/{sp.project_id}/why/{sp.prd_id}.md",
            content_hash=content_hash(section), version=sp.version,
            grill_decisions=grill,
            fields=(
                ("baseline_hash", sp.baseline_hash),
                ("version", sp.version),
                ("grill_decisions", grill),
                ("content_hash", content_hash(section)),
            ),
            body=section,
        ))
    for pre in sorted(
        (p for p in earned.preambles if not p.local_only),
        key=lambda p: p.project_id,
    ):
        planned.append(_Art(
            kind="preamble", id=pre.project_id,
            path=f"projects/{pre.project_id}/preamble.md",
            content_hash=pre.content_hash, version=pre.version,
            fields=(("version", pre.version), ("content_hash", pre.content_hash)),
            body=pre.body,
        ))
    for cs in sorted(
        (c for c in earned.constraints if c.accepted and not c.local_only),
        key=lambda c: (c.project_id, c.set_id),
    ):
        planned.append(_Art(
            kind="constraints", id=cs.set_id,
            path=f"projects/{cs.project_id}/constraints/{cs.set_id}.md",
            content_hash=cs.accepted_hash, version=None,
            fields=(("accepted_hash", cs.accepted_hash),),
            body=cs.body,
        ))
    if earned.rules.state == RULES_NOT_PUBLISHED or (
        earned.rules.state == RULES_NOT_PULLED and planned
    ):
        planned.append(_Art(
            kind="rules", id="rules", path=None, content_hash=None,
            version=None, grill_decisions=earned.rules.state,
        ))
    return planned


def _lesson_fields(lesson: Lesson) -> list[tuple[str, object]]:
    fields: list[tuple[str, object]] = [("reach", lesson.reach)]
    # A blank state is omitted on purpose. The audit then fails the render
    # instead of publishing a lesson whose eligibility nobody recorded.
    if isinstance(lesson.eligibility_state, str) and lesson.eligibility_state.strip():
        fields.append(("eligibility.state", lesson.eligibility_state))
    fields.extend((
        ("caught_state", lesson.caught_state),
        ("effectiveness.score", lesson.effectiveness_score),
        ("source", lesson.source),
        ("item_id", lesson.item_id),
        ("content_hash", lesson.content_hash),
    ))
    return fields


def _grill(baseline: str) -> tuple[str, str]:
    from app.services.prds import section_bodies

    bodies = section_bodies(baseline or "")
    if GRILL_HEADING not in bodies:
        return ABSENT_WHY, "absent"
    return bodies[GRILL_HEADING], "present"


def _rules_document(rules: RulesPull) -> str:
    rows = sorted(rules.toggles, key=lambda t: str(t.get("rule_id") or ""))
    return _canonical({
        "changed_at": rules.changed_at,
        "locks": [{"locked": bool(t.get("locked")), "rule_id": t.get("rule_id")} for t in rows],
        "params": [{"params": t.get("params"), "rule_id": t.get("rule_id")} for t in rows],
        "profile": rules.profile,
        "toggles": [{"on": t.get("on"), "rule_id": t.get("rule_id")} for t in rows],
        "version": rules.version,
    })


def _write(planned: list[_Art]) -> dict[str, str]:
    files: dict[str, str] = {}
    for art in planned:
        if not art.path:
            continue
        if art.kind == "rules":
            files[art.path] = art.body
        else:
            files[art.path] = _markdown(art.fields, art.body)
    return files


def _markdown(fields: tuple, body: str) -> str:
    head = "---\n" + "".join(
        f"{key}: {json.dumps(value, ensure_ascii=False)}\n" for key, value in fields
    ) + "---\n"
    if body.endswith("\n"):
        return head + body
    return head + body + "\n"


def _parse(text: str) -> tuple[dict | None, str]:
    if not text.startswith("---\n"):
        return None, text
    rest = text[4:]
    idx = rest.find("\n---\n")
    if idx < 0:
        return None, text
    parsed: dict = {}
    for line in rest[:idx].splitlines():
        if not line:
            continue
        key, sep, raw = line.partition(": ")
        if not sep:
            return None, text
        try:
            parsed[key] = json.loads(raw)
        except json.JSONDecodeError:
            return None, text
    return parsed, rest[idx + 5:]


def _audit(earned: Earned, planned: list[_Art], files: dict[str, str]) -> str | None:
    """Fail closed. A tree that omits a row, drops eligibility, or keeps a hash
    the body no longer matches is not returned at all."""
    want = {art.path for art in planned if art.path}
    if set(files) != want:
        return "omitted_row"
    if earned.rules.state != RULES_PULLED and "rules/decisions.json" in files:
        return "omitted_row"
    for art in planned:
        if not art.path:
            continue
        text = files[art.path]
        if art.kind == "rules":
            try:
                body = json.loads(text)
            except json.JSONDecodeError:
                return "hash_mismatch"
            for key in ("toggles", "params", "locks", "profile", "version", "changed_at"):
                if key not in body:
                    return "omitted_row"
            # Equality only. A greater version is not a newer one.
            if body.get("version") != earned.rules.version:
                return "hash_mismatch"
            continue
        fm, body = _parse(text)
        if fm is None:
            return "hash_mismatch"
        if art.kind == "lesson":
            state = fm.get("eligibility.state")
            if not isinstance(state, str) or not state.strip():
                return "eligibility_missing"
        field = _HASH_FIELD[art.kind]
        # The stamped hash is the ledger's. Recomputing it from a changed body
        # and writing the new digest would publish the change under a fresh
        # name; the render fails instead.
        if fm.get(field) != art.content_hash or content_hash(body) != art.content_hash:
            return "hash_mismatch"
        if art.kind == "why" and fm.get("grill_decisions") != art.grill_decisions:
            return "omitted_row"
    doc = _manifest_doc(earned, planned, files)
    got = {(row["kind"], row["id"]) for row in doc["artifacts"]}
    expected = {(art.kind, art.id) for art in planned if art.path}
    if got != expected:
        return "omitted_row"
    if earned.rules.state in (RULES_NOT_PUBLISHED, RULES_NOT_PULLED):
        if doc.get("rules") != earned.rules.state:
            return "omitted_row"
    elif "rules" in doc:
        return "omitted_row"
    return None


def _manifest_doc(earned: Earned, planned: list[_Art], files: dict[str, str]) -> dict:
    artifacts = []
    for art in planned:
        if not art.path:
            continue
        digest = art.content_hash
        if art.kind == "rules":
            digest = content_hash(files[art.path])
        row = {
            "kind": art.kind,
            "id": art.id,
            "content_hash": digest,
            "version": art.version,
            "path": art.path,
        }
        if art.kind == "why":
            row["grill_decisions"] = art.grill_decisions
        artifacts.append(row)
    artifacts.sort(key=lambda row: row["path"])
    doc: dict = {"artifacts": artifacts}
    if earned.rules.state in (RULES_NOT_PUBLISHED, RULES_NOT_PULLED):
        doc["rules"] = earned.rules.state
    return doc


def _canonical(doc: dict) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
