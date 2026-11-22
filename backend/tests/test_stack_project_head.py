"""GRPH-1825 — a project's stack map and scans compare to its code-graph revision.

Not the box's own build sha: that identifies Graphban, not the project's code.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models import AppTopology, CodeNode, CodeScan
from app.services import code_graph
from app.services import scans as scans_svc
from app.services import stack as stack_svc

PID = "core"
R = "a" * 40
R2 = "b" * 40
BUILD = "c" * 40


@pytest.fixture()
def db(client, monkeypatch):
    from app.config import settings
    from app.db import SessionLocal

    monkeypatch.setattr(settings, "git_sha", BUILD)
    s = SessionLocal()
    s.query(CodeNode).filter(CodeNode.project_id == PID).delete()
    s.query(AppTopology).filter(AppTopology.project_id == PID).delete()
    s.query(CodeScan).filter(CodeScan.project_id == PID).delete()
    s.commit()
    try:
        yield s
    finally:
        s.close()


def _graph(db, *revisions):
    for i, rev in enumerate(revisions):
        code_graph.describe_code(
            db, project_id=PID, nodes=[{"path": f"src/f{i}.py", "kind": "file", "content_hash": f"sha256:{i:064x}"}],
            revision=rev,
        )


def _topology(db, mapped_sha):
    db.add(AppTopology(
        project_id=PID, mapped_sha=mapped_sha, mapped_at=datetime.now(timezone.utc),
        components=[{
            "id": "src", "dir": "src", "label": "src",
            "provenance": {"file": "src/index.ts", "rule": "main", "line": 1},
        }],
    ))
    db.commit()


def _mapping(db):
    return scans_svc.component_health(db, PID)["mapping"]


def test_map_at_graph_revision_is_mapped_while_build_sha_differs(db):
    _graph(db, R, R)
    _topology(db, R)
    m = _mapping(db)
    assert m["state"] == "mapped"
    assert m["head_sha"] == R != BUILD


def test_map_at_other_revision_is_stale(db):
    _graph(db, R)
    _topology(db, R2)
    assert _mapping(db)["state"] == "stale"


def test_mixed_revision_graph_is_freshness_unknown(db):
    _graph(db, R, R2)
    _topology(db, R)
    m = _mapping(db)
    assert m["state"] == "freshness_unknown"
    assert m["head_sha"] is None


def test_empty_graph_is_freshness_unknown_not_the_build_sha(db):
    _topology(db, BUILD)
    assert _mapping(db)["state"] == "freshness_unknown"


def _scan_state(db, head_sha=""):
    health = scans_svc.component_health(db, PID, head_sha=head_sha)
    comp = next(c for c in health["components"] if c["path"] == "src/")
    return health, comp["categories"]["stub"]["state"]


def _scan_at(db, sha):
    scans_svc.record_scan(
        db, project_id=PID, scanner_id="stubscan", path="src/", scanned_sha=sha,
        findings=[], result="complete", category="stub",
    )


def test_scan_cell_at_graph_revision_is_clean(db):
    _graph(db, R)
    _topology(db, R)
    _scan_at(db, R)
    health, state = _scan_state(db)
    assert state == "clean_at_sha"
    assert health["head_sha"] == R


def test_scan_cell_at_other_revision_is_stale(db):
    _graph(db, R)
    _topology(db, R)
    _scan_at(db, R2)
    assert _scan_state(db)[1] == "stale"


def test_scan_cell_with_mixed_graph_is_never_clean(db):
    _graph(db, R, R2)
    _topology(db, R)
    _scan_at(db, R)
    health, state = _scan_state(db)
    assert health["head_sha"] is None
    assert state != "clean_at_sha"


def test_explicit_head_sha_overrides_graph_revision(db):
    _graph(db, R)
    _topology(db, R)
    due = scans_svc.scans_due(db, PID, head_sha=R2)
    assert due["head_sha"] == R2
    assert scans_svc.scans_due(db, PID)["head_sha"] == R


def test_project_head_is_not_the_build_sha(db):
    # Sabotage guard: restoring the build sha as the head must fail here.
    _graph(db, R)
    assert stack_svc.project_head_sha(db, PID) == R
    assert stack_svc.project_head_sha(db, PID) != BUILD


def _symbol(db, name, revision):
    code_graph.describe_code(
        db, project_id=PID, nodes=[{"path": f"src/f0.py::{name}", "kind": "symbol"}],
        revision=revision,
    )


def test_symbol_nodes_do_not_unpin_the_file_map(db):
    _graph(db, R, R)
    _symbol(db, "a", "")
    _symbol(db, "b", R2)
    assert stack_svc.project_head_sha(db, PID) == R
    _topology(db, R)
    m = _mapping(db)
    assert m["head_sha"] == R
    assert m["state"] == "mapped"
    assert scans_svc.scans_due(db, PID)["head_sha"] == R


def test_mixed_file_revisions_stay_unknown_whatever_symbols_say(db):
    _graph(db, R, R2)
    _symbol(db, "a", R)
    assert stack_svc.project_head_sha(db, PID) is None
    _topology(db, R)
    assert _mapping(db)["state"] == "freshness_unknown"


def test_file_node_without_revision_is_unknown(db):
    _graph(db, R, "")
    assert stack_svc.project_head_sha(db, PID) is None


def test_retired_nodes_do_not_unpin_the_map(db):
    _graph(db, R, R)
    code_graph.describe_code(
        db, project_id=PID, nodes=[{"path": "src/old.py", "kind": "file",
                                    "content_hash": "sha256:" + "9" * 64}],
        revision=R2,
    )
    code_graph.describe_code(db, project_id=PID, stale=["src/old.py"], revision=R)
    old = db.query(CodeNode).filter_by(project_id=PID, path="src/old.py").one()
    assert not old.fresh and old.revision == R2
    assert stack_svc.project_head_sha(db, PID) == R


def test_directory_nodes_without_content_do_not_unpin_the_map(db):
    _graph(db, R, R)
    code_graph.describe_code(db, project_id=PID, nodes=[{"path": "src", "kind": "module"}])
    d = db.query(CodeNode).filter_by(project_id=PID, path="src").one()
    assert not d.content_hash and not d.revision
    assert stack_svc.project_head_sha(db, PID) == R
