"""`gitops.merge_method`: squash | merge | rebase (GRPH-1428)."""
from __future__ import annotations

import pytest

from app.services import gitops as gitops_svc
from tests.test_gitops import _get, _patch


def _view(project_id):
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        # What `get_context` hands the supervisor under `gitops` (GRPH-1428).
        return gitops_svc.for_agent(gitops_svc.resolve(db, project_id))["merge_method"]
    finally:
        db.close()


def test_unset_is_unmeasured_and_reads_as_squash(client, auth):
    r = _get(client, auth)
    assert r.json()["merge_method"] == {"value": None, "source": "unmeasured"}
    # Unmeasured, not a guess: the supervisor reads it as `squash` (fleet test_merge_method).
    assert _view("core") == {"value": None, "source": "unmeasured"}


@pytest.mark.parametrize("value", ["squash", "merge", "rebase"])
def test_accepts_the_three_methods(client, auth, value):
    r = _patch(client, auth, {"merge_method": value})
    assert r.status_code == 200, r.text
    assert r.json()["merge_method"] == {"value": value, "source": "project"}
    assert _view("core") == {"value": value, "source": "project"}


def test_refuses_a_value_outside_the_three(client, auth):
    r = _patch(client, auth, {"merge_method": "fast-forward"})
    assert r.status_code == 422
    assert _get(client, auth).json()["merge_method"]["value"] is None
