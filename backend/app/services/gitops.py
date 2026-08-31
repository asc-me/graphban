"""Sparse delivery contract: org house process + project overlay.

Unset is unmeasured, never a default branch and never "no requirements". Linked live
reads come from the cloud; local columns are `was` only.
"""
from __future__ import annotations

import json
import logging
import re

import httpx
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Organization, Project
from app.schemas import (
    GitopsControl,
    GitopsField,
    GitopsFields,
    GitopsProjectRef,
    GitopsView,
    GitopsWas,
)
from app.security import authz
from app.services import code_sync

logger = logging.getLogger("graphban.gitops")

REVIEWER_BARS = ("sign_off", "forge", "both")
NAMING_TOKENS = ("item_id", "tag", "slug", "version", "date")
VERSION_SCHEMES = ("git_tag", "semver", "calver")
GLOB_METACHARS = ("*", "?", "[")
FIELD_SOURCES = ("project", "org", "unmeasured")
CONTROL_STATES = ("local", "linked_set", "linked_unset", "linked_unreachable")
FIELDS = (
    "base_branch",
    "no_push_to_base",
    "branch_name_pattern",
    "pr_title_pattern",
    "reviewer_bar",
)

COL = {
    "base_branch": "gitops_base_branch",
    "no_push_to_base": "gitops_no_push_to_base",
    "branch_name_pattern": "gitops_branch_name_pattern",
    "pr_title_pattern": "gitops_pr_title_pattern",
    "reviewer_bar": "gitops_reviewer_bar",
    "version_from": "gitops_version_scheme",
}
_PATTERN_FIELDS = ("branch_name_pattern", "pr_title_pattern")
_LITERAL_FIELDS = ("base_branch",)
_TOKEN_RE = re.compile(r"\{([^{}]+)\}")
_TOKENS_HELP = ", ".join("{" + t + "}" for t in NAMING_TOKENS)
_FETCH_TIMEOUT = 3.0

LINKED_PATCH_DETAIL = "gitops on a linked instance is owned by the org admin"

MESSAGES = {
    "local": "",
    "linked_unset": "Linked; the org has not set a git process.",
    "linked_set": "Controlled by the org admin.",
    "linked_unreachable": (
        "Linked; the org could not be reached. Git process is unmeasured — not the local values."
    ),
}


def _unmeasured() -> GitopsField:
    return GitopsField(value=None, source="unmeasured")


def _fields_from(pairs: dict[str, GitopsField]) -> GitopsFields:
    return GitopsFields(**{f: pairs[f] for f in FIELDS})


def _empty_fields() -> GitopsFields:
    return _fields_from({f: _unmeasured() for f in FIELDS})


def _pick(project: Project | None, org: Organization | None, field: str) -> GitopsField:
    col = COL[field]
    if project is not None:
        value = getattr(project, col)
        if value is not None:
            return GitopsField(value=value, source="project")
    if org is not None:
        value = getattr(org, col)
        if value is not None:
            return GitopsField(value=value, source="org")
    return _unmeasured()


def _snapshot(project: Project | None) -> GitopsWas:
    if project is None:
        return GitopsWas()
    return GitopsWas(**{f: getattr(project, COL[f]) for f in FIELDS})


def _control(state: str, *, writable: bool = False) -> GitopsControl:
    return GitopsControl(state=state, writable=writable, message=MESSAGES[state])


def _view(*, project_id, org_id, fields, version_from, state, was=None, projects=None,
          writable=False) -> GitopsView:
    return GitopsView(
        project_id=project_id,
        org_id=org_id,
        fields=fields,
        version_from=version_from,
        control=_control(state, writable=writable),
        was=was,
        projects=projects,
    )


def _unmeasured_view(*, state: str, project_id=None, org_id=None, was=None) -> GitopsView:
    return _view(
        project_id=project_id,
        org_id=org_id,
        fields=_empty_fields(),
        version_from=_unmeasured(),
        state=state,
        was=was,
    )


