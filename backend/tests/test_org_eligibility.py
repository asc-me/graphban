"""Org eligibility is independence-only, and a missing scan is unverifiable.

Sabotaging the cluster to [shard] without a scan must not report ineligible-as-looked:
that is 'we did not look' wearing 'nothing has earned it'.
"""
from app.models import MemoryShard
from app.services import memory as mem_svc

_UNSET = object()


def _sh(sid, *, user, project, reach="project", origin="user:x", attributed=_UNSET):
    return MemoryShard(
        id=sid, text="x", project_id=project, reach=reach, origin=origin,
        actor_user_id=user,
        attributed_project_id=project if attributed is _UNSET else attributed,
        status="published",
    )


def test_unscanned_cluster_is_unverifiable_not_ineligible():
    shard = _sh("m1", user="u1", project="p1")
    out = mem_svc.org_eligibility(
        shard, [shard], scan="cluster_scope_unmeasured")
    assert out["state"] == "unverifiable"
    assert out["state"] != "ineligible"
    assert out["independence"] is None
    assert "cluster_scope_unmeasured" in out["reason"]


def test_scan_not_scanned_is_unverifiable_even_when_ids_are_set():
    """The load-bearing third state. Both ids set + cluster of one is still
    unverifiable if nobody scanned siblings — otherwise 1×1 reads as looked."""
    shard = _sh("m1", user="u1", project="p1")
    out = mem_svc.org_eligibility(shard, [shard], scan="cluster_scope_unmeasured")
    assert out["state"] == "unverifiable"


def test_sabotaged_cluster_of_one_without_a_scan_is_not_ineligible_as_looked():
    """If published_cluster is replaced with [shard], eligibility must not
    report ineligible with independence 1. That is the absence-as-clean lie."""
    shard = _sh("m1", user="u1", project="p1")
    out = mem_svc.org_eligibility(shard, [shard], scan="cluster_scope_unmeasured")
    assert out["state"] != "ineligible"
    assert out["state"] == "unverifiable"


def test_empty_cluster_is_unverifiable_not_independence_zero():
    shard = _sh("m1", user="u1", project="p1")
    out = mem_svc.org_eligibility(shard, [], scan="scanned")
    assert out["state"] == "unverifiable"
    assert out["independence"] is None


def test_null_user_is_unverifiable_not_ineligible():
    shard = _sh("m1", user=None, project="p1")
    out = mem_svc.org_eligibility(shard, [shard], scan="scanned")
    assert out["state"] == "unverifiable"
    assert "distinct_users" in out["reason"]


def test_ingest_without_attributed_project_is_unverifiable():
    shard = _sh(
        "m1", user="u1", project="core", origin="ingest:claude-code:done",
        attributed=None,
    )
    # _project_of must not fall back to project_id for ingest.
    assert mem_svc._project_of(shard) is None
    out = mem_svc.org_eligibility(shard, [shard], scan="scanned")
    assert out["state"] == "unverifiable"
    assert "distinct_projects" in out["reason"]


def test_scanned_one_user_three_projects_is_eligible():
    cluster = [
        _sh("a", user="u1", project="p1"),
        _sh("b", user="u1", project="p2"),
        _sh("c", user="u1", project="p3"),
    ]
    out = mem_svc.org_eligibility(cluster[0], cluster, scan="scanned")
    assert out["state"] == "eligible"
    assert out["independence"] == 3
    assert out["distinct_projects"] == 3
    assert out["distinct_users"] == 1


def test_scanned_two_by_two_is_eligible():
    cluster = [
        _sh("a", user="u1", project="p1"),
        _sh("b", user="u2", project="p2"),
    ]
    out = mem_svc.org_eligibility(cluster[0], cluster, scan="scanned")
    assert out["state"] == "eligible"
    assert out["independence"] == 3


def test_scanned_three_users_one_project_is_eligible():
    cluster = [
        _sh("a", user="u1", project="p1"),
        _sh("b", user="u2", project="p1"),
        _sh("c", user="u3", project="p1"),
    ]
    out = mem_svc.org_eligibility(cluster[0], cluster, scan="scanned")
    assert out["state"] == "eligible"
    assert out["independence"] == 3


def test_scanned_one_user_two_projects_is_ineligible():
    cluster = [
        _sh("a", user="u1", project="p1"),
        _sh("b", user="u1", project="p2"),
    ]
    out = mem_svc.org_eligibility(cluster[0], cluster, scan="scanned")
    assert out["state"] == "ineligible"
    assert out["independence"] == 2


def test_scanned_cluster_of_one_with_both_fields_is_honestly_ineligible():
    """We looked. 1×1 is independence 1. That is not the unscanned case."""
    shard = _sh("m1", user="u1", project="p1")
    out = mem_svc.org_eligibility(shard, [shard], scan="scanned")
    assert out["state"] == "ineligible"
    assert out["independence"] == 1


def test_already_org_reach_is_promoted():
    shard = _sh("m1", user="u1", project="p1", reach="org")
    out = mem_svc.org_eligibility(shard, [shard], scan="scanned")
    assert out["state"] == "promoted"


def test_eligibility_does_not_consume_effectiveness():
    """Independence-only: a failing lesson with 1×3 is still eligible."""
    cluster = [
        _sh("a", user="u1", project="p1"),
        _sh("b", user="u1", project="p2"),
        _sh("c", user="u1", project="p3"),
    ]
    out = mem_svc.org_eligibility(cluster[0], cluster, scan="scanned")
    assert out["state"] == "eligible"
    assert "effectiveness" not in out
    assert "score" not in out
