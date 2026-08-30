"""Ingest stamps attributed_project_id from the Claude Code transcript directory (GRPH-356).

Unmapped stays NULL — never the collapsed project_id, never "core". Actor stays NULL
until a human publishes. Old rows are not backfilled.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import MemoryShard
from app.services import memory as mem_svc
from app.services.ingest.claude_code import ClaudeCodeAdapter
from app.services.ingest.runner import ingest
from tests.test_transcript_ingest import LESSON, _line


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _write_repo(root, folder: str, session: str, text: str = LESSON,
                cwd: str | None = None) -> None:
    d = root / f"-Users-test-src-{folder}"
    d.mkdir(parents=True, exist_ok=True)
    cwd = cwd or f"/Users/test/src/{folder}"
    (d / f"{session}.jsonl").write_text(_line(text, sessionId=session, cwd=cwd))


def _transcript_shards(db) -> list[MemoryShard]:
    return list(db.scalars(
        select(MemoryShard).where(MemoryShard.source.like("transcript:%"))
    ).all())


def _publish(client, auth, shard_id: str) -> None:
    r = client.post(f"/api/memory/shards/{shard_id}/publish", headers=auth)
    assert r.status_code == 200, r.text


def _elig(client, auth, shard_id: str, project_id: str = "core") -> dict:
    r = client.get(f"/api/memory/lessons/{shard_id}?project_id={project_id}", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()["eligibility"]


def test_repo_dir_for_none_at_adapter_root(tmp_path):
    adapter = ClaudeCodeAdapter(root=str(tmp_path))
    at_root = tmp_path / "s.jsonl"
    at_root.write_text(_line(LESSON))
    assert adapter.repo_dir_for(str(at_root)) is None


def test_repo_dir_for_is_the_encoded_repo_folder(tmp_path):
    adapter = ClaudeCodeAdapter(root=str(tmp_path))
    nested = tmp_path / "-Users-test-src-alpha-map" / "s.jsonl"
    nested.parent.mkdir()
    nested.write_text(_line(LESSON))
    assert adapter.repo_dir_for(str(nested)) == "-Users-test-src-alpha-map"


def test_unmapped_ingest_leaves_attributed_null_never_core(client, db, tmp_path):
    """A file at the adapter root has no repo identity. Copying project_id='core' fails this."""
    (tmp_path / "s.jsonl").write_text(_line(LESSON, sessionId="sess-unmapped"))
    ingest(db, ClaudeCodeAdapter(root=str(tmp_path)), project_id="core")
    shards = _transcript_shards(db)
    assert len(shards) == 1
    shard = shards[0]
    assert shard.project_id == "core"
    assert shard.attributed_project_id is None
    assert shard.attributed_project_id != "core"
    assert shard.actor_user_id is None
    assert mem_svc._project_of(shard) is None


def test_mapped_ingest_sets_attributed_project_not_collapsed_id(client, auth, db, tmp_path):
    pid = client.post("/api/projects", json={"name": "Mapped Alpha"}, headers=auth).json()["id"]
    _write_repo(tmp_path, pid, "sess-mapped")
    ingest(db, ClaudeCodeAdapter(root=str(tmp_path)), project_id="core")
    shards = _transcript_shards(db)
    assert len(shards) == 1
    shard = shards[0]
    assert shard.project_id == "core"
    assert shard.attributed_project_id == pid
    assert shard.attributed_project_id != "core"
    assert shard.actor_user_id is None


def test_unmatched_nested_dir_stays_null_not_ineligible(client, auth, db, tmp_path):
    """A per-repo folder that does not match a project is unmeasured, not independence 1."""
    mystery = tmp_path / "-Users-test-src-unknown-repo"
    mystery.mkdir()
    (mystery / "s.jsonl").write_text(_line(LESSON, sessionId="sess-mystery"))
    ingest(db, ClaudeCodeAdapter(root=str(tmp_path)), project_id="core")
    shard = _transcript_shards(db)[0]
    assert shard.attributed_project_id is None
    _publish(client, auth, shard.id)
    db.refresh(shard)
    assert shard.actor_user_id == "u1"
    assert shard.attributed_project_id is None
    elig = _elig(client, auth, shard.id)
    assert elig["state"] == "unverifiable"
    assert elig["state"] != "ineligible"
    assert "distinct_projects" in elig["reason"]


def test_ingest_cluster_without_map_stays_unverifiable_not_ineligible(
    client, auth, db, tmp_path,
):
    """Three unmapped ingest+publish rows look like a cluster and still cannot be counted.

    Filling attributed_project_id=project_id would make 1 project × 1 user → ineligible
    (we 'looked'). Unmapped must stay unverifiable.
    """
    for i in range(3):
        (tmp_path / f"s{i}.jsonl").write_text(_line(LESSON, sessionId=f"sess-unmapped-{i}"))
    ingest(db, ClaudeCodeAdapter(root=str(tmp_path)), project_id="core")
    shards = _transcript_shards(db)
    assert len(shards) == 3
    for s in shards:
        assert s.attributed_project_id is None
        _publish(client, auth, s.id)
        db.refresh(s)
        assert s.actor_user_id == "u1"
        assert s.attributed_project_id is None
    elig = _elig(client, auth, shards[0].id)
    assert elig["state"] == "unverifiable"
    assert elig["state"] != "ineligible"
    assert "distinct_projects" in elig["reason"]
    promo = client.post(
        f"/api/memory/lessons/{shards[0].id}/promote-org", json={}, headers=auth,
    )
    assert promo.status_code == 422, promo.text
    assert promo.json()["detail"]["state"] == "unverifiable"


def _ingest_mapped(client, auth, db, tmp_path, names: list[str]) -> list[MemoryShard]:
    ids = [
        client.post("/api/projects", json={"name": n}, headers=auth).json()["id"]
        for n in names
    ]
    for i, pid in enumerate(ids):
        _write_repo(tmp_path, pid, f"sess-map-{i}")
    ingest(db, ClaudeCodeAdapter(root=str(tmp_path)), project_id="core")
    shards = _transcript_shards(db)
    assert len(shards) == len(ids)
    by_attr = {s.attributed_project_id: s for s in shards}
    assert set(by_attr) == set(ids)
    for s in shards:
        _publish(client, auth, s.id)
        db.refresh(s)
        assert s.actor_user_id == "u1"
        assert s.attributed_project_id in ids
        assert s.project_id == "core"
    return shards


def test_mapped_one_user_three_projects_ingest_publish_can_promote(
    client, auth, db, tmp_path,
):
    shards = _ingest_mapped(
        client, auth, db, tmp_path,
        ["Ingest Alpha", "Ingest Beta", "Ingest Gamma"],
    )
    elig = _elig(client, auth, shards[0].id)
    assert elig["state"] == "eligible", elig
    assert elig["cluster_scan"] == "scanned"
    assert elig["distinct_projects"] == 3
    assert elig["distinct_users"] == 1
    assert elig["independence"] == 3
    promo = client.post(
        f"/api/memory/lessons/{shards[0].id}/promote-org", json={}, headers=auth,
    )
    assert promo.status_code == 200, promo.text
    assert promo.json()["reach"] == "org"
    assert promo.json()["transferability"] == "evidenced"


def test_mapped_one_user_two_projects_cannot_promote(client, auth, db, tmp_path):
    shards = _ingest_mapped(
        client, auth, db, tmp_path,
        ["Ingest Delta", "Ingest Epsilon"],
    )
    elig = _elig(client, auth, shards[0].id)
    assert elig["state"] == "ineligible", elig
    assert elig["independence"] == 2
    assert elig["distinct_projects"] == 2
    assert elig["distinct_users"] == 1
    promo = client.post(
        f"/api/memory/lessons/{shards[0].id}/promote-org", json={}, headers=auth,
    )
    assert promo.status_code == 422, promo.text
    assert promo.json()["detail"]["state"] == "ineligible"
    db.refresh(shards[0])
    assert shards[0].reach == "project"


@pytest.mark.parametrize("folder,forbidden", [
    ("acme-core", "core"),
    ("acme-web", "web"),
])
def test_acme_core_and_acme_web_are_not_seed_projects(
    client, db, tmp_path, folder, forbidden,
):
    """Suffix match on the encoded folder would stamp seed `core`/`web`.

    cwd last component is `acme-core` / `acme-web`, which is not those ids.
    """
    _write_repo(tmp_path, folder, f"sess-{folder}")
    ingest(db, ClaudeCodeAdapter(root=str(tmp_path)), project_id="core")
    shards = _transcript_shards(db)
    assert len(shards) == 1
    shard = shards[0]
    assert shard.attributed_project_id is None
    assert shard.attributed_project_id != forbidden
    assert mem_svc._project_of(shard) is None


def test_hosted_ingest_does_not_attribute_a_foreign_org(
    client, auth, db, tmp_path, monkeypatch,
):
    """Attribution is an independence input. A unique name in org B must not
    stamp org A's ingest — that would let a 1×3 of foreign ids fire the formula.
    """
    from app.config import settings
    from app.models import Organization, OrgMembership
    from app.services import projects as proj_svc

    db.add_all([
        Organization(id="orgIngestA", name="Org Ingest A"),
        Organization(id="orgIngestB", name="Org Ingest B"),
    ])
    db.flush()
    a = proj_svc.create_project(
        db, name="Tenant A Ingest", owner_user_id="u1", tag="ITA",
        org_id="orgIngestA",
    )
    b = proj_svc.create_project(
        db, name="Tenant B Unique", owner_user_id="u1", tag="ITB",
        org_id="orgIngestB",
    )
    db.add_all([
        OrgMembership(org_id="orgIngestA", user_id="u1", role="owner"),
        OrgMembership(org_id="orgIngestB", user_id="u1", role="owner"),
    ])
    db.commit()

    monkeypatch.setattr(settings, "hosted_mode", True)
    _write_repo(tmp_path, b.id, "sess-foreign")
    ingest(db, ClaudeCodeAdapter(root=str(tmp_path)), project_id=a.id)
    shards = _transcript_shards(db)
    assert len(shards) == 1
    shard = shards[0]
    assert shard.project_id == a.id
    assert shard.attributed_project_id is None
    assert shard.attributed_project_id != b.id
    _publish(client, auth, shard.id)
    db.refresh(shard)
    elig = _elig(client, auth, shard.id, project_id=a.id)
    assert elig["state"] == "unverifiable"
    assert elig["state"] != "ineligible"
    assert "distinct_projects" in elig["reason"]
    promo = client.post(
        f"/api/memory/lessons/{shard.id}/promote-org", json={}, headers=auth,
    )
    assert promo.status_code == 422, promo.text
    assert promo.json()["detail"]["state"] == "unverifiable"


def test_ambiguous_equal_length_hits_stay_null_not_a_guess(client, auth, db, tmp_path):
    """Two projects named Alpha (ids alpha / alpha-2, both name-slug alpha).

    Returning winners[0] would stamp a guessed id and read as ineligible
    (we 'looked') instead of unverifiable.
    """
    first = client.post("/api/projects", json={"name": "Alpha"}, headers=auth).json()["id"]
    second = client.post("/api/projects", json={"name": "Alpha"}, headers=auth).json()["id"]
    assert first != second
    _write_repo(tmp_path, "alpha", "sess-alpha", cwd="/Users/test/src/alpha")
    ingest(db, ClaudeCodeAdapter(root=str(tmp_path)), project_id="core")
    shards = _transcript_shards(db)
    assert len(shards) == 1
    shard = shards[0]
    assert shard.attributed_project_id is None
    assert shard.attributed_project_id not in {first, second}
    _publish(client, auth, shard.id)
    db.refresh(shard)
    assert shard.attributed_project_id is None
    elig = _elig(client, auth, shard.id)
    assert elig["state"] == "unverifiable"
    assert elig["state"] != "ineligible"
    assert "distinct_projects" in elig["reason"]