def _org_of(db: Session, project: Project | None) -> Organization | None:
    if project is None or not project.org_id:
        return None
    return db.get(Organization, project.org_id)


def resolve_local(db: Session, project_id: str | None) -> GitopsView:
    """This database's org+project rows. Never inspects link_status. Never outbound.

    GET /api/sync/gitops calls ONLY this so a box whose SYNC_CLOUD_URL points at itself
    cannot recurse.
    """
    if not project_id:
        return _unmeasured_view(state="local")
    project = db.get(Project, project_id)
    if project is None:
        return _unmeasured_view(state="local", project_id=project_id)
    org = _org_of(db, project)
    return _view(
        project_id=project.id,
        org_id=project.org_id,
        fields=_fields_from({f: _pick(project, org, f) for f in FIELDS}),
        version_from=_pick(project, org, "version_from"),
        state="local",
    )


def resolve_org(db: Session, org_id: str) -> GitopsView:
    """House process only. source is org or unmeasured — never project. Roster is every
    project with this org_id, not the caller's readable set."""
    org = db.get(Organization, org_id)
    pairs = {}
    for f in FIELDS:
        if org is not None:
            value = getattr(org, COL[f])
            pairs[f] = GitopsField(value=value, source="org") if value is not None else _unmeasured()
        else:
            pairs[f] = _unmeasured()
    if org is not None:
        scheme = org.gitops_version_scheme
        version_from = GitopsField(value=scheme, source="org") if scheme is not None else _unmeasured()
    else:
        version_from = _unmeasured()
    rows = db.scalars(
        select(Project).where(Project.org_id == org_id).order_by(Project.name)
    ).all()
    roster = [GitopsProjectRef(id=p.id, name=p.name, tag=p.tag) for p in rows]
    return _view(
        project_id=None,
        org_id=org_id,
        fields=_fields_from(pairs),
        version_from=version_from,
        state="local",
        projects=roster,
    )


def live_view(cloud: GitopsView, *, was: GitopsWas, project_id: str | None) -> GitopsView:
    """Rewrite cloud control. A sync GET is resolve_local, so its state is local and its
    writable is meaningless for this box — passing it through would look editable."""
    if any(getattr(cloud.fields, f).source != "unmeasured" for f in FIELDS):
        state = "linked_set"
    else:
        state = "linked_unset"
    return _view(
        project_id=project_id,
        org_id=cloud.org_id,
        fields=cloud.fields,
        version_from=cloud.version_from,
        state=state,
        was=was,
        writable=False,
    )


