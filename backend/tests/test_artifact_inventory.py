"""Telemetry that can see artifacts Graphban did not write (GRPH-354 / PRD-16).

`usage_report` and `stale_artifacts` read `ArtifactRecommendation`, which only ever holds
what this pipeline generated — so the retirement half was measuring its own footprint. A
fresh install reported a population of zero while the operator's `.claude/` directory held
dozens of real artifacts, and those are the ones spending context on every turn.

Three properties carry the whole feature, and each is tested by breaking it:

- **Read-only.** Not asserted in a docstring — proven, by recording every file's contents and
  mtime before a scan and comparing after.
- **Fork detection.** A generated artifact a human has edited must never be re-rendered:
  `install_plan` updates in FULL, so re-rendering silently discards their edit.
- **Orphan detection is scoped per root.** A scan of one root must not mark another root's
  artifacts missing, which would be an absence read as a finding.

And one that is easy to get backwards: a DISCOVERED artifact is never measurable, whatever
its tier. Graphban meters its own MCP calls and instruments the hooks it renders; it has no
instrumentation inside a hand-written skill, so `uses: 0` there would be a measurement nobody
took — and zero uses on a measurable tier is what queues a delete.
"""
import pytest

from app.models import ArtifactRecommendation
from app.services import artifact_inventory as inv_svc
from app.services import artifacts as art_svc


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def proj(db):
    from app.models import Project

    db.add(Project(id="inv", name="Inventory", tag="IV"))
    db.commit()
    return "inv"


@pytest.fixture()
def tree(tmp_path):
    """A `.claude` directory shaped like a real one, with one of each tier."""
    root = tmp_path / ".claude"
    (root / "skills" / "deploy").mkdir(parents=True)
    (root / "skills" / "deploy" / "SKILL.md").write_text("---\nname: deploy\n---\nSteps.")
    (root / "agents").mkdir()
    (root / "agents" / "reviewer.md").write_text("---\nname: reviewer\n---\nPrompt.")
    (root / "hooks").mkdir()
    (root / "hooks" / "guard.sh").write_text("#!/bin/sh\necho guard\n")
    (root / "rules").mkdir()
    (root / "rules" / "house.md").write_text("Always run the migration guard.")
    # Noise that must NOT be inventoried — a scan that hoovered these up would report a
    # population made mostly of logs.
    (root / "settings.json").write_text("{}")
    (root / "projects").mkdir()
    (root / "projects" / "session.jsonl").write_text('{"type":"user"}\n')
    return root


def _snapshot(root):
    return {str(p): (p.stat().st_mtime_ns, p.read_bytes())
            for p in sorted(root.rglob("*")) if p.is_file()}


# ---- the scan ----------------------------------------------------------------------------

def test_a_scan_finds_every_tier(tree):
    found, stats = inv_svc.scan([str(tree)])

    tiers = {d.tier for d in found}
    assert tiers == {"skill", "agent", "hook", "rule"}
    assert stats["files"] == 4


def test_a_scan_ignores_files_that_are_not_artifacts(tree):
    """`settings.json` and a transcript are not artifacts. Counting them would inflate the
    population figure the retirement half reasons about."""
    found, _ = inv_svc.scan([str(tree)])

    names = {d.path.rsplit("/", 1)[-1] for d in found}
    assert "settings.json" not in names and "session.jsonl" not in names


def test_a_scan_writes_nothing_moves_nothing_deletes_nothing(tree):
    """THE constraint, proven rather than asserted. PRD-16 is explicit that the inventory is
    read-only, and a scan that touched a human's `.claude/` even once is a feature nobody
    would leave enabled."""
    before = _snapshot(tree)

    inv_svc.scan([str(tree)])

    assert _snapshot(tree) == before


def test_a_missing_root_is_not_an_error(tmp_path):
    """An operator points this at a path that does not exist on this machine. That is a
    normal Tuesday, not a crash."""
    found, stats = inv_svc.scan([str(tmp_path / "nope")])
    assert found == [] and stats["roots"] == 0


def test_the_same_file_under_two_roots_is_counted_once(tree):
    found, _ = inv_svc.scan([str(tree), str(tree.parent)])
    assert len({d.path for d in found}) == len(found)


def test_a_trailing_newline_is_not_a_fork(tree):
    """A hash that fired on a final newline would flag every artifact an editor touched, and
    a fork warning that cries wolf stops protecting the edits it exists to protect."""
    assert inv_svc.content_hash("body") == inv_svc.content_hash("body\n")


# ---- recording, and what the states mean --------------------------------------------------

def test_recording_a_scan_makes_hand_written_artifacts_visible(db, proj, tree):
    found, _ = inv_svc.scan([str(tree)])

    stats = inv_svc.record_scan(db, project_id=proj, root=str(tree),
                                items=[d.as_dict() for d in found])

    assert stats["added"] == 4
    assert len(inv_svc.inventory(db, proj)) == 4


