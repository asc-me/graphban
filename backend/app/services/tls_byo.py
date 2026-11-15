"""Tier 2 and 3 of the box's HTTPS (GRPH-1637, P83 D3): bring your own certificate, or ask
Let's Encrypt through Caddy's ACME, plus the box address and the http->https redirect switch.

Upload: exactly three things refuse a certificate, each reported on its own — the key does not
match the certificate, the certificate does not cover the box address, the certificate has
expired. Everything else is a warning (the address not resolving to this box — DNS may lag; a
wildcard). The uploaded key is stored through `security.secrets.encrypt` and no API returns it.
It is written to the shared TLS directory as `box.crt`/`box.key` and the front end reloads.

Expiry: 30 days or fewer left is `expiring-soon`; at expiry the box goes back to its own
certificate, the redirect turns off, and the status says why. Stop-using removes the row (so the
key) and the published files and does the same. Let's Encrypt has no expiring-soon state: Caddy
renews it.

Redirect: available (and on by default) only while a tier 2/3 certificate is active. The target
is the stored box address, never the request's Host.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
import ssl
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import NameOID
from sqlalchemy.orm import Session

from app.config import settings
from app.models import TlsCustom
from app.security.secrets import decrypt, encrypt
from app.services import tls_ca, tls_names

log = logging.getLogger(__name__)

_ROW_ID = "box"
EXPIRING_SOON = timedelta(days=30)
LE_PENDING_TIMEOUT = timedelta(minutes=10)
ACME_SNIPPET = os.path.join("acme", "le.caddy")


class CertificateRefused(ValueError):
    """The upload cannot be used. `reasons` is a list of {code, message}, one per cause."""

    def __init__(self, reasons: list[dict]):
        super().__init__("; ".join(r["message"] for r in reasons))
        self.reasons = reasons


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def _row(db: Session) -> TlsCustom | None:
    return db.get(TlsCustom, _ROW_ID)


def _san_names(cert: x509.Certificate) -> list[str]:
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return []
    out = [d.lower() for d in san.get_values_for_type(x509.DNSName)]
    out += [str(i) for i in san.get_values_for_type(x509.IPAddress)]
    return out


def _covering(names: list[str], address: str) -> str | None:
    """The certificate name that covers `address`, or None. A wildcard covers one label."""
    for n in names:
        if n == address:
            return n
        if n.startswith("*.") and "." in address and tls_names._ip(address) is None:
            if address.split(".", 1)[1] == n[2:]:
                return n
    return None


def inspect(chain_pem: str, key_pem: str, box_address: str, now: datetime | None = None) -> dict:
    """Check an upload. Returns {subject, not_after, warnings}; raises CertificateRefused."""
    now = now or _now()
    address = tls_names.normalise(box_address)
    reasons: list[dict] = []
    if not address or not tls_names.is_valid(address) or "*" in address:
        raise CertificateRefused([{"code": "bad_address", "message": f"{box_address!r} is not a valid box address"}])
    try:
        certs = x509.load_pem_x509_certificates(chain_pem.encode())
        if not certs:
            raise ValueError
    except ValueError:
        raise CertificateRefused([{"code": "bad_certificate", "message": "the certificate file is not a PEM certificate"}])
    try:
        key = serialization.load_pem_private_key(key_pem.encode(), password=None)
    except (ValueError, TypeError):
        raise CertificateRefused([{"code": "bad_key", "message": "the key file is not an unencrypted PEM private key"}])
    leaf = certs[0]
    pub = lambda k: k.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    if pub(leaf.public_key()) != pub(key.public_key()):
        reasons.append({"code": "key_mismatch", "message": "the key does not match the certificate"})
    names = _san_names(leaf)
    hit = _covering(names, address)
    if hit is None:
        reasons.append({"code": "address_not_covered",
                        "message": f"the certificate does not cover {address}"})
    not_after = _aware(leaf.not_valid_after_utc)
    if not_after <= now:
        reasons.append({"code": "expired",
                        "message": f"the certificate expired on {not_after.date().isoformat()}"})
    if reasons:
        raise CertificateRefused(reasons)
    warnings = []
    if hit and hit.startswith("*."):
        warnings.append("wildcard")
    if not tls_names.resolves_to_box(address):
        warnings.append("address_not_here")
    try:
        subject = leaf.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    except IndexError:
        subject = names[0] if names else ""
    return {"subject": subject, "not_after": not_after, "warnings": warnings}


def _clear_files() -> None:
    if not settings.tls_dir:
        return
    path = os.path.join(settings.tls_dir, ACME_SNIPPET)
    if os.path.exists(path):
        os.unlink(path)


def publish(db: Session, own_row=None) -> bool:
    """Write whichever certificate is current where the front end loads it."""
    row = _row(db)
    if row is not None and row.kind == "upload" and row.state == "active":
        return _publish_upload(row)
    own = own_row or tls_ca.ensure_ca(db)
    return tls_ca.publish_leaf(own) if own is not None else False


def _publish_upload(row: TlsCustom) -> bool:
    if not settings.tls_dir:
        return False
    key = decrypt(row.cert_key)
    if not key:
        return False
    try:
        os.makedirs(settings.tls_dir, exist_ok=True)
        tls_ca._write_atomic(os.path.join(settings.tls_dir, "box.crt"), row.cert_chain, 0o644)
        tls_ca._write_atomic(os.path.join(settings.tls_dir, "box.key"), key, 0o600)
        tls_ca.reload_front_end()
        return True
    except Exception:  # noqa: BLE001
        log.warning("tls: could not publish the uploaded certificate", exc_info=True)
        return False


def upload(db: Session, chain_pem: str, key_pem: str, box_address: str,
           now: datetime | None = None) -> dict:
    info = inspect(chain_pem, key_pem, box_address, now)
    row = _row(db)
    if row is None:
        row = TlsCustom(id=_ROW_ID, kind="upload", state="active", box_address="")
        db.add(row)
    row.kind, row.state, row.reason = "upload", "active", ""
    row.box_address = tls_names.normalise(box_address)
    row.cert_chain, row.cert_key = chain_pem, encrypt(key_pem)
    row.subject, row.not_after = info["subject"], info["not_after"]
    row.warnings = json.dumps(info["warnings"])
    row.redirect_on = True
    db.commit()
    _clear_files()
    publish(db)
    return info


def stop(db: Session) -> bool:
    """Stop using the uploaded or Let's Encrypt certificate: the row (and key) and the files go,
    the box's own certificate is served, the redirect is off with it."""
    row = _row(db)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    _clear_files()
    _back_to_own(db)
    return True


