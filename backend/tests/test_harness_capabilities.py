"""PRD-41 S1 — capability derivation (criteria 1, 2). One fixture per §5 leaf.

The function is pure: a dummy item plus a frozen diff and outcome. Sabotage of criterion 1
is deriving a primary label; sabotage of criterion 2 is returning an empty list.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.services import harness as hsvc


def _item(**kw):
    defaults = dict(touchpoints=[], tags=[], description="", evidence=[],
                    prd_id=None, prd_section=None, branch="gb/x")
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _shape(**kw):
    base = dict(files_added=0, files_modified=0, files_deleted=0, files_renamed=0,
                test_files=0, net_lines=0, layers=[], added=[], modified=[],
                deleted=[], renamed=[], paths=[])
    base.update(kw)
    return base


def test_catalog_is_the_closed_enum():
    cat = hsvc.capability_catalog()
    assert cat["leaves"] == list(hsvc.CAPABILITY_LEAVES)
    assert set(cat["families"]) >= {"A", "B", "C", "H", "E", "F", "other"}
    assert "A4" in cat["labels"] and cat["families"]["A"] == ["A1", "A2", "A3", "A4", "A5"]


def test_no_match_lands_in_other_not_an_empty_list():
    """2. Sabotage: return [] and the page would show no cell, which reads as none."""
    got = hsvc.capabilities(_item(touchpoints=["backend/app/x.py"]))
    assert got == ["other"]


def test_criterion_1_migration_plus_added_route_is_the_set():
    """1. Sabotage: derive a primary label only and the A2 cell empties."""
    item = _item(touchpoints=["backend/alembic/versions/0117_capability_axis.py"])
    shape = _shape(
        files_added=2, net_lines=40, layers=["B1", "B4"],
        added=["backend/app/routers/harness.py",
               "backend/alembic/versions/0117_capability_axis.py"],
        paths=["backend/app/routers/harness.py",
               "backend/alembic/versions/0117_capability_axis.py"],
    )
    got = hsvc.capabilities(item, shape, {"outcome": "signed_off"})
    assert set(got) >= {"A2", "A4", "B1"}
    assert "other" not in got


def test_hybrid_file_tags_every_matching_leaf():
    got = hsvc.capabilities(_item(touchpoints=["backend/app/models/__init__.py"]))
    assert set(got) >= {"A4", "B4"}


# ---- one fixture per leaf -------------------------------------------------------------------

def test_A1_localised_fix():
    item = _item(tags=["bug"], touchpoints=["backend/app/foo.py", "backend/tests/test_foo.py"])
    shape = _shape(files_added=0, files_modified=2, test_files=1,
                   added=[], modified=["backend/app/foo.py", "backend/tests/test_foo.py"],
                   paths=["backend/app/foo.py", "backend/tests/test_foo.py"])
    assert "A1" in hsvc.capabilities(item, shape)


def test_A2_feature_slice():
    shape = _shape(files_added=2, layers=["B1", "B5"],
                   added=["backend/app/routers/x.py", "web/src/features/x/X.tsx"])
    assert "A2" in hsvc.capabilities(_item(), shape)


def test_A3_rename_with_tests_untouched():
    shape = _shape(files_renamed=1, renamed=["backend/app/services/renamed.py"],
                   paths=["backend/app/services/renamed.py"])
    assert "A3" in hsvc.capabilities(_item(), shape)


def test_A4_alembic():
    assert "A4" in hsvc.capabilities(_item(touchpoints=["backend/alembic/versions/0001.py"]))


def test_A5_deletion():
    shape = _shape(files_deleted=1, net_lines=-12, deleted=["backend/app/dead.py"])
    assert "A5" in hsvc.capabilities(_item(), shape)


def test_B1_rest():
    assert "B1" in hsvc.capabilities(_item(touchpoints=["backend/app/routers/items.py"]))


def test_B2_mcp():
    assert "B2" in hsvc.capabilities(_item(touchpoints=["backend/app/mcp_server.py"]))


def test_B3_service_only():
    assert "B3" in hsvc.capabilities(
        _item(touchpoints=["backend/app/services/items.py"]))
    # Another layer present means this is not B3.
    assert "B3" not in hsvc.capabilities(
        _item(touchpoints=["backend/app/services/items.py", "backend/app/routers/items.py"]))


def test_B4_models():
    assert "B4" in hsvc.capabilities(_item(touchpoints=["backend/app/models/__init__.py"]))


def test_B5_react():
    assert "B5" in hsvc.capabilities(_item(touchpoints=["web/src/features/harness/HarnessView.tsx"]))


def test_B6_visual():
    assert "B6" in hsvc.capabilities(_item(touchpoints=["web/src/components/ui/button.tsx"]))


def test_B7_cli():
    assert "B7" in hsvc.capabilities(_item(touchpoints=["fleet/src/gbfleet/worktree.py"]))


def test_B8_infra():
    assert "B8" in hsvc.capabilities(_item(touchpoints=[".github/workflows/ci.yml"]))


def test_C1_red_sabotage():
    item = _item(evidence=[{"kind": "sabotage", "tests_failed": 2, "detail": "reverted"}])
    assert "C1" in hsvc.capabilities(item, None, {"evidence": item.evidence})


def test_C2_red_to_green():
    shape = _shape(test_files=1, modified=["backend/tests/test_x.py"])
    assert "C2" in hsvc.capabilities(_item(), shape, {"turns_used": 4, "outcome": "signed_off"})


def test_C3_repro_first():
    item = _item(tags=["bug"])
    shape = _shape(files_added=1, test_files=1, added=["backend/tests/test_repro.py"])
    assert "C3" in hsvc.capabilities(item, shape)


def test_H1_lease_path():
    assert "H1" in hsvc.capabilities(_item(touchpoints=["backend/app/services/claiming.py"]))


def test_H2_authz():
    assert "H2" in hsvc.capabilities(_item(touchpoints=["backend/app/security/authz.py"]))


def test_H3_both_engines():
    assert "H3" in hsvc.capabilities(_item(touchpoints=[
        "backend/tests/test_sqlite.py", "backend/tests/test_postgres.py"]))


def test_H4_size_L():
    item = _item(touchpoints=[f"f{i}.py" for i in range(6)])
    assert "H4" in hsvc.capabilities(item)


def test_H5_has_a_spec():
    assert "H5" in hsvc.capabilities(_item(prd_section="S1"))
    assert "H5" not in hsvc.capabilities(_item())


def test_E1_released_or_tool_errors():
    assert "E1" in hsvc.capabilities(_item(), None, {"outcome": "released"})
    assert "E1" in hsvc.capabilities(_item(), None, {"tool_errors": 3})
    assert "E1" in hsvc.capabilities(_item(branch=""), None, {"has_branch": False})


def test_E2_instruction_file():
    assert "E2" in hsvc.capabilities(_item(touchpoints=["AGENTS.md"]))


def test_E3_docs():
    assert "E3" in hsvc.capabilities(_item(touchpoints=["docs/api-reference.md"]))


def test_F_reviewer_role():
    assert "F1" in hsvc.capabilities(_item(), None, {"role": "reviewer", "outcome": "bounced"})
    assert "F2" in hsvc.capabilities(_item(), None, {"role": "reviewer", "outcome": "signed_off"})
    assert "F3" in hsvc.capabilities(
        _item(), None, {"role": "reviewer", "outcome": "bounced", "bounce_category": "quality"})
    assert "F1" not in hsvc.capabilities(_item(), None, {"outcome": "signed_off"})
