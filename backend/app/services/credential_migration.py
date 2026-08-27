"""Every existing key becomes a row (PRD-25 S6, GRPH-512).

**Dedupe on CONTENT, not on provider kind.** That is the whole migration, and it is why there
is no collision to adjudicate. Two entries agreeing on kind, endpoint and key are the SAME
credential and collapse to one row with both projects pointing at it. Two that disagree are two
credentials. Nothing merges that should not; nothing is discarded.

**Two projects sharing a key but wanting different models is not a conflict.** They point at
one credential and the one whose model differs gets a `model_override`. The credential's own
`model` is the most common among the sharers, so the fewest overrides are created.

**The tie-breaker is deterministic** (grill): most common model, then the lexicographically
smallest. "Most common" alone is not a function — two models at equal frequency would make the
output depend on row order, which is how a migration stops being reproducible.

**`base_url` is normalised before comparison**: scheme and host lowercased, trailing slashes
stripped, default ports dropped. Path case is PRESERVED, because paths are case-sensitive and
folding them would merge two genuinely different endpoints into one credential.

**Migrated credentials start `pending_validation`** with a full retry budget rather than being
assumed valid. A key that worked when it was saved may not work now, and this has no evidence
either way.

**AMENDED BY THE GRILL: the legacy blob is NOT deleted here.** The slice originally specified
that rows, pointers, overrides and blob deletion commit together or not at all. Deleting the
only copy of the old configuration in the same breath as writing the new one is what makes a
bad migration unrecoverable. Rows, pointers and overrides still commit atomically; the blob is
left as a read-only vestige that nothing consults once S6 removes resolution step 0. A later
migration removes it, once the new path has actually served traffic.

**A malformed entry is SKIPPED and marked, never an abort** (grill). All-or-nothing would mean
one bad blob blocks the upgrade entirely, leaving the operator with no working deployment and
no way in to fix the row that caused it.
"""
from __future__ import annotations

import logging
from collections import Counter
from secrets import token_urlsafe
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy.orm import Session

from app.models import Credential, DeploymentConfig, PlatformConfig, Project
from app.security import secrets

logger = logging.getLogger("graphban.credential_migration")

#: Ports that carry no information for their scheme, so `https://h:443` and `https://h` are the
#: same endpoint and must not become two credentials.
DEFAULT_PORTS = {"https": 443, "http": 80}


def normalise_url(raw: str) -> str:
    """Canonical form for comparison. Scheme and host case-folded, default port and trailing
    slashes dropped — **path case preserved**, because paths are case-sensitive and folding
    them would merge two genuinely different endpoints."""
    if not raw:
        return ""
    parts = urlsplit(raw.strip())
    if not parts.scheme:
        return raw.strip().rstrip("/")
    host = (parts.hostname or "").lower()
    scheme = parts.scheme.lower()
    if parts.port and parts.port != DEFAULT_PORTS.get(scheme):
        host = f"{host}:{parts.port}"
    path = parts.path.rstrip("/")
    return urlunsplit((scheme, host, path, parts.query, ""))


def _identity(kind: str, base_url: str, api_key: str) -> tuple[str, str, str]:
    """What makes two entries the same credential. Deliberately NOT the model — that is the
    thing two projects are allowed to disagree about while sharing a key."""
    return (kind, normalise_url(base_url), api_key)


def choose_model(models: list[str]) -> str:
    """Most common, then lexicographically smallest.

    The tie-break is not decoration: with two models at equal frequency, "most common" alone
    depends on iteration order, and a migration whose output depends on row order cannot be
    reproduced or reasoned about.
    """
    present = [m for m in models if m]
    if not present:
        return ""
    counts = Counter(present)
    top = max(counts.values())
    return sorted(m for m, n in counts.items() if n == top)[0]