def _back_to_own(db: Session) -> None:
    tls_ca.sync_leaf(db)
    own = tls_ca.ensure_ca(db)
    if own is not None:
        tls_ca.publish_leaf(own)


def check_expiry(db: Session, now: datetime | None = None) -> None:
    """At expiry of an upload the box switches to its own certificate and the redirect turns
    off; the row stays (without its key) so the status can say why."""
    now = now or _now()
    row = _row(db)
    if row is None or row.kind != "upload" or row.state != "active":
        return
    if _aware(row.not_after) > now:
        return
    row.state, row.redirect_on, row.cert_key = "expired", False, ""
    row.reason = f"your certificate expired on {_aware(row.not_after).date().isoformat()}"
    db.commit()
    _back_to_own(db)


# --- Let's Encrypt (Caddy ACME) ---

def _acme_snippet(domain: str) -> str:
    return ("https://%s:{$HTTPS_PORT} {\n\ttls {\n\t\tissuer acme\n\t}\n\timport front\n}\n" % domain)


def request_letsencrypt(db: Session, domain: str, now: datetime | None = None) -> None:
    name = tls_names.normalise(domain)
    if (not name or not tls_names.is_valid(name) or tls_names._ip(name) is not None
            or "." not in name or name.endswith(tls_names.LOCAL_SUFFIXES)):
        raise CertificateRefused([{"code": "bad_address",
                        "message": "Let's Encrypt needs a public domain name, not an address or a local name"}])
    row = _row(db)
    if row is None:
        row = TlsCustom(id=_ROW_ID, kind="letsencrypt", state="pending", box_address="")
        db.add(row)
    row.kind, row.state, row.reason = "letsencrypt", "pending", ""
    row.box_address, row.cert_chain, row.cert_key = name, "", ""
    row.not_after, row.warnings, row.redirect_on = None, "[]", True
    row.created_at = now or _now()
    db.commit()
    if settings.tls_dir:
        path = os.path.join(settings.tls_dir, ACME_SNIPPET)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tls_ca._write_atomic(path, _acme_snippet(name), 0o644)


