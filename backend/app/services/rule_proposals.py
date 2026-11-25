"""Rule proposals: a cited, direction-checked suggestion a project can adopt (GRPH-1775 / P91 D2, D3).

A proposal is a *suggestion*; nothing here changes a rule (`rules.person_adopts`). No MCP tool
adopts, dismisses or promotes one. What this module owns is what a proposal must be to exist:

- **Cited.** At least one citation, each one resolving: an `item` or `observation` or `lesson`
  of this project, a `file` (path + line range + commit) that exists at that commit, or a `url`.
  A citation that cannot be checked is a refusal, never a pass.
- **Valid.** A `new_rule` draft passes `rule_pack.validate_draft`.
- **Direction computed, not claimed.** `rule_pack.direction` against the rule's resolved value.
  An agent may only tighten; a person or an import run may propose a loosening, flagged `loosens`.
- **Never off.** Turning a rule off or removing it is refused for every proposer, as is any
  invariant of `docs/rule-candidates.md` "The line", and a param change with no comparator.
- **Folded.** The same primitive and params as an open proposal appends citations to it.
"""
from __future__ import annotations

import copy
import re
from datetime import datetime, timezone
from collections.abc import Callable
from typing import NoReturn

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.errors import NotFound, Refused, Validation
from app.models import (CustomRule, Item, MemoryShard, Observation, PrimitiveGapCount, Project,
                        RuleGuidance, RuleProposal, utcnow)
from app.services import custom_rules
from app.services import events as events_svc
from app.services import rule_pack
from app.services import rule_toggles as T

#: The registry rule every refusal of a malformed or uncited proposal names.
CITED_RULE = "rules.proposal_cited"

KINDS = ("new_rule", "turn_on", "tighten")
OFF_KINDS = ("turn_off", "off", "remove", "delete", "disable")
CITATION_KINDS = ("file", "item", "observation", "lesson", "url")
PROPOSER_KINDS = ("agent", "person", "import")
STATUSES = ("open", "adopted", "dismissed", "superseded")

#: Rules whose opposite setting would make a receipt say something false (`docs/rule-candidates.md`
#: "The line", "Stays hard-coded"). They are not library rules and cannot be proposed against.
INVARIANTS = {
    "rules.person_adopts": "a person adopts a rule",
    "gate.approval_earned_by_grill": "approval is earned by the grill",
    "review.not_your_own_work": "review is a second opinion",
    "review.attest_the_head": "a receipt must name the code that was reviewed",
    "review.uncompared_is_not_a_pass": "a check that could not run is not a pass",  # nosec B105
    "review.acceptance_covered": "every acceptance clause is tested",
    "merge.covering": "unreviewed files do not merge under a sign-off",
    "review.sabotage_floor": "the sabotage floor at effort 3",
    "fleet.quarantine": "quarantine after repeated refusals",
}

#: rule id -> primitive, for rules registered without a pack template (the built-in constants).
_PRIMITIVE_OF = {
    T.EXCLUSIVE_AREAS_RULE: "exclusive_areas", T.GENERATED_FILES_RULE: "generated_files",
    T.DONE_PREDICATES_RULE: "done_predicates", T.PATH_CLASSES_RULE: "path_classes",
    T.TEST_SCOPE_RULE: "test_scope", T.FOUND_BUG_RULE: "found_bug",
    T.UNDECLARED_FILES_RULE: "undeclared_files", T.MEMORY_WRITE_MODE_RULE: "memory_write_mode",
}

_FILE_TIMEOUT = 5.0
_REPO = re.compile(r"^github\.com/([^/]+/[^/]+)$")

#: `(repo_url, path, commit) -> bool | None` — True present, False absent, None unverifiable.
FileReader = Callable[[str, str, str], "bool | None"]


