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
from collections.abc import Callable

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.errors import NotFound, Refused, Validation
from app.models import Item, MemoryShard, Observation, Project, RuleProposal
from app.services import custom_rules
from app.services import rule_pack
from app.services import rule_toggles as T

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

def _refuse(detail: str, hint: str | None = None, rule_id: str | None = None) -> Refused:
    return Refused(422, detail, rule_id=rule_id, hint=hint)


def _resolve_citation(db: Session, project: Project, c: object) -> dict:
    if not isinstance(c, dict) or c.get("kind") not in CITATION_KINDS:
        raise _refuse(f"a citation must be an object with kind one of {CITATION_KINDS}")
    kind, ref = c["kind"], c.get("ref")
    if kind == "file":
        f = ref if isinstance(ref, dict) else {}
        path, commit, lines = f.get("path"), f.get("commit"), f.get("lines")
        if not (isinstance(path, str) and path and isinstance(commit, str) and commit):
            raise _refuse("a `file` citation's ref needs `path` and `commit` (and `lines`)")
        if path.startswith("/") or ".." in path.split("/"):
            raise _refuse(f"file citation path {path!r} must be repo-relative")
        got = FILE_READER(project.repo_url or "", path, commit)
        if got is None:
            raise _refuse(f"could not verify {path!r} at {commit[:12]}: the project's forge was "
                          "not readable", hint="set the project's repo_url and retry")
        if not got:
            raise _refuse(f"{path!r} does not exist at {commit[:12]}")
        return {"kind": "file", "ref": {"path": path, "commit": commit,
                                        "lines": str(lines) if lines else ""}}
    if not isinstance(ref, (str, int)) or isinstance(ref, bool) or not str(ref).strip():
        raise _refuse(f"a `{kind}` citation needs a `ref`")
    ref = str(ref).strip()
    if kind == "item":
        it = db.get(Item, ref)
        if it is None or it.project_id != project.id:
            raise _refuse(f"cited item {ref!r} does not exist in this project")
    elif kind == "observation":
        o = db.get(Observation, int(ref)) if ref.isdigit() else None
        if o is None or o.project_id != project.id:
            raise _refuse(f"cited observation {ref!r} does not exist in this project")
    elif kind == "lesson":
        s = db.get(MemoryShard, ref)
        if s is None or s.project_id != project.id:
            raise _refuse(f"cited lesson {ref!r} does not exist in this project")
    else:  # url
        if not re.match(r"^https?://[^\s/]+\S*$", ref):
            raise _refuse(f"cited url {ref!r} is not an http(s) url")
    return {"kind": kind, "ref": ref}


def _citations(db: Session, project: Project, raw: object) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        raise _refuse("a proposal needs at least one citation",
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
        row = custom_rules._row(db, rule_id, project_id)
        return row.primitive if row else None
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
            raise _refuse(f"{inv!r} is an invariant ({why_inv}); it is not a team setting",
                          rule_id=T.PERSON_RULE)
    if params is not None and not isinstance(params, dict):
        raise Validation("`params` must be an object")
    cites = _citations(db, project, citations)
    params = copy.deepcopy(params or {})

    draft: dict = {}
    if kind == "new_rule":
        if not primitive or not slug:
            raise Validation("a `new_rule` needs `slug` and `primitive`")
        if not custom_rules.SLUG.match(slug):
            raise Validation("`slug` must be lower-case letters, digits, `-` or `_`, 1-64 long")
        prim = rule_pack.PRIMITIVES.get(primitive)
        if prim is None:
            raise Validation(f"primitive {primitive!r} does not exist in primitive API "
                             f"{rule_pack.PRIMITIVE_API}")
        rule_id = custom_rules.PREFIX + slug
        draft = {"id": rule_id, "stage": prim.stage, "title": (title or "").strip(),
                 "summary": (why or "").strip(), "primitive": primitive,
                 "claims": claims or prim.level, "params": params}
        if not draft["title"] or not draft["summary"]:
            raise Validation("a `new_rule` needs a `title` and a `why`")
        bad = rule_pack.validate_draft(draft)
        if bad:
            raise _refuse(f"the draft is invalid: {bad}")
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
                raise _refuse(f"{rule_id!r} is already on")
            new_params = {**cur_params, **params}
            direction = rule_pack.direction(primitive, None, new_params)
        else:  # tighten
            if not params:
                raise Validation("a `tighten` needs the `params` to change")
            new_params = {**cur_params, **params}
            if new_params == cur_params:
                raise _refuse(f"{rule_id!r} already has those params")
            direction = rule_pack.direction(primitive, cur_params, new_params)
            if direction == "not_proposable":
                raise _refuse(f"{rule_id!r} has a changed param with no comparator, so its "
                              "direction cannot be computed; a person sets it")
        draft = {"params": new_params}
    if direction == "not_proposable":
        raise _refuse(f"the change to {rule_id!r} has no computable direction")
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
            "created_at": row.created_at.isoformat() if row.created_at else None}


def list_for_project(db: Session, project_id: str, status: str | None = None) -> list[dict]:
    q = select(RuleProposal).where(RuleProposal.project_id == project_id)
    if status:
        q = q.where(RuleProposal.status == status)
    return [view(r) for r in db.scalars(q.order_by(RuleProposal.id))]
