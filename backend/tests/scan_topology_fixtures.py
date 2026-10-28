"""Shared app-topology rows for scan-board tests (GRPH-1124)."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import AppTopology

_PROV = {"file": "compose.yml", "rule": "test.fixture", "line": 1}


def graphban_components() -> list[dict]:
    """Graphban's own layout: four trees plus manifest paths for security cells."""
    return [
        {"id": "web", "dir": "web", "label": "Web", "provenance": dict(_PROV)},
        {"id": "api", "dir": "backend", "label": "API", "provenance": dict(_PROV)},
        {"id": "fleet", "dir": "fleet", "label": "Fleet", "provenance": dict(_PROV)},
        {"id": "cli", "dir": "cli", "label": "CLI", "provenance": dict(_PROV)},
        {
            "id": "api-pyproject",
            "dir": "backend/pyproject.toml",
            "label": "backend/pyproject.toml",
            "provenance": {"file": "backend/pyproject.toml", "rule": "project", "line": 1},
        },
        {
            "id": "web-lock",
            "dir": "web/package-lock.json",
            "label": "web/package-lock.json",
            "provenance": {"file": "web/package-lock.json", "rule": "lockfile", "line": 1},
        },
    ]


def seed_core_topology(db: Session, *, mapped_sha: str = "mapped0001") -> None:
    row = db.get(AppTopology, "core")
    payload = {
        "producer_id": "pytest",
        "producer_version": "0",
        "mapped_sha": mapped_sha,
        "mapped_at": datetime.now(timezone.utc),
        "previous_mapped_sha": "",
        "posted_by": "test",
        "components": graphban_components(),
        "infrastructure": [],
        "external_services": [],
        "unresolved": [],
        "edges": [],
    }
    if row is None:
        db.add(AppTopology(project_id="core", **payload))
    else:
        for key, val in payload.items():
            setattr(row, key, val)
    db.commit()
