"""`GET /api/items` returns lean rows by default (GRPH-1228).

On a 1,100-item project the full list was 8.2 MB on every page, nearly all of it evidence,
descriptions and bounce reasons that no list view draws. `?fields=full` and the per-item
read keep them.
"""
from __future__ import annotations

import json

import pytest

from app.schemas import ITEM_HEAVY_FIELDS

BIG = "x" * 5000

# The pinned lean field set. Adding a field to `ItemOut` must be a decision about whether the
# list should carry it: a heavy one belongs in ITEM_HEAVY_FIELDS, a light one goes here.
LEAN_FIELDS = {
    "id", "project_id", "title", "status", "tags", "touchpoints", "effort", "sort_order",
    "blocker", "date", "reporter", "pr", "github_url", "assignee", "claimed_by", "prd_id",
    "prd_section", "reach", "fidelity", "created_at", "updated_at", "done_at", "decision",
    "snoozed_until", "has_attestation",
}


@pytest.fixture()
def seeded(client):
    from app.db import SessionLocal
    from app.models import Item
    from app.services import items as items_svc

    db = SessionLocal()
    try:
        item = items_svc.create_item(
            db, title="heavy", description=BIG, project_id="core", status="review"
        )
        row = db.get(Item, item.id)
        row.evidence = [
            {"kind": "note", "detail": BIG, "url": ""},
            {"kind": "attestation", "detail": "ok", "url": "", "adapter": "git", "commit": "abc"},
        ]
        row.bounce_reason = BIG
        db.commit()
        return item.key if hasattr(item, "key") else item.id
    finally:
        db.close()


def _find(rows, key):
    return next(r for r in rows if r["id"] == key)


def test_default_list_omits_heavy_fields(client, auth, seeded):
    r = client.get("/api/items?project_id=core", headers=auth)
    assert r.status_code == 200
    row = _find(r.json(), seeded)
    for f in ("description", "evidence", "bounce_reason"):
        assert f not in row
    assert BIG not in r.text
    assert row["has_attestation"] is True


def test_full_list_includes_heavy_fields(client, auth, seeded):
    r = client.get("/api/items?project_id=core&fields=full", headers=auth)
    assert r.status_code == 200
    row = _find(r.json(), seeded)
    assert row["description"] == BIG
    assert len(row["evidence"]) == 2
    assert row["bounce_reason"] == BIG


def test_detail_still_full(client, auth, seeded):
    row = client.get(f"/api/items/{seeded}", headers=auth).json()
    assert row["description"] == BIG and row["evidence"]


def test_lean_field_set_is_pinned(client, auth, seeded):
    row = _find(client.get("/api/items?project_id=core", headers=auth).json(), seeded)
    assert set(row) == LEAN_FIELDS
    full = _find(client.get("/api/items?project_id=core&fields=full", headers=auth).json(), seeded)
    assert set(full) - set(row) == ITEM_HEAVY_FIELDS


def test_lean_list_is_much_smaller_on_a_large_project(client, auth):
    from app.db import SessionLocal
    from app.models import Item
    from app.services import items as items_svc

    db = SessionLocal()
    try:
        for i in range(1000):
            it = items_svc.create_item(db, title=f"t{i}", description="d" * 800, project_id="core")
            db.get(Item, it.id).evidence = [{"kind": "note", "detail": "e" * 1500, "url": ""}] * 3
        db.commit()
    finally:
        db.close()
    lean = len(client.get("/api/items?project_id=core", headers=auth).content)
    full = len(client.get("/api/items?project_id=core&fields=full", headers=auth).content)
    print(f"GRPH-1228 size: full={full} lean={lean}")
    assert lean * 3 < full
