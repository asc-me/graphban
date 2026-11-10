"""GRPH-1496: `plan.path_classes` — per-project globs for the delegation brief's lane and class."""
from __future__ import annotations

import pytest

from app.models import User
from app.services import delegation as D
from app.services import rule_pack as P
from app.services import rule_toggles as T
from tests.test_rule_pack_p74 import PACK, TRUST, _pack

TEMPLATE = '''id = "plan.path_classes"
stage = "Plan"
title = "Path classes"
summary = "s"
primitive = "path_classes"
claims = "checked"
default = "on"
[params]
frontend = ["web", "web/**"]
migration = ["*alembic/versions*", "*models/__init__*"]
mcp_tool = ["*mcp_server.py"]
docs = ["*.md", "docs/**"]
'''

CASES = [
    ["web/src/a.tsx"], ["backend/x.py"], ["web/a.ts", "backend/x.py"],
    ["backend/alembic/versions/x.py"], ["backend/app/models/__init__.py"],
    ["backend/app/mcp_server.py"], ["docs/rules.md"], ["README.md", "docs/a.json"],
    ["./web/a.tsx"], ["web"], ["docs/a.json", "backend/x.py"], [],
]


@pytest.fixture()
def library(monkeypatch):
    monkeypatch.setattr(T, "LIBRARY", {})
    pack = P.load(*_pack({"plan.path_classes": TEMPLATE}), trusted_keys=TRUST)
    for tid, t in pack.templates.items():
        T.register(P._library_rule(tid, t, pack.refused.get(tid, "")))
    return pack


def _old_checklist(paths):
    if any("alembic/versions" in p or "models/__init__" in p for p in paths):
        return "migration"
    if any(p.endswith("mcp_server.py") for p in paths):
        return "mcp_tool"
    web = lambda p: (p.strip().lstrip("./") == "web" or p.strip().lstrip("./").startswith("web/"))
    if paths and all(web(p) for p in paths):
        return "frontend"
    if paths and all(p.endswith(".md") or p.startswith("docs/") for p in paths):
        return "docs"
    return None


@pytest.mark.parametrize("paths", CASES)
def test_defaults_reproduce_the_old_answers(paths):
    assert D.checklist_for(paths) == _old_checklist(paths)
    assert D.checklist_for(paths, T.path_classes(None, None)) == _old_checklist(paths)
    expect = ("backend" if not paths or not any(p.strip().lstrip("./").startswith("web")
              for p in paths) else None)
    if expect:
        assert D.lane_for(paths)["value"] == expect


def test_toggle_defaults_equal_delegation_defaults():
    assert T.PATH_CLASSES_DEFAULT == D.PATH_CLASSES_DEFAULT


def test_migration_glob_replaces_alembic():
    cls = {"migration": ["db/migrations/**"]}
    assert D.checklist_match(["db/migrations/0003.sql"], cls) == ("migration", ["db/migrations/**"])
    assert D.checklist_for(["backend/alembic/versions/x.py"], cls) is None


def test_frontend_glob_sets_lane():
    lane = D.lane_for(["client/src/App.tsx"], {"frontend": ["client/**"]})
    assert lane["value"] == "frontend"
    assert lane["basis"] == ["client/src/App.tsx"]
    assert lane["matched"] == ["client/**"]
    assert D.lane_for(["web/a.tsx"], {"frontend": ["client/**"]})["value"] == "backend"


def test_empty_list_never_matches():
    assert D.checklist_for(["web/a.tsx"], {"frontend": []}) is None


def test_text_names_the_matched_glob(client, library):
    from types import SimpleNamespace
    item = SimpleNamespace(key="X-1", title="t", branch="", bounce_reason="")
    text = D._text(item, summary="", touchpoints=["db/migrations/1.sql"], blocked_by=[],
                   checklist="migration", checklist_matched=["db/migrations/**"],
                   lessons=[], previous=None, attempts=[])
    assert ("Task class: migration (matched db/migrations/**; follow that checklist "
            "in AGENTS.md)") in text


def test_pack_loads_and_a_person_writes_each_param(client, library):
    from app.db import SessionLocal
    assert library.refused == {}
    db = SessionLocal()
    try:
        assert T.path_classes(db, "core") == T.PATH_CLASSES_DEFAULT
        user = db.query(User).first()
        for k, v in {"frontend": ["client/**"], "migration": ["db/migrations/**"],
                     "mcp_tool": ["tools/*.py"], "docs": []}.items():
            T.set_project_toggle(db, "core", T.PATH_CLASSES_RULE, {"params": {k: v}},
                                 actor=user, reason="this project's layout")
            db.commit()
            assert T.path_classes(db, "core")[k] == v
    finally:
        db.close()


def test_a_pack_without_the_rule_reads_the_default(monkeypatch):
    monkeypatch.setattr(T, "LIBRARY", {})
    assert T.path_classes(None, None) == T.PATH_CLASSES_DEFAULT
