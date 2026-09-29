"""Read-only aggregation endpoints for the Dashboard, Roadmap, Links, and MCP views."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.mcp_server import TOOLS
from app.models import User
from app.security import authz
from app.security.deps import get_current_user
from app.services import dashboard as dash_svc
from app.services import events as events_svc
from app.services import links as links_svc
from app.services import mcp_stats
from app.services import roadmap as roadmap_svc


def _ref_key(db, stored_id: str) -> str:
    """A link endpoint is an item OR a request (`links.a`/`b` are untyped), so try both.
    Falls back to the stored id so a dangling edge still serializes (PRD-13)."""
    from app.services import keys

    for kind in ("item", "request"):
        row = db.get(keys.MODELS[kind], stored_id)
        if row is not None:
            return row.key
    return stored_id

router = APIRouter(tags=["analytics"])


@router.get("/events")
def events(
    project_id: str | None = None,
    action: str | None = None,
    lens: str | None = None,
    actor: str | None = None,
    surface: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    bucket: int | None = None,
    time_range: str | None = Query(None, alias="range"),
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """The audit ledger (AL-43): who did what, most-recent-first. Scoped to the
    caller's readable projects; a `project_id` narrows to one (must be readable).

    Also the Activity view's one read (PRD-47 S10): `lens` / `actor` / `surface` /
    `target_type` / `target_id` / `bucket` / `range` filter the rows, and the response
    carries the lens counts, the stacked histogram and the three facets for the same
    selection. With no filters the payload is the ledger it always was and
    `histogram.coverage` says `not_requested` rather than returning 48 empty bars.

    A filter value this endpoint does not define is a 422, not a widening: an unknown
    lens quietly read as "everything" would answer a question nobody asked.
    """
    readable = authz.readable_project_ids(db, user.id)
    if project_id is not None:
        authz.require_readable(db, user.id, project_id)
        readable = [project_id]
    try:
        return events_svc.list_events(
            db, project_ids=readable, limit=limit, offset=offset, action=action,
            lens=lens, actor=actor, surface=surface, target_type=target_type,
            target_id=target_id, bucket=bucket, range_key=time_range,
        )
    except events_svc.BadFilter as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/dashboard")
def dashboard(project_id: str | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authz.require_readable(db, user.id, project_id)
    return dash_svc.build(db, project_id=project_id)


@router.get("/roadmap")
def roadmap(project_id: str | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authz.require_readable(db, user.id, project_id)
    return roadmap_svc.list_roadmap(db, project_id=project_id)


@router.get("/links")
def links(project_id: str | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authz.require_readable(db, user.id, project_id)
    rows = links_svc.list_links(db, project_id=project_id)
    return [
        {"id": l.id, "a": _ref_key(db, l.a), "b": _ref_key(db, l.b), "type": l.type,
         "confidence": l.confidence, "reason": l.reason}
        for l in rows
    ]


@router.get("/mcp/tools")
def mcp_tools(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    counts = mcp_stats.counts(db)
    tools = []
    for t in TOOLS:
        props = t["inputSchema"].get("properties", {})
        required = set(t["inputSchema"].get("required", []))
        param_details = []
        for pname, pspec in props.items():
            param_details.append({
                "name": pname,
                "type": pspec.get("type", "string"),
                "description": pspec.get("description", ""),
                "required": pname in required,
                "enum": pspec.get("enum"),
            })
        tools.append({
            "name": t["name"],
            "description": t["description"],
            "params": list(props.keys()),
            "param_details": param_details,
            "calls": counts.get(t["name"], 0),
            "status": "live",
        })
    return {"live": len(TOOLS), "tools": tools}