def test_a_file_that_disappears_is_orphaned_never_deleted(db, proj, tree):
    found, _ = inv_svc.scan([str(tree)])
    inv_svc.record_scan(db, project_id=proj, root=str(tree),
                        items=[d.as_dict() for d in found])
    (tree / "hooks" / "guard.sh").unlink()

    again, _ = inv_svc.scan([str(tree)])
    stats = inv_svc.record_scan(db, project_id=proj, root=str(tree),
                                items=[d.as_dict() for d in again])

    assert stats["orphaned"] == 1
    rows = {r.path.rsplit("/", 1)[-1]: r for r in inv_svc.inventory(db, proj)}
    assert rows["guard.sh"].state == "orphaned", "flagged"
    assert len(rows) == 4, "and still present as a row — never hard-deleted"


def test_scanning_one_root_does_not_orphan_another(db, proj, tree, tmp_path):
    """A scan of `~/.claude` says nothing about `~/work/.cursor`. Marking those missing
    because this pass never looked there is an absence read as a finding — the defect class
    this whole feature exists to close, and the easiest place to reintroduce it."""
    other = tmp_path / "work" / ".cursor"
    (other / "rules").mkdir(parents=True)
    (other / "rules" / "style.mdc").write_text("Prefer explicit imports.")
    for root in (tree, other):
        found, _ = inv_svc.scan([str(root)])
        inv_svc.record_scan(db, project_id=proj, root=str(root),
                            items=[d.as_dict() for d in found])

    found, _ = inv_svc.scan([str(tree)])
    inv_svc.record_scan(db, project_id=proj, root=str(tree),
                        items=[d.as_dict() for d in found])

    rows = {r.path.rsplit("/", 1)[-1]: r for r in inv_svc.inventory(db, proj)}
    assert rows["style.mdc"].state == "present", "a root nobody scanned is not a root that is gone"


# ---- fork detection, and the install refusal it exists for ---------------------------------

def _generated(db, project_id, path, contents):
    rec = ArtifactRecommendation(
        project_id=project_id, tier="skill", scope="deploy", title="Deploy",
        lesson_ids=[], status="approved", draft=contents, draft_path=path,
        install_class="file_additive")
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


def test_an_untouched_generated_artifact_is_not_forked(db, proj, tree):
    path = ".claude/skills/deploy/SKILL.md"
    on_disk = (tree / "skills" / "deploy" / "SKILL.md").read_text()
    rec = _generated(db, proj, path, on_disk)

    found, _ = inv_svc.scan([str(tree)])
    stats = inv_svc.record_scan(db, project_id=proj, root=str(tree),
                                items=[d.as_dict() for d in found])

    # The match is asserted FIRST, and it is the whole point of the test. Without this the
    # test passes just as well when `_match_generated` never matches anything at all —
    # nothing forked, nothing refused, and fork detection entirely broken.
    row = next(r for r in inv_svc.inventory(db, proj) if r.path.endswith("SKILL.md"))
    assert row.recommendation_id == rec.id, "the file was recognised as this generated artifact"
    assert stats["forked"] == 0
    assert inv_svc.fork_of(db, rec) is None
    assert art_svc.install_plan(db, rec)["allowed"] is True


def test_a_hand_edited_generated_artifact_is_flagged_forked(db, proj, tree):
    path = ".claude/skills/deploy/SKILL.md"
    rec = _generated(db, proj, path, "---\nname: deploy\n---\nWhat we rendered.")

    found, _ = inv_svc.scan([str(tree)])
    stats = inv_svc.record_scan(db, project_id=proj, root=str(tree),
                                items=[d.as_dict() for d in found])

    assert stats["forked"] == 1
    assert inv_svc.fork_of(db, rec) is not None


def test_install_refuses_to_re_render_a_forked_artifact(db, proj, tree):
    """THE acceptance criterion, and the reason fork detection is worth building at all.

    `install_plan` updates machine-owned artifacts by FULL re-render, never by patch. Writing
    over an artifact a human has edited discards their work and reports success — the exact
    trust failure the propose-only boundary exists to prevent, arriving through the one path
    that was allowed to write.
    """
    path = ".claude/skills/deploy/SKILL.md"
    rec = _generated(db, proj, path, "---\nname: deploy\n---\nWhat we rendered.")
    assert art_svc.install_plan(db, rec)["allowed"] is True, "allowed before the fork is known"

    found, _ = inv_svc.scan([str(tree)])
    inv_svc.record_scan(db, project_id=proj, root=str(tree),
                        items=[d.as_dict() for d in found])

    plan = art_svc.install_plan(db, rec)
    assert plan["allowed"] is False
    assert "edited by hand" in plan["reason"]
    assert plan["contents"] == rec.draft, "still handed back so it can be reconciled by hand"