def _serving_public_cert(domain: str, port: int) -> str | None:
    """None when a publicly trusted certificate is served for `domain`, else the reason."""
    try:
        with socket.create_connection((domain, port), timeout=5) as s:
            with ssl.create_default_context().wrap_socket(s, server_hostname=domain):
                return None
    except ssl.SSLCertVerificationError as e:
        return f"the certificate served is not publicly trusted ({e.verify_message})"
    except OSError as e:
        return f"could not reach {domain}: {e}"


def check_letsencrypt(db: Session, now: datetime | None = None, probe=_serving_public_cert) -> None:
    now = now or _now()
    row = _row(db)
    if row is None or row.kind != "letsencrypt" or row.state not in ("pending", "failed"):
        return
    # The published HTTPS port (WEB_HTTPS_PORT), as the API sees it — not the web container's.
    reason = probe(row.box_address, settings.web_https_port)
    if reason is None:
        row.state, row.reason = "active", ""
        db.commit()
    elif now - _aware(row.created_at) > LE_PENDING_TIMEOUT:
        row.state, row.reason = "failed", reason  # still serving the current certificate
        db.commit()


# --- redirect ---

def tier_active(db: Session) -> bool:
    row = _row(db)
    return row is not None and row.state == "active"


def set_redirect(db: Session, enabled: bool) -> None:
    row = _row(db)
    if row is None or row.state != "active":
        raise CertificateRefused([{"code": "redirect_unavailable",
                        "message": "the redirect needs a public or uploaded certificate; the box's "
                                   "own certificate cannot redirect"}])
    row.redirect_on = enabled
    db.commit()


# Never redirected: agent and CLI configs that point at http:// must keep working (httpx does
# not follow redirects by default). Caddy's redirect snippet exempts them too.
_NO_REDIRECT_PREFIXES = ("/api/", "/health")


def redirect_target(db: Session, uri: str) -> str | None:
    """Where plain http should go, or None to serve it. Built from the stored box address and
    the box's published HTTPS port — nothing listens on 443 unless WEB_HTTPS_PORT says so."""
    uri = uri if uri.startswith("/") else "/" + uri
    path = uri.split("?", 1)[0]
    if path == "/api" or path.startswith(_NO_REDIRECT_PREFIXES):
        return None
    row = _row(db)
    if row is None or row.state != "active" or not row.redirect_on:
        return None
    host = row.box_address
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    port = settings.web_https_port
    authority = host if port == 443 else f"{host}:{port}"
    return f"https://{authority}{uri}"


def custom_status(db: Session, now: datetime | None = None) -> dict | None:
    """The tier 2/3 block of the settings read. Public material only — never a key."""
    now = now or _now()
    check_expiry(db, now)
    check_letsencrypt(db, now)
    row = _row(db)
    if row is None:
        return None
    state = row.state
    na = _aware(row.not_after)
    if row.kind == "upload" and state == "active" and na - now <= EXPIRING_SOON:
        state = "expiring-soon"
    return {
        "kind": row.kind, "state": state, "box_address": row.box_address,
        "subject": row.subject, "not_after": na.isoformat() if na else None,
        "warnings": json.loads(row.warnings or "[]"), "reason": row.reason,
        "redirect_on": bool(row.redirect_on),
    }
