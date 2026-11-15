"""The box's HTTPS surface (GRPH-1634, P83 D2): the settings-page read, the CA download, the
operator name list, and the `ask` endpoint Caddy's on-demand TLS calls. No route returns a
private key."""
from __future__ import annotations

import ipaddress

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import User
from app.security.deps import get_current_user
from app.services import tls_ca, tls_names

router = APIRouter(prefix="/https", tags=["https"])


def _operator(user: User = Depends(get_current_user)) -> User:
    if settings.hosted_mode:
        raise HTTPException(403, "This is a hosted instance: its operator manages certificates.")
    return user


class NameIn(BaseModel):
    name: str


@router.get("")
def https_status(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return tls_ca.status(db)


@router.get("/ca.crt")
def ca_certificate(db: Session = Depends(get_db)):
    """The public CA certificate, for installing on a device. Public by nature."""
    pem = tls_ca.ca_certificate_pem(db)
    if pem is None:
        raise HTTPException(404, "the box certificate authority is not ready")
    return Response(pem, media_type="application/x-x509-ca-cert",
                    headers={"Content-Disposition": 'attachment; filename="graphban-box-ca.crt"'})


@router.post("/names")
def add_name(body: NameIn, db: Session = Depends(get_db), user: User = Depends(_operator)):
    try:
        tls_names.add_operator(db, body.name)
    except tls_names.NameRefused as e:
        raise HTTPException(422, str(e))
    tls_ca.sync_leaf(db)
    return tls_ca.status(db)


@router.delete("/names/{name}")
def remove_name(name: str, db: Session = Depends(get_db), user: User = Depends(_operator)):
    try:
        if not tls_names.remove_operator(db, name):
            raise HTTPException(404, "no such name")
    except tls_names.NameRefused as e:
        raise HTTPException(422, str(e))
    tls_ca.sync_leaf(db)
    return tls_ca.status(db)


def _internal(request: Request) -> bool:
    """Caddy calls this over the compose network. Refuse anything that is not from a private
    address, or that arrived through a proxy (X-Forwarded-For)."""
    if request.headers.get("x-forwarded-for"):
        return False
    host = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host == "testclient"
    return ip.is_loopback or ip.is_private


@router.get("/ask")
def ask(request: Request, domain: str = "", db: Session = Depends(get_db)):
    """Caddy on-demand TLS: 200 when `domain` is on the allowlist, 404 otherwise."""
    if not _internal(request):
        raise HTTPException(403, "internal only")
    if not tls_names.allowed(db, domain):
        raise HTTPException(404, "name not on the allowlist")
    return {"allowed": True}