def plan_migration(entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """Group raw blob entries into credentials. Returns (credentials, skipped).

    Pure: no database, so the grouping rules are testable without fixtures and the runner
    below has nothing to decide.
    """
    groups: dict[tuple[str, str, str], dict] = {}
    skipped: list[dict] = []

    for entry in entries:
        kind = (entry.get("kind") or "").strip()
        if not kind:
            # A blob whose key is not a provider id at all. Skipped and reported rather than
            # aborting the run — see the module docstring.
            skipped.append({**entry, "why": "no provider kind"})
            continue
        key = _identity(kind, entry.get("base_url") or "", entry.get("api_key") or "")
        group = groups.setdefault(key, {
            "kind": kind,
            "base_url": entry.get("base_url") or "",
            "api_key": entry.get("api_key") or "",
            "projects": [],
            "models": [],
        })
        group["projects"].append(entry["project_id"])
        group["models"].append(entry.get("model") or "")

    out = []
    for group in groups.values():
        model = choose_model(group["models"])
        out.append({
            **group,
            "model": model,
            # Only the projects whose model DIFFERS need an override. Choosing the most common
            # model above is what keeps this list as short as possible.
            "overrides": {
                pid: m for pid, m in zip(group["projects"], group["models"])
                if m and m != model
            },
        })
    return out, skipped


def _label_for(kind: str, seen: Counter) -> str:
    """Kind, plus a disambiguator only when the kind already exists.

    Same-kind-different-key is the only case needing one — identical entries have already
    collapsed — so the common case gets a clean name.
    """
    seen[kind] += 1
    return kind if seen[kind] == 1 else f"{kind} ({seen[kind]})"


def collect_entries(db: Session) -> list[dict]:
    """Every configured provider entry across every project's legacy blob."""
    entries: list[dict] = []
    for cfg in db.query(PlatformConfig).all():
        for kind, conf in (cfg.providers or {}).items():
            if not isinstance(conf, dict):
                entries.append({"project_id": cfg.project_id, "kind": kind, "malformed": True})
                continue
            # An entry with neither a key nor an endpoint was never usable; it is not a
            # credential, it is a blank row the old table let you save.
            if not conf.get("api_key") and not conf.get("base_url"):
                continue
            entries.append({
                "project_id": cfg.project_id,
                "kind": kind,
                "base_url": conf.get("base_url") or "",
                "api_key": conf.get("api_key") or "",
                "model": conf.get("chat_model") or "",
            })
    return entries


def migrate(db: Session, scope: str = "") -> dict:
    """Run it. Rows, pointers and overrides commit together; the blob is left alone.

    Idempotent by construction: a project that already has a `credential_id` is not migrated
    again, so a re-run after a partial failure reconciles rather than duplicating.
    """
    entries = [e for e in collect_entries(db) if not e.get("malformed")]
    malformed = [e for e in collect_entries(db) if e.get("malformed")]

    already = {
        p.id for p in db.query(Project).filter(Project.credential_id.isnot(None)).all()
    }
    entries = [e for e in entries if e["project_id"] not in already]

    credentials, skipped = plan_migration(entries)
    skipped += [{**e, "why": "malformed entry"} for e in malformed]

    labels: Counter = Counter()
    created: list[Credential] = []
    for group in credentials:
        cred = Credential(
            id=f"cred_{token_urlsafe(8)}",
            org_id=scope or None,
            kind=group["kind"],
            label=_label_for(group["kind"], labels),
            base_url=group["base_url"],
            api_key=group["api_key"],   # already encrypted in the blob; carried across as-is
            model=group["model"],
            # No evidence either way that a key saved months ago still works.
            state="pending_validation",
        )
        db.add(cred)
        created.append(cred)

        for pid in group["projects"]:
            project = db.get(Project, pid)
            if project is None:
                continue
            project.credential_id = cred.id
            override = group["overrides"].get(pid)
            if override:
                project.model_override = override

    default_id, referenced = "", 0
    if created:
        counts = {c.id: len(g["projects"]) for c, g in zip(created, credentials)}
        default_id = max(counts, key=lambda k: (counts[k], k))
        referenced = counts[default_id]
        row = db.get(DeploymentConfig, scope or "")
        if row is None:
            row = DeploymentConfig(scope=scope or "")
            db.add(row)
        if row.default_credential_id is None:
            row.default_credential_id = default_id

    db.commit()

    report = {
        "credentials_created": len(created),
        "projects_pointed": sum(len(g["projects"]) for g in credentials),
        "overrides": sum(len(g["overrides"]) for g in credentials),
        # A heuristic, and on a deployment where one project matters more than five others it
        # picks wrong — so it is REPORTED with the count that decided it, rather than silent.
        # There is no rollback mechanism on purpose: changing the default is an ordinary edit
        # in the credentials view, and a migration-shaped escape hatch would imply otherwise.
        "default_credential_id": default_id,
        "default_chosen_because": f"referenced by {referenced} project(s)" if default_id else "",
        "skipped": skipped,
    }
    logger.info("credential migration: %s", report)
    return report
