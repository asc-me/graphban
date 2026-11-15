"""The box's own certificate authority (GRPH-1634, P83 D2) — the mkcert model.

On first start the box creates a CA and issues itself a server certificate for the names in
`tls_names`. Both private keys go through `security.secrets.encrypt` (as the VAPID key does) and
no API returns one. Workers starting together race on the fixed row id; the loser re-reads.

Name Constraints are put on the CA as defence in depth: the local-only suffixes, the private
ranges, the box's own interface addresses (whatever range, e.g. Tailscale 100.64/10), the box
hostname and any operator names present at creation. They are fixed at creation, so a name
added later that falls outside them is still served but a constraint-enforcing client would
reject it. Whether Chrome or Safari enforce Name Constraints on a user-installed root is NOT
verified here — do not rely on this as a security boundary.

The leaf is re-issued when the name set changes (at most once per
`TLS_REISSUE_DEBOUNCE_SECONDS`; a change inside the window waits for the next sync) and
before it expires. There is no scheduler: `sync_leaf` runs at startup and whenever a new name
is seen or the status is read.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
import tempfile
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import TlsAuthority
from app.security.secrets import decrypt, encrypt
from app.services import tls_names

log = logging.getLogger(__name__)

_ROW_ID = "box"
CA_DAYS = 3650
LEAF_DAYS = 90
RENEW_BEFORE = timedelta(days=30)

PRIVATE_RANGES = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
                  "fc00::/7", "fe80::/10")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _pem_key(key) -> str:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def _pem_cert(cert) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Graphban box"),
                      x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def name_constraints(db: Session) -> x509.NameConstraints:
    permitted: list[x509.GeneralName] = [x509.DNSName(s) for s in tls_names.LOCAL_SUFFIXES]
    permitted += [x509.IPAddress(ipaddress.ip_network(r)) for r in PRIVATE_RANGES]
    extra = [r.name for r in tls_names.list_names(db)]
    extra += [a for a in tls_names.interface_addresses()]
    host = tls_names.normalise(socket.gethostname())
    if host and tls_names.is_valid(host):
        extra.append(host)
    seen = set()
    for n in extra:
        if n in seen or tls_names.is_loopback(n):
            continue
        seen.add(n)
        ip = tls_names._ip(n)
        permitted.append(x509.IPAddress(ipaddress.ip_network(ip.compressed + ("/32" if ip.version == 4 else "/128")))
                         if ip is not None else x509.DNSName(n))
    return x509.NameConstraints(permitted_subtrees=permitted, excluded_subtrees=None)


def _generate_ca(db: Session) -> tuple[str, str, datetime]:
    key = ec.generate_private_key(ec.SECP256R1())
    now = _now()
    not_after = now + timedelta(days=CA_DAYS)
    subject = _name("Graphban box CA")
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(not_after)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(name_constraints(db), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    return _pem_cert(cert), _pem_key(key), not_after


def ensure_ca(db: Session) -> TlsAuthority | None:
    """Startup: create the CA once. The loser of a race rolls back and re-reads. Never raises."""
    try:
        row = db.get(TlsAuthority, _ROW_ID)
        if row is None:
            tls_names.seed_auto_names(db)
            cert, key, not_after = _generate_ca(db)
            db.add(TlsAuthority(id=_ROW_ID, ca_cert=cert, ca_key=encrypt(key), ca_not_after=not_after))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
            row = db.get(TlsAuthority, _ROW_ID)
        return row
    except Exception:  # noqa: BLE001 - the box serves without https
        log.error("tls: could not ensure the box certificate authority", exc_info=True)
        db.rollback()
        return None


def _issue_leaf(row: TlsAuthority, names: list[str]) -> tuple[str, str, datetime]:
    ca_key_pem = decrypt(row.ca_key)
    if not ca_key_pem:
        raise RuntimeError("the CA private key cannot be decrypted (SECRET_ENCRYPTION_KEY changed)")
    ca_key = serialization.load_pem_private_key(ca_key_pem.encode(), password=None)
    ca_cert = x509.load_pem_x509_certificate(row.ca_cert.encode())
    key = ec.generate_private_key(ec.SECP256R1())
    now = _now()
    not_after = now + timedelta(days=LEAF_DAYS)
    sans = [x509.IPAddress(tls_names._ip(n)) if tls_names._ip(n) is not None else x509.DNSName(n)
            for n in names]
    cert = (x509.CertificateBuilder().subject_name(_name(names[0])).issuer_name(ca_cert.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(not_after)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                           critical=False)
            .sign(ca_key, hashes.SHA256()))
    return _pem_cert(cert), _pem_key(key), not_after


def _write_atomic(path: str, data: str, mode: int) -> None:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def publish_leaf(row: TlsAuthority) -> bool:
    """Write the current leaf and key where the front end loads them, then ask it to reload."""
    if not settings.tls_dir or not row.leaf_cert:
        return False
    try:
        os.makedirs(settings.tls_dir, exist_ok=True)
        key = decrypt(row.leaf_key)
        if not key:
            return False
        _write_atomic(os.path.join(settings.tls_dir, "box.crt"), row.leaf_cert, 0o644)
        _write_atomic(os.path.join(settings.tls_dir, "box.key"), key, 0o600)
        if settings.tls_reload_url:
            import httpx

            httpx.post(settings.tls_reload_url, timeout=5)
        return True
    except Exception:  # noqa: BLE001 - best effort; the next sync tries again
        log.warning("tls: could not publish the box certificate", exc_info=True)
        return False


def sync_leaf(db: Session, now: datetime | None = None) -> str:
    """Issue or renew the leaf when needed. Returns issued | current | debounced | no-ca |
    no-names | failed."""
    now = now or _now()
    row = ensure_ca(db)
    if row is None:
        return "no-ca"
    names = sorted(n.name for n in tls_names.list_names(db))
    if not names:
        return "no-names"
    covered = json.loads(row.leaf_names or "[]")
    expiring = (row.leaf_not_after is None
                or _aware(row.leaf_not_after) - now < RENEW_BEFORE)
    if row.leaf_cert and covered == names and not expiring:
        return "current"
    issued_at = _aware(row.leaf_issued_at)
    if (row.leaf_cert and not expiring and issued_at is not None
            and now - issued_at < timedelta(seconds=settings.tls_reissue_debounce_seconds)):
        return "debounced"
    try:
        cert, key, not_after = _issue_leaf(row, names)
        row.leaf_cert, row.leaf_key, row.leaf_names = cert, encrypt(key), json.dumps(names)
        row.leaf_not_after, row.leaf_issued_at = not_after, now
        db.commit()
    except Exception:  # noqa: BLE001
        log.error("tls: could not issue the box certificate", exc_info=True)
        db.rollback()
        return "failed"
    publish_leaf(row)
    return "issued"


def see_host(db: Session, raw_host: str) -> bool:
    """A request Host: add it if eligible, and re-issue if the set grew and the window allows."""
    added = tls_names.add_seen_host(db, raw_host)
    if added:
        sync_leaf(db)
    return added


def status(db: Session) -> dict:
    """The settings-page read. Public material only."""
    sync_leaf(db)
    row = db.get(TlsAuthority, _ROW_ID)
    return {
        "tier": "box-ca",
        "ready": bool(row and row.leaf_cert),
        "names": [{"name": n.name, "source": n.source, "origin": n.origin}
                  for n in tls_names.list_names(db)],
        "max_names": tls_names.MAX_NAMES,
        "covered_names": json.loads(row.leaf_names) if row else [],
        "leaf_not_after": _aware(row.leaf_not_after).isoformat() if row and row.leaf_not_after else None,
        "ca_not_after": _aware(row.ca_not_after).isoformat() if row else None,
        "ca_certificate_path": "/api/https/ca.crt",
    }


def ca_certificate_pem(db: Session) -> str | None:
    row = db.get(TlsAuthority, _ROW_ID)
    return row.ca_cert if row else None