def _github_file_exists(repo_url: str, path: str, commit: str) -> bool | None:
    m = _REPO.match(repo_url or "")
    if not m:
        return None
    headers = {"Accept": "application/vnd.github+json"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    try:
        r = httpx.get(f"https://api.github.com/repos/{m.group(1)}/contents/{path}",
                      params={"ref": commit}, headers=headers, timeout=_FILE_TIMEOUT)
    except httpx.HTTPError:
        return None
    if r.status_code == 200:
        return True
    return False if r.status_code == 404 else None


#: The project's repo read path. Tests replace it; a forge it does not know reads as unverifiable.
FILE_READER: FileReader = _github_file_exists


# ---- citations ---------------------------------------------------------------------------

def _refuse(detail: str, hint: str | None = None) -> NoReturn:
    """A proposal that is not cited, checkable and well-formed does not exist (`CITED_RULE`)."""
    raise Refused(422, detail, rule_id=CITED_RULE, hint=hint)


def _resolve_citation(db: Session, project: Project, c: object) -> dict:
    if not isinstance(c, dict) or c.get("kind") not in CITATION_KINDS:
        _refuse(f"a citation must be an object with kind one of {CITATION_KINDS}")
    kind, ref = c["kind"], c.get("ref")
    if kind == "file":
        f = ref if isinstance(ref, dict) else {}
        path, commit, lines = f.get("path"), f.get("commit"), f.get("lines")
        if not (isinstance(path, str) and path and isinstance(commit, str) and commit):
            _refuse("a `file` citation's ref needs `path` and `commit` (and `lines`)")
        if path.startswith("/") or ".." in path.split("/"):
            _refuse(f"file citation path {path!r} must be repo-relative")
        got = FILE_READER(project.repo_url or "", path, commit)
        if got is None:
            _refuse(f"could not verify {path!r} at {commit[:12]}: the project's forge was "
                          "not readable", hint="set the project's repo_url and retry")
        if not got:
            _refuse(f"{path!r} does not exist at {commit[:12]}")
        return {"kind": "file", "ref": {"path": path, "commit": commit,
                                        "lines": str(lines) if lines else ""}}
    if not isinstance(ref, (str, int)) or isinstance(ref, bool) or not str(ref).strip():
        _refuse(f"a `{kind}` citation needs a `ref`")
    ref = str(ref).strip()
    if kind == "item":
        it = db.get(Item, ref)
        if it is None or it.project_id != project.id:
            _refuse(f"cited item {ref!r} does not exist in this project")
    elif kind == "observation":
        o = db.get(Observation, int(ref)) if ref.isdigit() else None
        if o is None or o.project_id != project.id:
            _refuse(f"cited observation {ref!r} does not exist in this project")
    elif kind == "lesson":
        s = db.get(MemoryShard, ref)
        if s is None or s.project_id != project.id:
            _refuse(f"cited lesson {ref!r} does not exist in this project")
    else:  # url
        if not re.match(r"^https?://[^\s/]+\S*$", ref):
            _refuse(f"cited url {ref!r} is not an http(s) url")
    return {"kind": kind, "ref": ref}


def _citations(db: Session, project: Project, raw: object) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        _refuse("a proposal needs at least one citation",
                      hint="cite an item, observation, lesson, file at a commit, or url")
    out: list[dict] = []
    for c in raw:
        r = _resolve_citation(db, project, c)
        if r not in out:
            out.append(r)
    return out


# ---- rule lookup -------------------------------------------------------------------------

def _primitive_of(rule_id: str, project_id: str, db: Session) -> str | None:
    if custom_rules.is_custom(rule_id):
        # Project -> org (GRPH-1779): an org-owned rule is a rule of each of its projects.
        try:
            return custom_rules.get(db, rule_id, project_id).primitive
        except NotFound:
            return None
    pack = rule_pack.CURRENT
    t = pack.templates.get(rule_id) if pack else None
    if t and t.get("primitive") in rule_pack.PRIMITIVES:
        return t["primitive"]
    return _PRIMITIVE_OF.get(rule_id)


# ---- the write ---------------------------------------------------------------------------

def propose(db: Session, *, project_id: str, proposer_kind: str, proposer: str, kind: str,
            citations: object, rule_id: str | None = None, slug: str | None = None,
            primitive: str | None = None, title: str | None = None, why: str | None = None,
            params: dict | None = None, claims: str | None = None) -> tuple[RuleProposal, bool]:
    """File a proposal. Returns `(proposal, folded)`; `folded` means it joined an open one."""
    if proposer_kind not in PROPOSER_KINDS:
        raise Validation(f"proposer_kind must be one of {PROPOSER_KINDS}")
    project = db.get(Project, project_id)
    if project is None:
        raise NotFound(f"project {project_id!r} not found")
    target = rule_id or (custom_rules.PREFIX + slug if slug else "")
    if kind in OFF_KINDS:
        raise Refused(403, f"turning off or removing rule {target or '(unnamed)'!r} is not a "
                      "proposal; only a person changes a rule off",
                      rule_id=T.PERSON_RULE,
                      hint="propose a `tighten`, or ask a person to switch it off")
    if kind not in KINDS:
        raise Validation(f"kind must be one of {KINDS}")
    for inv, why_inv in INVARIANTS.items():
        if target == inv or (primitive or "") == inv:
            raise Refused(422, f"{inv!r} is an invariant ({why_inv}); it is not a team "
                          "setting", rule_id=T.PERSON_RULE)
    if params is not None and not isinstance(params, dict):
        raise Validation("`params` must be an object")
    cites = _citations(db, project, citations)
    params = copy.deepcopy(params or {})

    draft: dict = {}
    if kind == "new_rule":
        if not primitive or not slug:
            raise Validation("a `new_rule` needs `slug` and `primitive`")
        if not custom_rules.SLUG.match(slug):
            raise Validation(custom_rules.SLUG_WORDS)
        prim = rule_pack.PRIMITIVES.get(primitive)
        if prim is None:
            raise Validation(f"primitive {primitive!r} does not exist in primitive API "
                             f"{rule_pack.PRIMITIVE_API}")
        custom_rules.refuse_unevaluated(primitive)
        rule_id = custom_rules.PREFIX + slug
        draft = {"id": rule_id, "stage": prim.stage, "title": (title or "").strip(),
                 "summary": (why or "").strip(), "primitive": primitive,
                 "claims": claims or prim.level, "params": params}
        if not draft["title"] or not draft["summary"]:
            raise Validation("a `new_rule` needs a `title` and a `why`")
        bad = rule_pack.validate_draft(draft)
        if bad:
            _refuse(f"the draft is invalid: {bad}")
        if custom_rules._row(db, rule_id, project_id) is not None:
            raise Validation(f"{rule_id} already exists in this project")
        direction = rule_pack.direction(primitive, None, params)
        new_params = params
    else:
        if not rule_id:
            raise Validation(f"`{kind}` needs a `rule_id`")
        primitive = _primitive_of(rule_id, project_id, db)
        if primitive is None:
            raise NotFound(f"{rule_id!r} is not a library or custom rule of this project")
        cur = T.resolve(db, rule_id, project_id)
        cur_params = copy.deepcopy(cur["params"]["value"] or {})
        if kind == "turn_on":
            if cur["on"]["value"]:
                _refuse(f"{rule_id!r} is already on")
            new_params = {**cur_params, **params}
            direction = rule_pack.direction(primitive, None, new_params)
        else:  # tighten
            if not params:
                raise Validation("a `tighten` needs the `params` to change")
            new_params = {**cur_params, **params}
            if new_params == cur_params:
                _refuse(f"{rule_id!r} already has those params")
            direction = rule_pack.direction(primitive, cur_params, new_params)
            if direction == "not_proposable":
                _refuse(f"{rule_id!r} has a changed param with no comparator, so its "
                              "direction cannot be computed; a person sets it")
        draft = {"params": new_params}
    if direction == "not_proposable":
        _refuse(f"the change to {rule_id!r} has no computable direction")
    loosens = direction == "loosen"
    if loosens and proposer_kind == "agent":
        raise Refused(403, f"proposing {rule_id!r} would loosen it; an agent may only tighten",
                      rule_id=T.PERSON_RULE,
                      hint="a person or an import run may propose a loosening")

    # Fold: same primitive and params as an open proposal of the same kind. A new rule folds
    # whatever slug it was given; a change to an existing rule folds only on that rule.
    q = select(RuleProposal).where(
        RuleProposal.project_id == project_id, RuleProposal.status == "open",
        RuleProposal.kind == kind, RuleProposal.primitive == primitive)
    if kind != "new_rule":
        q = q.where(RuleProposal.rule_id == rule_id)
    # A dismissed proposal stays dismissed until it brings a citation it did not have.
    dq = select(RuleProposal).where(
        RuleProposal.project_id == project_id, RuleProposal.status == "dismissed",
        RuleProposal.kind == kind, RuleProposal.primitive == primitive)
    if kind != "new_rule":
        dq = dq.where(RuleProposal.rule_id == rule_id)
    for dis in db.scalars(dq.order_by(RuleProposal.id)):
        if dis.params == new_params and all(c in (dis.citations or []) for c in cites):
            _refuse(f"a proposal for {rule_id!r} with these params was dismissed"
                    + (f" ({dis.decided_reason})" if dis.decided_reason else ""),
                    hint="cite something new to reopen it")
    for open_p in db.scalars(q.order_by(RuleProposal.id)):
        if open_p.params == new_params:
            have = list(open_p.citations or [])
            open_p.citations = have + [c for c in cites if c not in have]
            db.commit()
            return open_p, True
    row = RuleProposal(project_id=project_id, kind=kind, rule_id=rule_id, primitive=primitive,
                       draft=draft, params=new_params, citations=cites, proposer=proposer,
                       proposer_kind=proposer_kind, direction=direction, loosens=loosens,
                       status="open")
    db.add(row)
    db.flush()
    db.commit()
    return row, False


def view(row: RuleProposal, *, folded: bool = False) -> dict:
    return {"id": row.id, "project_id": row.project_id, "kind": row.kind, "rule_id": row.rule_id,
            "primitive": row.primitive, "draft": row.draft, "params": row.params,
            "citations": row.citations, "proposer": row.proposer,
            "proposer_kind": row.proposer_kind, "direction": row.direction,
            "loosens": row.loosens, "status": row.status, "folded": folded,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "decided_by": row.decided_by, "decided_reason": row.decided_reason,
            "decided_at": row.decided_at.isoformat() if row.decided_at else None,
            "org_dismissed_by": row.org_dismissed_by,
            "org_dismissed_reason": row.org_dismissed_reason,
            "org_dismissed_at": (row.org_dismissed_at.isoformat()
                                 if row.org_dismissed_at else None)}


def list_for_project(db: Session, project_id: str, status: str | None = None) -> list[dict]:
    q = select(RuleProposal).where(RuleProposal.project_id == project_id)
    if status:
        q = q.where(RuleProposal.status == status)
    return [view(r) for r in db.scalars(q.order_by(RuleProposal.id))]


# ---- adoption (GRPH-1776 / P91 D4) -------------------------------------------------------
# A person's act (`rules.person_adopts`): REST only, project owner or admin. Every rule adopted
# from a proposal starts `observing` (D5); direct Rules-page writes stay immediate.

def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _history(p: RuleProposal) -> str:
    cites = ", ".join(f"{c['kind']}:{c['ref'] if isinstance(c['ref'], str) else c['ref'].get('path')}"
                      for c in (p.citations or []))
    return f"proposed by {p.proposer} (cited {cites})"


def _open_proposal(db: Session, project_id: str, proposal_id: int) -> RuleProposal:
    p = db.get(RuleProposal, proposal_id)
    if p is None or p.project_id != project_id:
        raise NotFound(f"proposal {proposal_id!r} not found in this project")
    if p.status != "open":
        raise Validation(f"proposal {proposal_id} is {p.status}, not open")
    return p


def _stage_adoption(db: Session, user, p: RuleProposal, reason: str, batch: bool,
                    claimed_slugs: set[str]):
    """Check and apply one proposal to the session, uncommitted, recording NOTHING.

    Returns a callable that records the proposal's audit events. Events commit
    (`events.record`), so the caller runs every recorder only after every proposal in the
    batch has staged: no commit may land before the last refusal point."""
    pid = p.project_id
    toggle = None
    if p.kind == "new_rule":
        d = p.draft or {}
        bad = rule_pack.validate_draft(d)
        if bad:
            raise Validation(f"the draft is no longer valid: {bad}")
        if p.rule_id in claimed_slugs:
            raise Validation(f"proposal {p.id} creates {p.rule_id}, which another proposal in "
                             "this batch already creates", hint="adopt one of them, dismiss the other")
        if custom_rules._row(db, p.rule_id, pid) is not None:
            raise Validation(f"{p.rule_id} already exists in this project")
        custom_rules.refuse_if_locked(db, p.rule_id, pid)
        claimed_slugs.add(p.rule_id)
        db.add(CustomRule(rule_id=p.rule_id, project_id=pid, primitive=p.primitive,
                          primitive_api=rule_pack.PRIMITIVE_API, stage=d["stage"],
                          params=copy.deepcopy(p.params or {}), title=d["title"],
                          why=d["summary"], claims=d["claims"], state="observing",
                          author_id=user.id, reason=reason))
        state = "observing"
    elif custom_rules.is_custom(p.rule_id):
        custom_rules.refuse_if_locked(db, p.rule_id, pid)
        row = custom_rules.get(db, p.rule_id, pid)
        if row.org_id:  # GRPH-1779: the org owns it; the project's change is a kept override
            row = CustomRule(rule_id=row.rule_id, project_id=pid, primitive=row.primitive,
                             primitive_api=row.primitive_api, stage=row.stage,
                             params={}, title=row.title, why=row.why, claims=row.claims,
                             state=row.state, author_id=user.id, reason=reason)
            db.add(row)
        row.params = copy.deepcopy(p.params or {})
        if p.kind == "turn_on":
            row.state = "observing"
        state = row.state
    else:
        changes: dict = {"params": copy.deepcopy(p.params or {})}
        if p.kind == "turn_on":
            changes["on"] = True
        prior = T._row(db, p.rule_id, project_id=pid)
        was_observing = prior.observing_since if prior is not None else None
        toggle = T._stage_project_toggle(db, pid, p.rule_id, changes, user)
        if p.kind == "turn_on":
            # D5 (GRPH-1777): an adopted library rule starts observing, not enforcing.
            toggle[0].observing_since = utcnow()
        elif was_observing is not None and hasattr(toggle[0], "observing_since"):
            # D5: promotion is a separate person act. Adopting a tighten on an observing rule
            # changes its params and keeps it observing, from when it began.
            toggle[0].observing_since = was_observing
        db.flush()
        resolved = T.resolve(db, p.rule_id, pid)
        state = ("observing" if p.kind == "turn_on" or resolved.get("state") == "observing"
                 else "on" if resolved["on"]["value"] else "off")
    p.status = "adopted"
    p.decided_by = user.handle or user.name
    p.decided_reason = reason
    p.decided_at = _now()
    db.flush()

    def record() -> None:
        if toggle is not None:
            row, before, old = toggle
            T._record_project_toggle(db, pid, p.rule_id, row, before, old, user, reason)
        events_svc.record_user(
            db, user, action="adopt_rule_proposal", target_type="rule", target_id=p.rule_id,
            project_id=pid,
            meta={"proposal_id": p.id, "kind": p.kind, "state": state, "reason": reason,
                  "batch": batch, "history": f"{_history(p)}, adopted by {p.decided_by}",
                  "proposer": p.proposer, "proposer_kind": p.proposer_kind,
                  "citations": p.citations, "adopted_by": p.decided_by})
    return record


def adopt(db: Session, actor: object, project_id: str, proposal_ids: list[int],
          reason: str) -> list[RuleProposal]:
    """Adopt one proposal or a batch under one reason, all-or-nothing. A `loosens` proposal is
    refused in a batch (naming it); adopted alone it needs its own reason.

    Every proposal is checked and staged before any audit event is recorded, because recording
    an event commits: a refusal at any member leaves every proposal open and no rule changed."""
    user = T._check_project_writer(db, actor, project_id)
    why = T._reason(reason)
    ids = list(dict.fromkeys(proposal_ids))
    if not ids:
        raise Validation("name at least one proposal to adopt")
    batch = len(ids) > 1
    try:
        rows = [_open_proposal(db, project_id, i) for i in ids]
        if batch:
            loose = [p.id for p in rows if p.loosens]
            if loose:
                raise Validation(
                    f"proposal {', '.join(map(str, loose))} loosens a rule and cannot be adopted "
                    "in a batch", hint="adopt it alone, with its own reason")
        slugs: set[str] = set()
        recorders = [_stage_adoption(db, user, p, why, batch, slugs) for p in rows]
        db.commit()
    except BaseException:
        db.rollback()
        raise
    # The adoption is committed; audit events follow (each commits on its own, and a failed
    # audit never undoes the audited act — events.record).
    for rec in recorders:
        rec()
    if batch:
        events_svc.record_user(
            db, user, action="adopt_rule_proposals", target_type="project",
            target_id=project_id, project_id=project_id,
            meta={"proposal_ids": ids, "rule_ids": [p.rule_id for p in rows], "reason": why})
    return rows


def dismiss(db: Session, actor: object, project_id: str, proposal_id: int,
            reason: str) -> RuleProposal:
    user = T._check_project_writer(db, actor, project_id)
    why = T._reason(reason)
    p = _open_proposal(db, project_id, proposal_id)
    p.status = "dismissed"
    p.decided_by = user.handle or user.name
    p.decided_reason = why
    p.decided_at = _now()
    events_svc.record_user(
        db, user, action="dismiss_rule_proposal", target_type="rule", target_id=p.rule_id,
        project_id=project_id,
        meta={"proposal_id": p.id, "reason": why, "history": _history(p)})
    db.commit()
    return p


# ---- guidance (GRPH-1780 / P91 D8) -------------------------------------------------------
# A requirement no primitive can express is filed as advisory text under a stage. It is cited
# like a proposal, adopted only by a person, and shown to agents in the delegation brief.

GUIDANCE_KIND = "guidance"
GUIDANCE_STATUSES = ("open", "adopted", "dismissed")
LABEL_MAX = 60
TEXT_MAX = 400

#: Item status -> the stage an agent working it is in. Used to pick the guidance for a brief.
STAGE_OF_STATUS = {"backlog": "Plan", "next": "Plan", "blocked": "Plan", "in_progress": "Build",
                   "review": "Review", "done": "Merge"}


def file_guidance(db: Session, *, project_id: str, proposer_kind: str, proposer: str,
                  stage: object, label: object, text: object, citations: object) -> RuleGuidance:
    """File guidance. Cited or refused. Counts one primitive gap by stage and label, never text."""
    if proposer_kind not in PROPOSER_KINDS:
        raise Validation(f"proposer_kind must be one of {PROPOSER_KINDS}")
    project = db.get(Project, project_id)
    if project is None:
        raise NotFound(f"project {project_id!r} not found")
    if stage not in rule_pack.STAGES:
        raise Validation(f"`stage` must be one of {rule_pack.STAGES}")
    label = label.strip() if isinstance(label, str) else ""
    text = text.strip() if isinstance(text, str) else ""
    if not label or len(label) > LABEL_MAX:
        raise Validation(f"`label` is a short name, 1-{LABEL_MAX} characters")
    if not text or len(text) > TEXT_MAX:
        raise Validation(f"`text` must be 1-{TEXT_MAX} characters")
    cites = _citations(db, project, citations)
    row = RuleGuidance(project_id=project_id, stage=stage, label=label, text=text,
                       citations=cites, proposer=proposer, proposer_kind=proposer_kind,
                       status="open")
    db.add(row)
    gap = db.scalar(select(PrimitiveGapCount).where(
        PrimitiveGapCount.project_id == project_id, PrimitiveGapCount.stage == stage,
        PrimitiveGapCount.label == label))
    if gap is None:
        db.add(PrimitiveGapCount(project_id=project_id, stage=stage, label=label, count=1))
    else:
        gap.count = (gap.count or 0) + 1
    db.flush()
    db.commit()
    return row


def guidance_view(row: RuleGuidance) -> dict:
    return {"id": row.id, "project_id": row.project_id, "kind": GUIDANCE_KIND,
            "stage": row.stage, "label": row.label, "text": row.text,
            "citations": row.citations, "proposer": row.proposer,
            "proposer_kind": row.proposer_kind, "status": row.status,
            "created_at": row.created_at.isoformat() if row.created_at else None}


def list_guidance(db: Session, project_id: str, status: str | None = None) -> list[dict]:
    q = select(RuleGuidance).where(RuleGuidance.project_id == project_id)
    if status:
        q = q.where(RuleGuidance.status == status)
    return [guidance_view(r) for r in db.scalars(q.order_by(RuleGuidance.id))]


def decide_guidance(db: Session, user, project_id: str, guidance_id: int, *,
                    adopt: bool) -> RuleGuidance:
    """A person adopts or dismisses open guidance. No agent path reaches this (`rules.person_adopts`)."""
    row = db.get(RuleGuidance, guidance_id)
    if row is None or row.project_id != project_id:
        raise NotFound(f"guidance {guidance_id!r} not found")
    if row.status != "open":
        raise Validation(f"guidance {guidance_id} is already {row.status}")
    row.status = "adopted" if adopt else "dismissed"
    row.decided_by = getattr(user, "handle", None) or getattr(user, "name", None) or user.id
    row.decided_at = datetime.now(timezone.utc)
    db.commit()
    return row


def adopted_for_stage(db: Session, project_id: str, stage: str, limit: int) -> list[dict]:
    """Adopted guidance for one stage, oldest first, as `{label, text}` for a brief."""
    rows = db.scalars(select(RuleGuidance).where(
        RuleGuidance.project_id == project_id, RuleGuidance.stage == stage,
        RuleGuidance.status == "adopted").order_by(RuleGuidance.id).limit(limit)).all()
    return [{"label": r.label, "text": r.text} for r in rows]


def gap_counts(db: Session, project_id: str) -> list[dict]:
    """Stage, label and count. The copied text is not stored here and never appears."""
    rows = db.scalars(select(PrimitiveGapCount).where(
        PrimitiveGapCount.project_id == project_id)
        .order_by(PrimitiveGapCount.stage, PrimitiveGapCount.label)).all()
    return [{"stage": r.stage, "label": r.label, "count": int(r.count or 0)} for r in rows]


# ---- org adoption (GRPH-1779 / P91 D7) ---------------------------------------------------
# Proposals are still filed on a project only. An org admin adopts one at org scope through the
# org path (`quotas.require_org_rules`), including one a project dismissed: both acts stay in
# history, the project's dismissal in its own event and the org's adoption in the org's.

def _org_proposal(db: Session, org_id: str, proposal_id: int) -> RuleProposal:
    p = db.get(RuleProposal, proposal_id)
    if p is None or T._org_of(db, p.project_id) != org_id:
        raise NotFound(f"proposal {proposal_id!r} not found in this org")
    return p


def _overrides(db: Session, org_id: str, p: RuleProposal) -> list[dict]:
    """The org's projects that already hold their own value for the proposal's rule, with that
    value: the admin sees whose value wins before adopting (the project's own value does)."""
    out = []
    for pid in custom_rules._org_projects(db, org_id):
        if custom_rules.is_custom(p.rule_id):
            row = custom_rules._row(db, p.rule_id, pid)
            if row is not None:
                out.append({"project_id": pid, "on": row.state != "off", "params": row.params,
                            "state": row.state})
        else:
            row = T._row(db, p.rule_id, project_id=pid)
            if row is not None and (row.on is not None or row.params is not None):
                out.append({"project_id": pid, "on": row.on, "params": row.params})
    return out


def _org_view(db: Session, org_id: str, p: RuleProposal) -> dict:
    d = view(p)
    d.update(decided_by=p.decided_by, decided_reason=p.decided_reason,
             overrides=_overrides(db, org_id, p))
    proj = db.get(Project, p.project_id)
    d["project_name"] = proj.name if proj is not None else None
    return d


def _org_admin(db: Session, actor: object, org_id: str):
    from app.security import authz
    from app.services import quotas
    user = T._require_person(db, actor)
    authz.require_org_admin(db, user.id, org_id)
    quotas.require_org_rules(db, org_id)
    return user


def list_for_org(db: Session, actor: object, org_id: str, status: str | None = None) -> list[dict]:
    """Proposals from every project in the org, each with its project. Default (`open`) is the
    org's open list: open in its project and not dismissed for the org. `dismissed` is the org's
    Dismissed: proposals an org admin dismissed at org scope (they may still be open in the
    project). Any other status filters on the project's status."""
    _org_admin(db, actor, org_id)
    pids = custom_rules._org_projects(db, org_id)
    q = select(RuleProposal).where(RuleProposal.project_id.in_(pids))
    st = status or "open"
    if st == "open":
        q = q.where(RuleProposal.status == "open", RuleProposal.org_dismissed_at.is_(None))
    elif st == "dismissed":
        q = q.where(RuleProposal.org_dismissed_at.is_not(None))
    else:
        q = q.where(RuleProposal.status == st)
    return [_org_view(db, org_id, p) for p in db.scalars(q.order_by(RuleProposal.id))]


def _stage_org(db: Session, user, org_id: str, p: RuleProposal, why: str,
               after: list, claimed: set[str]) -> dict:
    """Apply one proposal at org scope to the session, uncommitted. Appends to `after` what
    must run once the transaction has committed."""
    if p.status not in ("open", "dismissed"):
        raise Validation(f"proposal {p.id} is {p.status}, not open or dismissed")
    prior = (f"dismissed by {p.decided_by}: {p.decided_reason}"
             if p.status == "dismissed" else None)
    overrides = _overrides(db, org_id, p)
    effect: dict = {}
    if p.kind == "new_rule":
        d = p.draft or {}
        bad = rule_pack.validate_draft(d)
        if bad:
            raise Validation(f"the draft is no longer valid: {bad}")
        if p.rule_id in claimed:
            raise Validation(f"proposal {p.id} creates {p.rule_id}, which another proposal in "
                             "this batch already creates", hint="adopt one of them, dismiss the other")
        claimed.add(p.rule_id)
        custom_rules.stage_org_row(
            db, user, org_id, rule_id=p.rule_id, primitive=p.primitive, stage=d["stage"],
            params=p.params or {}, title=d["title"], why=d["summary"], claims=d["claims"],
            state="observing", reason=why)
        state = "observing"
    elif custom_rules.is_custom(p.rule_id):
        row = custom_rules.get_org(db, org_id, p.rule_id)
        row.params = copy.deepcopy(p.params or {})
        if p.kind == "turn_on":
            row.state = "observing"
        state = row.state
    else:
        changes: dict = {"params": copy.deepcopy(p.params or {})}
        if p.kind == "turn_on":
            changes["on"] = True
        out = T.set_org_toggle(db, org_id, p.rule_id, changes, actor=user, reason=why,
                               stage=after)
        effect = {"projects_changed": out["projects_changed"]}
        state = "on" if out["on"] else "off"
    p.status = "adopted"
    p.decided_by = user.handle or user.name
    p.decided_reason = why
    p.decided_at = _now()
    db.flush()
    history = _history(p)
    history += (f", {prior}, then adopted at org scope by {p.decided_by}" if prior
                else f", adopted at org scope by {p.decided_by}")
    return {"state": state, "effect": effect, "prior": prior, "history": history,
            "overrides": overrides}


def _org_effect(db: Session, org_id: str, p: RuleProposal, effect: dict) -> dict:
    if custom_rules.is_custom(p.rule_id):
        return custom_rules.org_effect(db, org_id, p.rule_id)
    return {"projects_changed": effect.get("projects_changed", []), "overrides_kept": []}


def _org_event(db: Session, user, org_id: str, p: RuleProposal, why: str, staged: dict,
               batch: bool) -> None:
    effect = _org_effect(db, org_id, p, staged["effect"])
    events_svc.record_user(
        db, user, action="adopt_rule_proposal", target_type="rule", target_id=p.rule_id,
        project_id=p.project_id,
        meta={"scope": "org", "org_id": org_id, "proposal_id": p.id, "kind": p.kind,
              "state": staged["state"], "reason": why, "history": staged["history"],
              "previously": staged["prior"], "proposer": p.proposer,
              "proposer_kind": p.proposer_kind, "citations": p.citations,
              "adopted_by": p.decided_by, "batch": batch, **effect})


def adopt_org_batch(db: Session, actor: object, org_id: str, proposal_ids: list[int],
                    reason: str, *, dry_run: bool = False) -> list[dict]:
    """Adopt proposals from any of the org's projects at org scope, one reason, all-or-nothing in
    one transaction. A `loosens` proposal is refused from a batch (adopt it alone). One audit
    event names every project changed. Each result lists the projects that already override the
    rule with their value. `dry_run` stages everything, reads it back and rolls it all back."""
    user = _org_admin(db, actor, org_id)
    why = T._reason(reason)
    ids = list(dict.fromkeys(proposal_ids))
    if not ids:
        raise Validation("name at least one proposal to adopt")
    batch = len(ids) > 1
    after: list = []
    try:
        rows = [_org_proposal(db, org_id, i) for i in ids]
        if batch:
            loose = [p.id for p in rows if p.loosens]
            if loose:
                raise Validation(
                    f"proposal {', '.join(map(str, loose))} loosens a rule and cannot be adopted "
                    "in a batch", hint="adopt it alone, with its own reason")
        claimed: set[str] = set()
        staged = [_stage_org(db, user, org_id, p, why, after, claimed) for p in rows]
        results = []
        for p, st in zip(rows, staged):
            eff = _org_effect(db, org_id, p, st["effect"])
            results.append(dict(_org_view(db, org_id, p), overrides=st["overrides"],
                                state=st["state"], projects_changed=eff["projects_changed"]))
        if dry_run:
            db.rollback()
            return results
        db.commit()
    except BaseException:
        db.rollback()
        raise
    for fn in after:
        fn()
    effects = [_org_effect(db, org_id, p, st["effect"]) for p, st in zip(rows, staged)]
    for p, st in zip(rows, staged):
        _org_event(db, user, org_id, p, why, st, batch)
    if batch:
        changed = sorted({pid for e in effects for pid in e["projects_changed"]})
        events_svc.record_user(
            db, user, action="adopt_rule_proposals", target_type="org", target_id=org_id,
            meta={"scope": "org", "org_id": org_id, "proposal_ids": ids,
                  "rule_ids": [p.rule_id for p in rows], "reason": why,
                  "projects_changed": changed,
                  "projects": sorted({p.project_id for p in rows})})
    return results


def adopt_org(db: Session, actor: object, org_id: str, proposal_id: int,
              reason: str) -> RuleProposal:
    """Adopt a project's proposal at org scope. A `new_rule` becomes the org's one rule,
    `observing`; a `turn_on` / `tighten` of a library rule is the org's toggle write. A project
    that already holds the same custom rule keeps it as an override. One audit event names the
    projects the change reached."""
    adopt_org_batch(db, actor, org_id, [proposal_id], reason)
    return db.get(RuleProposal, proposal_id)


def dismiss_org(db: Session, actor: object, org_id: str, proposal_id: int,
                reason: str) -> dict:
    """Dismiss for the org only: who, when and why go on the proposal, which leaves the org's
    open list. Its project status is untouched, so the project can still adopt it."""
    user = _org_admin(db, actor, org_id)
    why = T._reason(reason)
    p = _org_proposal(db, org_id, proposal_id)
    if p.status != "open":
        raise Validation(f"proposal {proposal_id} is {p.status}, not open")
    if p.org_dismissed_at is not None:
        raise Validation(f"proposal {proposal_id} is already dismissed for this org")
    p.org_dismissed_by = user.handle or user.name
    p.org_dismissed_reason = why
    p.org_dismissed_at = _now()
    events_svc.record_user(
        db, user, action="dismiss_rule_proposal", target_type="rule", target_id=p.rule_id,
        project_id=p.project_id,
        meta={"scope": "org", "org_id": org_id, "proposal_id": p.id, "reason": why,
              "history": _history(p)})
    db.commit()
    return _org_view(db, org_id, p)