def fetch_cloud_gitops(db: Session) -> GitopsView | None:
    """GET {cloud_url}/api/sync/gitops. None on any failure. Never raises into MCP."""
    creds = code_sync.cloud_credentials(db)
    if creds is None:
        return None
    url, key = creds
    try:
        resp = httpx.get(
            f"{url.rstrip('/')}/api/sync/gitops",
            headers={"X-API-Key": key},
            timeout=_FETCH_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning("gitops cloud fetch status=%s", resp.status_code)
            return None
        return GitopsView.model_validate(resp.json())
    except (httpx.TimeoutException, httpx.HTTPError, OSError, ValidationError,
            json.JSONDecodeError, ValueError, TypeError) as e:
        logger.warning("gitops cloud fetch failed: %s", type(e).__name__)
        return None


def resolve(db: Session, project_id: str | None) -> GitopsView:
    """Live contract for a box: cloud when linked, else this DB. Unreachable is
    unmeasured, never the local row."""
    if project_id is None:
        view = resolve_local(db, None)
        logger.info("gitops.resolve project_id=%s state=%s linked_source=%s",
                    None, view.control.state, "")
        return view
    link = code_sync.link_status(db)
    if link["linked"]:
        project = db.get(Project, project_id)
        was = _snapshot(project)
        live = fetch_cloud_gitops(db)
        if live is None:
            view = _unmeasured_view(
                state="linked_unreachable",
                project_id=project_id,
                org_id=project.org_id if project is not None else None,
                was=was,
            )
        else:
            view = live_view(live, was=was, project_id=project_id)
        logger.info("gitops.resolve project_id=%s state=%s linked_source=%s",
                    project_id, view.control.state, link.get("source") or "")
        return view
    view = resolve_local(db, project_id)
    logger.info("gitops.resolve project_id=%s state=%s linked_source=%s",
                project_id, view.control.state, "")
    return view


def writable_for(db: Session, user_id: str, *, state: str, org_id: str | None,
                 project_id: str | None) -> bool:
    if state != "local":
        return False
    if org_id:
        return authz.org_admin_rank(authz.org_role(db, user_id, org_id))
    return project_id in authz.writable_project_ids(db, user_id)


def fill_writable(view: GitopsView, db: Session, user_id: str) -> GitopsView:
    view.control.writable = writable_for(
        db, user_id, state=view.control.state, org_id=view.org_id, project_id=view.project_id,
    )
    return view


def for_agent(view: GitopsView) -> dict:
    out: dict = {}
    for f in FIELDS:
        field = getattr(view.fields, f)
        out[f] = {"value": field.value, "source": field.source}
    out["tokens"] = list(NAMING_TOKENS)
    out["version_from"] = {
        "value": view.version_from.value,
        "source": view.version_from.source,
    }
    if view.control.state != "local":
        out["control"] = view.control.state
    if view.control.state == "linked_unreachable":
        out["note"] = (
            "linked; the org could not be reached — do not treat unset fields "
            "as no process"
        )
    elif any(out[f]["source"] == "unmeasured" for f in FIELDS):
        out["note"] = (
            "unset fields are unmeasured — not 'use main' and not 'no requirements'"
        )
    return out


def _has_glob(value: str) -> bool:
    return any(ch in value for ch in GLOB_METACHARS)


def _tokens_in(value: str) -> list[str]:
    return _TOKEN_RE.findall(value)


def _normalize_str(field: str, raw: str) -> str | None:
    value = raw.strip()
    if not value:
        return None
    if _has_glob(value):
        raise ValueError(
            f"{field} cannot contain glob metacharacters {GLOB_METACHARS}; "
            f"allowed naming tokens: {_TOKENS_HELP}"
        )
    found = _tokens_in(value)
    if field in _LITERAL_FIELDS and found:
        raise ValueError(
            f"{field} is a literal; naming tokens ({_TOKENS_HELP}) are for patterns only"
        )
    if field in _PATTERN_FIELDS:
        unknown = [t for t in found if t not in NAMING_TOKENS]
        if unknown:
            raise ValueError(
                f"unknown token {{{unknown[0]}}} in {field}; "
                f"allowed naming tokens: {_TOKENS_HELP}"
            )
    if field == "reviewer_bar" and value not in REVIEWER_BARS:
        raise ValueError(f"reviewer_bar must be one of: {', '.join(REVIEWER_BARS)}")
    if field == "version_from" and value not in VERSION_SCHEMES:
        raise ValueError(f"version_from must be one of: {', '.join(VERSION_SCHEMES)}")
    return value


def normalize(field: str, raw) -> str | bool | None:
    if raw is None:
        return None
    if field == "no_push_to_base":
        return bool(raw)
    if not isinstance(raw, str):
        raise ValueError(f"{field} must be a string or null")
    return _normalize_str(field, raw)


def apply_patch(row: Organization | Project, sent: dict) -> list[str]:
    """Write only the keys the client sent. Omitted keys are unchanged; null clears."""
    changed: list[str] = []
    for field in sent:
        if field not in COL:
            raise ValueError(f"unknown gitops field {field!r}")
        value = normalize(field, sent[field])
        setattr(row, COL[field], value)
        changed.append(field)
    return changed
