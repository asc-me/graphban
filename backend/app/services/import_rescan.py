"""Rescan and drift: an import on a project that already has adopted rules (GRPH-1953 / P92 D5).

`gban rules import` runs the structured parsers (`import_sources`) over the repo at a commit and
this module compares each finding with what the project holds. Only differences are filed:

- a **new** requirement (nothing in force says it) files as before;
- a **tightened** one files the tighter value (a list grows; a custom rule gains paths);
- a finding that says what an adopted rule already says is not filed. If it cites a different
  file than the rule was adopted from (the source moved or was renamed) it becomes a **source
  moved** notice;
- an adopted rule that no finding at the new commit still supports becomes a **source gone**
  notice.

A finding matches a rule on what the rule says (its primitive and params), never on where it was
cited. Notices are shown on the run's report and never acted on: no rule changes, no proposal is
filed. Loosening a rule whose source has gone stays a person's act.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import AppError
from app.models import CustomRule, Project, RuleGuidance, RuleProposal, User
from app.services import custom_rules, import_runs, import_sources as S
from app.services import rule_toggles as T

NOTICE = import_runs.NOTICE
GONE = "source gone"
MOVED = "source moved"

#: The stage a library rule's notice is drawn under, when the finding does not name one.
_STAGE_OF = {"build": "Build", "plan": "Plan", "review": "Review", "merge": "Merge",
             "spec": "Spec"}


def _paths(citations: object) -> set[str]:
    return {c["ref"]["path"] for c in citations or [] if isinstance(c, dict)
            and c.get("kind") == "file" and isinstance(c.get("ref"), dict)
            and isinstance(c["ref"].get("path"), str)}


def _covers(have: dict, want: dict) -> bool:
    """`have` already says everything `want` says: each list holds `want`'s entries, each other
    param is equal."""
    for k, v in (want or {}).items():
        h = (have or {}).get(k)
        if isinstance(v, list):
            if not isinstance(h, list) or not set(map(str, v)) <= set(map(str, h)):
                return False
        elif h != v:
            return False
    return True


def _overlaps(have: dict, want: dict) -> bool:
    """`want` still says some of what `have` says: a shared list entry or an equal value."""
    for k, v in (want or {}).items():
        h = (have or {}).get(k)
        if isinstance(v, list) and isinstance(h, list):
            if set(map(str, v)) & set(map(str, h)):
                return True
        elif h is not None and h == v:
            return True
    return not want and not have


def _state(db: Session, rule_id: str, project_id: str) -> tuple[bool, dict] | None:
    """`(on, params)` of a rule in force for the project, or None when it does not exist."""
    try:
        st = T.resolve(db, rule_id, project_id)
    except (AppError, KeyError):
        return None
    return bool(st["on"]["value"]), dict(st["params"]["value"] or {})


def _target(f: dict) -> str | None:
    p = f.get("proposal") or {}
    if p.get("kind") == "new_rule":
        return custom_rules.PREFIX + p["slug"] if p.get("slug") else None
    return p.get("rule_id")


class _Held:
    """What the project holds, read once per rescan."""

    def __init__(self, db: Session, project_id: str):
        self.db, self.project_id = db, project_id
        self.adopted = list(db.scalars(select(RuleProposal).where(
            RuleProposal.project_id == project_id, RuleProposal.status == "adopted")
            .order_by(RuleProposal.id)))
        self.custom = list(db.scalars(select(CustomRule).where(
            CustomRule.project_id == project_id)))
        self.guidance = list(db.scalars(select(RuleGuidance).where(
            RuleGuidance.project_id == project_id,
            RuleGuidance.status.in_(("open", "adopted")))))

    def adopted_paths(self, rule_id: str) -> set[str]:
        out: set[str] = set()
        for p in self.adopted:
            if p.rule_id == rule_id:
                out |= _paths(p.citations)
        return out


def _notice(label: str, stage: str, quote: str, path: str, text: str, **source) -> dict:
    return {"bucket": NOTICE, "stage": stage, "quote": quote, "label": label, "text": text,
            "source": {"path": path, **source}}


def _moved(rule_id: str, stage: str, f: dict, old: set[str]) -> dict:
    new = sorted(_paths((f.get("proposal") or {}).get("citations") or f.get("citations")))
    return _notice(
        MOVED, stage, f["quote"], new[0] if new else f["source"]["path"],
        f"{rule_id} already says this; it was adopted from {', '.join(sorted(old))} and is now "
        f"cited at {', '.join(new)}. Nothing filed.", **{k: v for k, v in f["source"].items()
                                                        if k != "path"})


def _match_proposal(held: _Held, f: dict) -> tuple[str, dict | None]:
    """`("file", finding)` to file (possibly rewritten as a tighter value), `("held", None)`
    when the project already holds it from the same source, or `("moved", notice)`."""
    p = f["proposal"]
    if p.get("kind") == "new_rule":
        same = [r for r in held.custom if r.primitive == p.get("primitive")
                and _covers(r.params or {}, p.get("params") or {})]
        if same:
            return _settle(held, f, same[0].rule_id, same[0].stage)
        rid = custom_rules.PREFIX + str(p.get("slug"))
        row = next((r for r in held.custom if r.rule_id == rid), None)
        if row is not None:
            # The rule exists with fewer paths: the difference is a tighten of that rule.
            params = {}
            for k, v in (p.get("params") or {}).items():
                have = (row.params or {}).get(k)
                params[k] = (list(have) + [x for x in v if x not in have]
                             if isinstance(v, list) and isinstance(have, list) else v)
            f = {**f, "proposal": {"kind": "tighten", "rule_id": rid, "params": params,
                                   "why": p.get("why"), "citations": p.get("citations")}}
        return "file", f
    rid = p.get("rule_id") or ""
    st = _state(held.db, rid, held.project_id)
    if st is None:
        return "file", f
    on, params = st
    if (p.get("kind") == "turn_on" and not on) or not _covers(params, p.get("params") or {}):
        return "file", f
    return _settle(held, f, rid, f["stage"])


def _settle(held: _Held, f: dict, rule_id: str, stage: str) -> tuple[str, dict | None]:
    old = held.adopted_paths(rule_id)
    new = _paths((f.get("proposal") or {}).get("citations"))
    if old and new and not (old & new):
        return "moved", _moved(rule_id, stage, f, old)
    return "held", None


def _match_gap(held: _Held, f: dict) -> tuple[str, dict | None]:
    same = [g for g in held.guidance if g.stage == f.get("stage") and g.label == f.get("label")
            and g.text == (f.get("text") or "").strip()]
    if not same:
        return "file", f
    old = set().union(*(_paths(g.citations) for g in same))
    new = _paths(f.get("citations"))
    if old and new and not (old & new):
        return "moved", _moved(f"guidance “{f.get('label')}”", f["stage"], f, old)
    return "held", None


def _dismissed(held: _Held, f: dict) -> bool:
    """A person dismissed this exact change: filing it again would be refused, so a rescan
    leaves it alone rather than reporting the same refusal on every run."""
    p = f.get("proposal")
    if not p or f["bucket"] != "proposed":
        return False
    rows = held.db.scalars(select(RuleProposal).where(
        RuleProposal.project_id == held.project_id, RuleProposal.status == "dismissed",
        RuleProposal.kind == p.get("kind")))
    for r in rows:
        if p.get("kind") == "new_rule":
            if r.primitive == p.get("primitive") and r.params == p.get("params"):
                return True
        elif r.rule_id == p.get("rule_id") and _covers(r.params or {}, p.get("params") or {}):
            return True
    return False


def _supported(raw: list[dict], p: RuleProposal) -> bool:
    for f in raw:
        q = f.get("proposal")
        if not q or f.get("bucket") != "proposed":
            continue
        if _target(f) == p.rule_id or (q.get("kind") == "new_rule" and p.kind == "new_rule"
                                       and q.get("primitive") == p.primitive):
            if _overlaps(p.params or {}, q.get("params") or {}):
                return True
    return False


def _unread_paths(unread: list[dict]) -> set[str]:
    return {str(u.get("path", "")).split(":", 1)[0].strip() for u in unread}


def _gone(held: _Held, raw: list[dict], unread: list[dict], commit: str) -> list[dict]:
    """A notice per adopted rule the parsers filed (`proposer_kind == "import"`) that no finding
    at `commit` still supports. A rule whose source could not be read is not called gone: an
    unread source is not an absent one."""
    blind = _unread_paths(unread)
    protection_blind = any(str(u.get("path", "")).startswith("branch protection")
                           for u in unread)
    out: list[dict] = []
    seen: set[str] = set()
    for p in reversed(held.adopted):  # newest first: the latest adoption names the source
        if p.proposer_kind != "import" or p.rule_id in seen:
            continue
        paths = _paths(p.citations)
        if not paths or all(import_runs.is_prose(x) for x in paths):
            continue  # prose is the skill's; a structured rescan says nothing about it
        if paths & blind or (protection_blind and p.rule_id == S.DONE_RULE):
            continue
        if _state(held.db, p.rule_id, held.project_id) is None:
            continue  # a person already removed it
        seen.add(p.rule_id)
        if _supported(raw, p):
            continue
        stage = ((p.draft or {}).get("stage") or _STAGE_OF.get(
            p.rule_id.split(".", 1)[0], "Build"))
        out.append(_notice(
            GONE, stage, f"{p.rule_id}: no finding at {commit[:12]} still supports it",
            sorted(paths)[0],
            f"{p.rule_id} was adopted from {', '.join(sorted(paths))} (proposal {p.id}). Nothing "
            "at this commit still says it. The rule is unchanged: only a person loosens or "
            "removes a rule."))
    for g in held.guidance:
        if g.status != "adopted" or g.proposer_kind != "import":
            continue
        paths = _paths(g.citations)
        if not paths or paths & blind or all(import_runs.is_prose(x) for x in paths):
            continue
        if any(f.get("bucket") == "gap" and f.get("stage") == g.stage
               and f.get("label") == g.label for f in raw):
            continue
        out.append(_notice(
            GONE, g.stage, f"guidance “{g.label}”: no finding at {commit[:12]} still supports "
            "it", sorted(paths)[0],
            f"Guidance “{g.label}” was adopted from {', '.join(sorted(paths))}. Nothing at this "
            "commit still says it. It is unchanged: only a person removes it."))
    return out


def compare(db: Session, project_id: str, read: dict) -> tuple[list[dict], list[dict]]:
    """`(findings_to_file, notices)` for a structured read (`import_sources.read_sources`,
    run WITHOUT `current`). Externals are recorded as before: they file nothing."""
    held = _Held(db, project_id)
    raw = read["findings"]
    current = S.current_params(db, project_id)
    keep: list[dict] = []
    notices: list[dict] = []
    for f in raw:
        if f["bucket"] == "proposed" and f.get("proposal"):
            how, out = _match_proposal(held, f)
        elif f["bucket"] == "gap":
            how, out = _match_gap(held, f)
        else:
            how, out = "file", f
        if how == "file":
            keep.append(out)
        elif how == "moved":
            notices.append(out)
    # A library list rule's proposal replaces the list whole: carry the adopted entries. An
    # engine already on with another engine becomes a gap here, so gaps are matched again.
    keep = [f for f in S._against_current(keep, current)
            if not _dismissed(held, f)
            and (f["bucket"] != "gap" or _match_gap(held, f)[0] == "file")]
    notices += _gone(held, raw, read.get("sources_unread") or [], read["commit"])
    return keep, notices


def scan(db: Session, user: User, project_id: str, *, commit: object, files: object,
         branch: object = None, sources_unread: object = None,
         protection_reader=None) -> int:
    """Run the parsers over `files` (path -> text, as the CLI read them at `commit`), compare
    with what the project holds, and record one run. Returns the run id."""
    from app.errors import NotFound, Validation
    from app.security import authz
    authz.require_writable(db, user.id, project_id)
    project = db.get(Project, project_id)
    if project is None:
        raise NotFound(f"project {project_id!r} not found")
    if not isinstance(files, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in files.items()):
        raise Validation("`files` must map each repo path to its text")
    sha = import_runs._str(commit, "commit")
    br = branch.strip() if isinstance(branch, str) and branch.strip() else "main"
    repo = S.repo_of(project.repo_url)
    protection, punread = None, None
    if repo is None:
        punread = S.unread(f"branch protection: {br}",
                           "the project has no GitHub repo set, so the forge was not read")
    else:
        protection, punread = (protection_reader or S.fetch_branch_protection)(repo, br)
    read = S.read_sources(files, commit=sha, repo=repo, branch=br, protection=protection,
                          protection_unread=punread)
    read["sources_unread"] = list(read["sources_unread"]) + import_runs._sources(
        sources_unread, "sources_unread")
    findings, notices = compare(db, project_id, read)
    run = import_runs.create_run(db, user, project_id, commit=sha,
                                 sources_read=read["sources_read"],
                                 sources_unread=read["sources_unread"], findings=findings,
                                 notices=notices)
    return run.id
