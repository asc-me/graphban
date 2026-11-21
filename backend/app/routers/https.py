"""The box's HTTPS surface (GRPH-1634, P83 D2): the settings-page read, the CA download, the
operator name list, and the `ask` endpoint Caddy's on-demand TLS calls. No route returns a
private key."""
from __future__ import annotations

import ipaddress

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import User
from app.security.deps import get_current_user
from app.services import tls_byo, tls_ca, tls_names

router = APIRouter(prefix="/https", tags=["https"])


def _operator(user: User = Depends(get_current_user)) -> User:
    if settings.hosted_mode:
        raise HTTPException(404, "the box certificate is a self-hosted feature; a hosted "
                                 "deployment's operator manages its certificates")
    return user


class NameIn(BaseModel):
    name: str


class UploadIn(BaseModel):
    certificate: str
    key: str
    box_address: str


class LetsEncryptIn(BaseModel):
    domain: str


class RedirectIn(BaseModel):
    enabled: bool


def _refused(e: tls_byo.CertificateRefused) -> HTTPException:
    return HTTPException(422, {"message": str(e), "reasons": e.reasons})


@router.post("/certificate")
def upload_certificate(body: UploadIn, db: Session = Depends(get_db), user: User = Depends(_operator)):
    try:
        tls_byo.upload(db, body.certificate, body.key, body.box_address)
    except tls_byo.CertificateRefused as e:
        raise _refused(e)
    return tls_ca.status(db)


@router.post("/letsencrypt")
def request_letsencrypt(body: LetsEncryptIn, db: Session = Depends(get_db), user: User = Depends(_operator)):
    try:
        tls_byo.request_letsencrypt(db, body.domain)
    except tls_byo.CertificateRefused as e:
        raise _refused(e)
    return tls_ca.status(db)


@router.delete("/certificate")
def stop_using(db: Session = Depends(get_db), user: User = Depends(_operator)):
    """Stop using the uploaded or Let's Encrypt certificate: files and key removed."""
    if not tls_byo.stop(db):
        raise HTTPException(404, "no uploaded or Let's Encrypt certificate to stop using")
    return tls_ca.status(db)


@router.put("/redirect")
def set_redirect(body: RedirectIn, db: Session = Depends(get_db), user: User = Depends(_operator)):
    try:
        tls_byo.set_redirect(db, body.enabled)
    except tls_byo.CertificateRefused as e:
        # No uploaded or Let's Encrypt certificate to redirect to: the same unprocessable
        # request as the other certificate checks here, not a fleet-policy refusal.
        raise _refused(e)
    return tls_ca.status(db)


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
    if not _internal(request):  # 404, not 403: from outside the endpoint does not exist
        raise HTTPException(404, "Not Found")
    if not tls_names.allowed(db, domain):
        raise HTTPException(404, "name not on the allowlist")
    return {"allowed": True}


@router.get("/redirect-check")
def redirect_check(request: Request, db: Session = Depends(get_db)):
    """Caddy's `forward_auth` for plain http: 204 serves the request; a 307 redirects it. The
    target is the stored box address — never the Host the caller sent. Not gated like `ask`:
    Caddy's reverse proxy adds X-Forwarded-For, and the answer (serve, or go to the box
    address) is nothing a caller could not see by opening the page."""
    target = tls_byo.redirect_target(db, request.headers.get("x-forwarded-uri") or "/")
    if target is None:
        return Response(status_code=204)
    return RedirectResponse(target, status_code=307)
