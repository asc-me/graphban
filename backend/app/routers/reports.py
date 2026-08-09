"""Report an issue with Graphban — the in-app (authenticated) side of the upstream
feedback channel. Forwards a user-initiated report to the maintainer's intake."""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException

from app.models import User
from app.schemas import UpstreamReportIn
from app.security.deps import get_current_user
from app.services import upstream as up_svc

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/upstream")
def upstream_config(_: User = Depends(get_current_user)):
    """Whether upstream reporting is on and where reports go — the UI uses this to show/hide
    the action and to tell the user (transparently) where their report is sent."""
    return {"enabled": up_svc.report_enabled(), "target": up_svc.target_host()}


@router.post("/upstream")
def upstream_report(body: UpstreamReportIn, _: User = Depends(get_current_user)):
    try:
        result = up_svc.submit_upstream(
            type_=body.type, title=body.title, detail=body.detail, source="in-app"
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    # HTTPStatusError subclasses HTTPError, so it has to come first — otherwise a
    # permanent 4xx reads as a transient gateway failure.
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        detail = f"upstream rejected the report (HTTP {code})"
        if code == 404:
            detail += (
                " — a hosted intake honours only the share token; set "
                "UPSTREAM_FEEDBACK_TOKEN. A project without public sharing enabled "
                "returns the same 404 by design."
            )
        # 502 says the upstream is at fault. A 4xx means ours is: our own config is wrong.
        raise HTTPException(502 if code >= 500 else 500, detail)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"upstream unreachable: {e}")
    req = result.get("request", {})
    return {"ok": True, "request_id": req.get("id"), "duplicates": result.get("duplicates", [])}
