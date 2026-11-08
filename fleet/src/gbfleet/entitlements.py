"""The accounts this machine can launch (GRPH-P72, "gbfleet registers the accounts").

A local file, outside every worktree, holding per record exactly four fields: a local
name, the vendor, the kind (`subscription`, `api_key`, `local`) and a launch handle. There
is no secret field and no scanner that guesses whether a string looks like a token: a
payload with any other field is refused and the file is left as it was. For an `api_key`
the handle is the NAME of an environment variable; its value is never read here.

`gban` does not read or write this file, and `~/.graphban/session.json` and
`supervisor.json` are not touched.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

KINDS = ("subscription", "api_key", "local")
FIELDS = ("name", "vendor", "kind", "handle")
#: Overrides the location; mainly for tests and for a machine with a different home.
PATH_ENV = "GBFLEET_ENTITLEMENTS"
FILENAME = "gbfleet-entitlements.json"


class EntitlementError(ValueError):
    """A record refused, or a name that does not exist. The file is unchanged."""


@dataclass(frozen=True)
class Entitlement:
    name: str
    vendor: str
    kind: str
    handle: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "vendor": self.vendor, "kind": self.kind, "handle": self.handle}


def default_path() -> Path:
    override = os.environ.get(PATH_ENV)
    return Path(override) if override else Path.home() / ".graphban" / FILENAME


def parse(payload: object) -> Entitlement:
    """Validate one record. Extra fields are refused by name; the handle is never judged."""
    if not isinstance(payload, dict):
        raise EntitlementError("an entitlement is a JSON object with name, vendor, kind, handle")
    extra = sorted(set(payload) - set(FIELDS))
    if extra:
        raise EntitlementError(
            f"refused: field {', '.join(repr(f) for f in extra)} is not one of "
            f"{', '.join(FIELDS)}. The file holds no other field."
        )
    for key in ("name", "vendor", "kind"):
        if not isinstance(payload.get(key), str) or not payload[key]:
            raise EntitlementError(f"refused: {key!r} is required")
    handle = payload.get("handle", "")
    if not isinstance(handle, str):
        raise EntitlementError("refused: 'handle' must be a string")
    if payload["kind"] not in KINDS:
        raise EntitlementError(f"refused: kind {payload['kind']!r} is not one of {', '.join(KINDS)}")
    if payload["kind"] == "local" and handle:
        raise EntitlementError("refused: a local entitlement has an empty handle")
    if payload["kind"] != "local" and not handle:
        raise EntitlementError(f"refused: a {payload['kind']} entitlement needs a handle")
    return Entitlement(payload["name"], payload["vendor"], payload["kind"], handle)


def load(path: Path | None = None) -> list[Entitlement]:
    path = path or default_path()
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [parse(rec) for rec in data]


def _save(records: list[Entitlement], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".ent-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump([r.as_dict() for r in records], fh, indent=2)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def add(record: Entitlement, path: Path | None = None) -> None:
    path = path or default_path()
    records = load(path)
    if any(r.name == record.name for r in records):
        raise EntitlementError(f"refused: an entitlement named {record.name!r} already exists")
    _save([*records, record], path)


def remove(name: str, path: Path | None = None) -> None:
    path = path or default_path()
    records = load(path)
    kept = [r for r in records if r.name != name]
    if len(kept) == len(records):
        raise EntitlementError(f"no entitlement named {name!r}")
    _save(kept, path)


def find(name: str, path: Path | None = None) -> Entitlement:
    for r in load(path):
        if r.name == name:
            return r
    raise EntitlementError(f"no entitlement named {name!r}")


def foreign_variables(selected: Entitlement, records: list[Entitlement]) -> tuple[str, ...]:
    """The API-key variable names held by records OTHER than `selected`.

    A launch on `selected` strips these from the child's environment: a child started on
    one record does not receive another record's key. The selected record's own variable is
    never in the list, even when another record names the same one.
    """
    own = selected.handle if selected.kind == "api_key" else None
    return tuple(sorted({r.handle for r in records
                         if r.kind == "api_key" and r.name != selected.name
                         and r.handle and r.handle != own}))


def format_list(records: list[Entitlement]) -> str:
    """Name, vendor, kind and handle. Never the environment."""
    if not records:
        return "no entitlements registered"
    return "\n".join(f"{r.name}\t{r.vendor}\t{r.kind}\t{r.handle}" for r in records)
