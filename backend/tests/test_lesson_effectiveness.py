"""Effectiveness is computed from outcomes, not stored, and quiet is not success.

An empty list is unknown / unmeasured / score None — never 1.0. origin_path=gone
forces dropping; an empty code graph is unindexed, not gone.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models import CodeNode, MemoryShard
from app.services import items as items_svc
from app.services import memory as mem_svc


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


NOW = datetime(2026, 8, 29, tzinfo=timezone.utc)


def _shard(**kw):
    defaults = dict(
        id="m_eff", text="A lesson.", project_id="core", status="published",
        created_at=NOW, reach="project", lesson_class="",
    )
    defaults.update(kw)
    return MemoryShard(**defaults)


def _oc(kind, *, source="human", at=None, oid=1):
    return SimpleNamespace(
        kind=kind, source=source, created_at=at or NOW, id=oid, shard_id="m_eff",
        detail="",
    )


def test_empty_outcomes_are_unmeasured_not_a_high_score():
    """THE absence rule. Nothing counted is unknown, not 1.0."""
    out = mem_svc.lesson_effectiveness(_shard(), [], origin_path="ok", now=NOW)
    assert out["score"] is None
    assert out["caught_state"] == "unknown"
    assert out["trend"] == "unmeasured"
    assert out["history"] == []
    assert out["score"] != 1.0


def test_applied_alone_is_still_unknown():
    """Applied is not caught. Absence of a later miss is not success."""
    out = mem_svc.lesson_effectiveness(
        _shard(), [_oc("applied")], origin_path="ok", now=NOW)
    assert out["score"] is None
    assert out["caught_state"] == "unknown"
    assert out["history"] == []


def test_two_caught_is_score_one():
    outs = [_oc("caught", oid=1), _oc("caught", oid=2)]
    out = mem_svc.lesson_effectiveness(_shard(), outs, origin_path="ok", now=NOW)
    assert out["score"] == 1.0
    assert out["caught_state"] == "caught"


def test_two_missed_is_score_zero():
    outs = [_oc("missed", oid=1), _oc("missed", oid=2)]
    out = mem_svc.lesson_effectiveness(_shard(), outs, origin_path="ok", now=NOW)
    assert out["score"] == 0.0
    assert out["caught_state"] == "missed"


def test_one_each_is_mixed():
    outs = [_oc("caught", oid=1), _oc("missed", oid=2)]
    out = mem_svc.lesson_effectiveness(_shard(), outs, origin_path="ok", now=NOW)
    assert out["caught_state"] == "mixed"
    assert out["score"] == 0.5


def test_contradicted_is_a_named_drop():
    out = mem_svc.lesson_effectiveness(
        _shard(), [_oc("contradicted")], origin_path="ok", now=NOW)
    assert "contradicted" in out["drop_reasons"]
    assert out["trend"] == "dropping"
    assert out["score"] == 0.0


def test_applied_then_missed_names_applied_and_recurred():
    earlier = NOW - timedelta(days=2)
    outs = [_oc("applied", at=earlier, oid=1), _oc("missed", at=NOW, oid=2)]
    out = mem_svc.lesson_effectiveness(_shard(), outs, origin_path="ok", now=NOW)
    assert "applied_and_recurred" in out["drop_reasons"]
    assert out["trend"] == "dropping"


def test_drops_while_age_state_is_still_fresh():
    """A shard can be fresh and already failing. Age is not effectiveness."""
    shard = _shard(created_at=NOW)
    outs = [_oc("missed", oid=1), _oc("missed", oid=2)]
    out = mem_svc.lesson_effectiveness(shard, outs, origin_path="ok", now=NOW)
    assert mem_svc.age_state(shard, now=NOW) == "fresh"
    assert out["score"] == 0.0
    assert out["caught_state"] == "missed"


def test_quiet_does_not_raise():
    """Nothing new happening is not a bonus. Trend stays stable, score unchanged."""
    old = NOW - timedelta(days=90)
    shard = _shard(created_at=old)
    outs = [_oc("caught", at=old, oid=1), _oc("caught", at=old + timedelta(days=1), oid=2)]
    out = mem_svc.lesson_effectiveness(shard, outs, origin_path="ok", now=NOW)
    assert out["score"] == 1.0
    assert out["trend"] == "stable"
    missed = [_oc("missed", at=old, oid=1)]
    low = mem_svc.lesson_effectiveness(shard, missed, origin_path="ok", now=NOW)
    assert low["score"] == 0.0
    assert low["trend"] == "stable"


def test_origin_path_gone_drops_even_when_unmeasured():
    out = mem_svc.lesson_effectiveness(_shard(), [], origin_path="gone", now=NOW)
    assert out["score"] is None
    assert out["trend"] == "dropping"
    assert "origin_path_gone" in out["drop_reasons"]
    assert out["caught_state"] == "unknown"


def test_origin_path_unindexed_does_not_drop():
    """An empty code graph is not a deleted path."""
    out = mem_svc.lesson_effectiveness(_shard(), [], origin_path="unindexed", now=NOW)
    assert out["score"] is None
    assert out["trend"] == "unmeasured"
    assert "origin_path_gone" not in out["drop_reasons"]


def test_origin_path_unknown_is_not_ok_or_gone():
    out = mem_svc.lesson_effectiveness(_shard(), [], origin_path="unknown", now=NOW)
    assert out["trend"] == "unmeasured"
    assert "origin_path_gone" not in out["drop_reasons"]


def test_empty_code_graph_is_unindexed_not_gone(db):
    """Sabotaging the empty-graph branch (treating 0 nodes as gone) must fail this."""
    item = items_svc.create_item(
        db, title="claimed a path", project_id="core",
        touchpoints=["app/services/memory.py"],
    )
    shard = mem_svc.add_memory(
        db, text_body="lesson about memory.py", item_id=item.id, project_id="core",
        status="published",
    )
    assert mem_svc.origin_path_state(db, shard) == "unindexed"
    eff = mem_svc.lesson_effectiveness(shard, [], origin_path=mem_svc.origin_path_state(db, shard))
    assert eff["trend"] == "unmeasured"
    assert "origin_path_gone" not in eff["drop_reasons"]


def test_graph_with_other_files_but_not_the_touchpoint_is_gone(db):
    item = items_svc.create_item(
        db, title="claimed a path", project_id="core",
        touchpoints=["app/services/memory.py"],
    )
    db.add(CodeNode(
        id="cn_other", project_id=item.project_id, path="web/src/App.tsx",
        kind="file", name="App",
    ))
    db.commit()
    shard = mem_svc.add_memory(
        db, text_body="lesson about memory.py", item_id=item.id, project_id="core",
        status="published",
    )
    assert mem_svc.origin_path_state(db, shard) == "gone"
    eff = mem_svc.lesson_effectiveness(shard, [], origin_path="gone")
    assert eff["trend"] == "dropping"
    assert "origin_path_gone" in eff["drop_reasons"]


def test_no_item_is_unknown_not_unindexed(db):
    shard = mem_svc.add_memory(db, text_body="ingested, no item", project_id="core",
                               status="published")
    assert mem_svc.origin_path_state(db, shard) == "unknown"


def test_origin_path_walks_item_project_not_viewer(db):
    """Org-reach listed from B still looks up A's graph."""
    item = items_svc.create_item(
        db, title="in core", project_id="core",
        touchpoints=["app/services/memory.py"],
    )
    db.add(CodeNode(
        id="cn_hit", project_id="core", path="app/services/memory.py",
        kind="file", name="memory",
    ))
    db.commit()
    shard = mem_svc.add_memory(
        db, text_body="lesson", item_id=item.id, project_id="core", status="published",
    )
    shard.reach = "org"
    db.commit()
    assert mem_svc.origin_path_state(db, shard) == "ok"
