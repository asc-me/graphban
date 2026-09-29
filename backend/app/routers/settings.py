"""Deployment settings — the "This box" surfaces with no project to be scoped by.

Log export is the only one here today (PRD-47 S15 / GRPH-966). It gets a router of its own
rather than a block in `platform.py` because everything in `platform.py` resolves through a
project the authz layer has already vetted, and this record has no project: one collector
endpoint, one exporter, one queue for the whole instance.

**Gated the way the other "This box" panels are** — `get_current_user`, a logged-in operator,
never an API key. That is `/platform/update-check` and `/platform/update-apply`'s gate, and it
is the only one available for a fact with no project to check a membership against. Nothing new
is invented here, which is what the item asks for.

Hosted 403s on every route, including the read: on a hosted box the operator runs the instance,
and a tenant changing where the whole box's telemetry goes is a cross-tenant write with extra
steps. The web UI redirects the panel away in hosted mode rather than showing a form that can
only fail.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import User
from app.security.deps import get_current_user
from app.services import events as events_svc
from app.services import logexport

router = APIRouter(prefix="/settings", tags=["settings"])

HOSTED_REFUSED = ("This is a hosted instance: its operator configures log export, not a tenant "
                  "of it.")


def _operator(user: User = Depends(get_current_user)) -> User:
    """A logged-in operator on a self-host box. The gate the other "This box" panels use."""
    if settings.hosted_mode:
        raise HTTPException(403, HOSTED_REFUSED)
    return user


class LogExportPatch(BaseModel):
    """The panel's save. Every field optional; an omitted one is left alone.

    There is no `project_id` and no cursor field. The first because the config is the
    deployment's, the second because a form save must not be able to move the export position.
    """

    enabled: bool | None = None
    endpoint: str | None = None
    protocol: str | None = None
    compression: str | None = None
    headers: dict[str, str] | None = None
    send_events: bool | None = None
    send_tool_calls: bool | None = None
    send_heartbeats: bool | None = None
    event_types: list[str] | None = None
    redact_summaries: bool | None = None
    redact_client_ips: bool | None = None
    mask_api_keys: bool | None = None


class TestBatchIn(BaseModel):
    """Overrides for `Send test batch`, so an endpoint can be probed before it is saved.

    An omitted field uses the stored config — pressing the button tests what is configured, and
    typing into the field tests what you typed.
    """

    endpoint: str | None = None
    protocol: str | None = None
    compression: str | None = None
    headers: dict[str, str] | None = None


@router.get("/log-export")
def get_log_export(db: Session = Depends(get_db), user: User = Depends(_operator)):
    """The whole panel in one response: config, status strip, live sample, and the choices.

    One response rather than a config read beside a counters read, so the panel cannot show a
    config from one moment next to numbers from another.
    """
    del user  # the gate is the dependency; nothing here is per-caller
    return logexport.view(db)


@router.patch("/log-export")
def update_log_export(body: LogExportPatch, db: Session = Depends(get_db),
                      user: User = Depends(_operator)):
    """Save the deployment's export config. 422 on a value this deployment does not define —
    the config is wrong, not the server (GRPH-485's rule)."""
    patch = body.model_dump(exclude_unset=True)
    try:
        _cfg, notes = logexport.update_config(db, **patch)
    except logexport.BadConfig as e:
        raise HTTPException(422, str(e)) from None
    events_svc.record_user(db, user, action="update_log_export", target_type="deployment",
                           target_id=logexport.SINGLETON_ID,
                           meta={"fields": sorted(patch.keys())})
    db.commit()
    return {**logexport.view(db), "notes": notes}


@router.post("/log-export/test-batch")
def test_log_export_batch(body: TestBatchIn, db: Session = Depends(get_db),
                          user: User = Depends(_operator)):
    """`Send test batch`.

    Always 200 with a result object — a failure to reach the collector is the ANSWER, not an
    error in this API. `ok` is true only when the collector accepted records, and `error` names
    which failure it was, so a portless endpoint says so instead of arriving as a generic
    connection error. Nothing about an unrun probe resembles a passed one: the panel starts at
    "not run" and only this call moves it.
    """
    out = logexport.send_test_batch(
        db, endpoint=body.endpoint, protocol=body.protocol,
        compression=body.compression, headers=body.headers)
    events_svc.record_user(db, user, action="log_export_test_batch",
                           target_type="deployment", target_id=logexport.SINGLETON_ID,
                           meta={"ok": out["ok"], "error": out.get("error") or ""})
    db.commit()
    return out


@router.get("/log-export/sample")
def get_log_export_sample(redact_summaries: bool | None = None,
                          redact_client_ips: bool | None = None,
                          mask_api_keys: bool | None = None,
                          db: Session = Depends(get_db),
                          user: User = Depends(_operator)):
    """The sample record under redaction choices the caller has not saved yet.

    A separate call rather than a client-side replay of the rules: the redaction logic has one
    owner, and a panel that re-implemented it to look "live" would be free to disagree with what
    the exporter actually does — which is the whole thing the sample is there to show.
    """
    del user
    cfg = logexport.get_config(db)
    red = logexport.Redaction.of(cfg, summaries=redact_summaries, client_ips=redact_client_ips,
                                 api_keys=mask_api_keys)
    return {"sample": logexport.sample_record(db, red), "redaction": red.dict()}