# ---- the usage report -------------------------------------------------------------------

def test_discovered_artifacts_appear_in_the_population(db, proj, tree):
    """The defect in one assertion: before this, a directory full of artifacts reported a
    population of zero."""
    assert art_svc.usage_report(db, proj)["population"] == 0

    found, _ = inv_svc.scan([str(tree)])
    inv_svc.record_scan(db, project_id=proj, root=str(tree),
                        items=[d.as_dict() for d in found])

    report = art_svc.usage_report(db, proj)
    assert report["population"] == 4 and report["discovered"] == 4


def test_a_discovered_artifact_is_never_measurable(db, proj, tree):
    """Not even a skill or a hook, whose GENERATED counterparts are measurable. The signal
    comes from instrumentation Graphban puts there, and a hand-written artifact has none — so
    `uses` must stay null. A zero would be a measurement nobody took, and zero uses on a
    measurable tier is what queues a delete."""
    found, _ = inv_svc.scan([str(tree)])
    inv_svc.record_scan(db, project_id=proj, root=str(tree),
                        items=[d.as_dict() for d in found])

    report = art_svc.usage_report(db, proj)
    discovered = [a for a in report["artifacts"] if a["origin"] == "discovered"]
    assert {a["tier"] for a in discovered} >= {"skill", "hook"}
    assert all(a["uses"] is None and a["measurable"] is False for a in discovered)


def test_a_discovered_artifact_is_never_stale_and_never_retired(db, proj, tree):
    """Staleness is a claim about observed disuse. Nothing about a discovered artifact is
    observed, so it can never enter the retirement queue however long it sits there."""
    found, _ = inv_svc.scan([str(tree)])
    inv_svc.record_scan(db, project_id=proj, root=str(tree),
                        items=[d.as_dict() for d in found])

    assert art_svc.stale_artifacts(db, proj) == []


def test_a_generated_artifact_is_not_double_counted(db, proj, tree):
    """It is one artifact whether or not a scan found it on disk. Counting it twice would
    inflate the population the retirement half reasons about."""
    on_disk = (tree / "skills" / "deploy" / "SKILL.md").read_text()
    _generated(db, proj, ".claude/skills/deploy/SKILL.md", on_disk)
    found, _ = inv_svc.scan([str(tree)])
    inv_svc.record_scan(db, project_id=proj, root=str(tree),
                        items=[d.as_dict() for d in found])

    report = art_svc.usage_report(db, proj)
    paths = [a["path"] for a in report["artifacts"]]
    assert report["population"] == 4, "3 discovered + 1 generated, not 5"
    assert len(paths) == len(set(paths))


# ---- the HTTP surface ---------------------------------------------------------------------

def test_the_route_records_a_posted_scan(client, tree):
    """The client-side path: the scan runs where the files are and posts its findings up.

    A server-side walk would find nothing under `hosted_mode` and nothing inside the compose
    container either — and would report a population of zero without erroring.
    """
    login = client.post("/api/auth/login",
                        json={"email": "alex@ascme-labs.com", "password": "graphban"})
    auth = {"Authorization": f"Bearer {login.json()['access_token']}"}
    raw = client.post("/api/api-keys", json={"name": "scanner"},
                      headers=auth).json()["plaintext"]
    found, _ = inv_svc.scan([str(tree)])

    r = client.post("/api/artifacts/inventory",
                    json={"root": str(tree), "items": [d.as_dict() for d in found],
                          "project_id": "core"},
                    headers={"X-API-Key": raw})

    assert r.status_code == 200, r.text
    assert r.json()["added"] == 4
    listed = client.get("/api/artifacts/inventory?project_id=core", headers=auth)
    assert listed.status_code == 200 and len(listed.json()) == 4


def test_the_route_requires_a_root(client):
    """Without one there is no scope for orphaning, and a scan that orphaned everything is a
    scan that reports the operator's whole toolkit missing."""
    login = client.post("/api/auth/login",
                        json={"email": "alex@ascme-labs.com", "password": "graphban"})
    auth = {"Authorization": f"Bearer {login.json()['access_token']}"}
    raw = client.post("/api/api-keys", json={"name": "scanner2"},
                      headers=auth).json()["plaintext"]

    r = client.post("/api/artifacts/inventory", json={"root": "", "items": []},
                    headers={"X-API-Key": raw})

    assert r.status_code == 422


def test_the_route_refuses_an_anonymous_caller(client):
    r = client.post("/api/artifacts/inventory", json={"root": "/x", "items": []})
    assert r.status_code == 401
