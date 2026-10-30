"""Which gate refusals become memory (GRPH-1234).

A `pr_cooldown` refusal is a routine wait, not a lesson: the item keeps its evidence note and
no shard is written. Refusals that name something to change (`attestation_missing`) still
write one. Migration 0160 retires the shards written before that.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import settings
from app.models import MemoryShard
from app.services import items as items_svc
from app.services import memory as memory_svc

from tests.test_pr_cooldown import ATTESTATION, PR, _item, db  # noqa: F401

MIGRATION = (Path(__file__).resolve().parents[1] / "alembic" / "versions"
             / "0160_retire_pr_cooldown_refusal_shards.py")


def _refusal_shards(db, item, source):
    return db.scalars(select(MemoryShard).where(
        MemoryShard.item_id == item.id, MemoryShard.source == source)).all()


def test_pr_cooldown_refusal_writes_evidence_but_no_shard(db, monkeypatch):
    monkeypatch.setattr(settings, "pr_cooldown_seconds", 60, raising=False)
    it = _item(db)
    with pytest.raises(items_svc.PRCooldown):
        items_svc.update_item(db, it.id, status="done",
                              evidence=[PR, ATTESTATION])
    db.refresh(it)
    assert any("(pr_cooldown)" in (e.get("detail") or "") for e in it.evidence)
    assert not _refusal_shards(db, it, "gate refusal: pr_cooldown")


def test_attestation_missing_refusal_still_writes_a_shard(db):
    it = _item(db)
    with pytest.raises(items_svc.MissingAttestation):
        items_svc.update_item(db, it.id, status="done")
    assert len(_refusal_shards(db, it, "gate refusal: attestation_missing")) == 1


def _seed(db, item, source, status="published"):
    shard = memory_svc.add_memory(
        db, text_body=f"{item.key} refused ({source})", scope="item", source=source,
        item_id=item.id, project_id=item.project_id, status=status, origin="gate",
        auto_triage=False)
    return shard.id


def test_migration_retires_only_pr_cooldown_shards_and_is_idempotent(db):
    spec = importlib.util.spec_from_file_location("m0160", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    it = _item(db)
    cool = _seed(db, it, "gate refusal: pr_cooldown")
    other = _seed(db, it, "gate refusal: attestation_missing")
    bind = db.connection()

    assert mod.retire(bind, dry_run=True) == 1
    assert mod.retire(bind) == 1
    db.expire_all()
    cooled = db.get(MemoryShard, cool)
    assert cooled.status == "retired" and cooled.retired_at is not None
    assert db.get(MemoryShard, other).status == "published"

    assert mod.retire(bind) == 0
