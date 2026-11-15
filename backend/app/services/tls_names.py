"""The name allowlist for the box's certificate (GRPH-1634, P83 D2).

Names fill themselves: the machine hostname and every non-loopback interface address at
startup, and a request `Host` the first time it is seen — but only when it is plainly local
(single label, `.local`/`.lan`/`.home.arpa`/`.internal`, a private address, or a name that
resolves to one of the box's own addresses). Anything else is added only by the operator.
Loopback (`localhost`, `127.0.0.1`, `::1`) is never a name. At most `MAX_NAMES`.
"""
from __future__ import annotations

import ipaddress
import re
import socket

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import TlsName

MAX_NAMES = 32
LOCAL_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal")

_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


class NameRefused(ValueError):
    """The name cannot be added; the message says why."""


def normalise(raw: str) -> str:
    """Lower-case host with any port, brackets and trailing dot removed."""
    host = (raw or "").strip().lower()
    if host.startswith("["):  # [::1]:8080
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:  # name:port or v4:port
        host = host.split(":", 1)[0]
    return host.rstrip(".")


def _ip(name: str):
    try:
        return ipaddress.ip_address(name)
    except ValueError:
        return None


def is_loopback(name: str) -> bool:
    ip = _ip(name)
    if ip is not None:
        return ip.is_loopback or ip.is_unspecified
    return name == "localhost" or name.endswith(".localhost")


def is_valid(name: str) -> bool:
    if _ip(name) is not None:
        return True
    return 0 < len(name) <= 253 and all(_LABEL.match(p) for p in name.split("."))


# RFC 1918, link-local and ULA — the ranges a seen Host may be in. Not `ip.is_private`, which
# also counts documentation and benchmarking ranges.
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(r) for r in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "fc00::/7", "fe80::/10"))


def is_private_ip(ip) -> bool:
    return any(ip.version == n.version and ip in n for n in PRIVATE_NETWORKS)


def interface_addresses() -> list[str]:
    """Best-effort: every non-loopback address of this machine."""
    found: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            found.add(info[4][0].split("%", 1)[0])
    except OSError:
        pass
    for family, probe in ((socket.AF_INET, "10.255.255.255"), (socket.AF_INET6, "2001:db8::1")):
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as s:
                s.connect((probe, 9))
                found.add(s.getsockname()[0].split("%", 1)[0])
        except OSError:
            pass
    return sorted(a for a in found if _ip(a) is not None and not is_loopback(a))


def resolves_to_box(name: str) -> bool:
    """True when `name` resolves to one of this box's own addresses."""
    own = set(interface_addresses())
    if not own:
        return False
    try:
        return any(i[4][0].split("%", 1)[0] in own for i in socket.getaddrinfo(name, None))
    except OSError:
        return False


def seen_host_eligible(name: str) -> bool:
    """The rule for adding a name from a request Host."""
    if not name or is_loopback(name) or not is_valid(name):
        return False
    ip = _ip(name)
    if ip is not None:
        return is_private_ip(ip)
    if "." not in name or name.endswith(LOCAL_SUFFIXES):
        return True
    return resolves_to_box(name)


def list_names(db: Session) -> list[TlsName]:
    return list(db.scalars(select(TlsName).order_by(TlsName.created_at, TlsName.name)))


def _add(db: Session, name: str, source: str, origin: str) -> bool:
    if db.get(TlsName, name) is not None:
        return False
    if db.query(TlsName).count() >= MAX_NAMES:
        raise NameRefused(f"the box certificate already covers the maximum of {MAX_NAMES} names; "
                          "remove an operator name first")
    db.add(TlsName(name=name, source=source, origin=origin))
    try:
        db.commit()
    except IntegrityError:  # another worker added it first
        db.rollback()
        return False
    return True


def seed_auto_names(db: Session) -> int:
    """Startup: the machine hostname and every non-loopback interface address."""
    added = 0
    host = normalise(socket.gethostname())
    candidates = []
    if host and not is_loopback(host) and is_valid(host):
        candidates.append((host, "hostname"))
    candidates += [(a, "network address") for a in interface_addresses()]
    for name, origin in candidates:
        try:
            added += _add(db, name, "auto", origin)
        except NameRefused:
            break
    return added


def add_seen_host(db: Session, raw_host: str) -> bool:
    """A request Host. True when it was newly added; ineligible or full is False, silently."""
    name = normalise(raw_host)
    if not seen_host_eligible(name):
        return False
    try:
        return _add(db, name, "auto", "seen as Host")
    except NameRefused:
        return False


def add_operator(db: Session, raw: str) -> TlsName:
    name = normalise(raw)
    if not name or not is_valid(name):
        raise NameRefused(f"{raw!r} is not a valid host name or address")
    if is_loopback(name):
        raise NameRefused("loopback names (localhost, 127.0.0.1, ::1) are never certified")
    _add(db, name, "operator", "operator")
    return db.get(TlsName, name)


def remove_operator(db: Session, raw: str) -> bool:
    row = db.get(TlsName, normalise(raw))
    if row is None:
        return False
    if row.source != "operator":
        raise NameRefused("automatic names cannot be removed")
    db.delete(row)
    db.commit()
    return True


def allowed(db: Session, raw: str) -> bool:
    """The Caddy `ask` answer: is this name on the allowlist."""
    return db.get(TlsName, normalise(raw)) is not None
