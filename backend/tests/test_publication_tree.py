"""The publication tree (GRPH-1763). The render is deterministic and fails closed.

The push is a different item. These tests never fetch or commit.
"""
import json

from app.hashing import content_hash
from app.services import publication_tree as tree
from app.services.publication_tree import (
    ABSENT_WHY,
    FAILED,
    NOTHING_EARNED,
    RENDERED,
    RULES_UNKNOWN,
    Constraint,
    Earned,
    Lesson,
    Preamble,
    RulesPull,
    Spec,
    render,
    render_deployment,
)

BASE_WITH = """## Problem

Ship the tree.

## Decisions from grilling

- The ledger stays the writer.
"""
BASE_WITHOUT = """## Problem

No grill was recorded.
"""
DRAFT_SENTINEL = "DRAFT-BODY-MUST-NOT-PUBLISH"
LOCAL_SENTINEL = "LOCAL-ONLY-BASELINE-MUST-NOT-PUBLISH"
UNACCEPTED = "UNACCEPTED-CONSTRAINTS-MUST-NOT-PUBLISH"


def _rules(**kwargs) -> RulesPull:
    base = dict(
        state="pulled", version="ver-eq", changed_at="2026-01-02T03:04:05+00:00",
        toggles=({"rule_id": "b", "on": False, "params": {"n": 2}, "locked": False},
                 {"rule_id": "a", "on": True, "params": None, "locked": True}),
        profile={"id": "std", "version": "3"},
    )
    base.update(kwargs)
    return RulesPull(**base)


def _lesson(**kwargs) -> Lesson:
    text = kwargs.pop("text", "Org lessons outlive the project.")
    base = dict(
        shard_id="m_org", reach="org", text=text, content_hash=content_hash(text),
        eligibility_state="promoted", caught_state="unknown", effectiveness_score=None,
        source="lesson from GRPH-1", item_id="GRPH-1", status="published",
    )
    base.update(kwargs)
    return Lesson(**base)


def _spec(**kwargs) -> Spec:
    body = kwargs.pop("baseline_body", BASE_WITH)
    base = dict(
        project_id="pub", prd_id="prd_ok", status="approved", local_only=False,
        has_baseline=True, version="v1.0", baseline_body=body,
        baseline_hash=content_hash(body), draft_body=DRAFT_SENTINEL,
    )
    base.update(kwargs)
    return Spec(**base)


def _earned(**kwargs) -> Earned:
    base = dict(rules=RulesPull(state="not_pulled"))
    base.update(kwargs)
    return Earned(**base)


def _manifest(result):
    return json.loads(result.files["manifest.json"])


def _joined(result) -> str:
    return "\n".join(result.files.values())


def test_two_renders_of_the_same_rows_are_the_same_bytes():
    earned = _earned(
        rules=_rules(),
        lessons=(_lesson(), _lesson(shard_id="m_b", text="Second.")),
        specs=(_spec(), _spec(prd_id="prd_bare", baseline_body=BASE_WITHOUT,
                              baseline_hash=content_hash(BASE_WITHOUT))),
        preambles=(Preamble("pub", 4, "Stand here.\n", content_hash("Stand here.\n")),),
        constraints=(Constraint("pub", "DS-1", "Use the type.\n",
                                content_hash("Use the type.\n"), accepted=True),),
    )
    first, second = render(earned), render(earned)
    assert first.result == RENDERED and second.result == RENDERED
    assert first.files == second.files
    assert first.manifest_hash == second.manifest_hash
    assert first.manifest_hash == content_hash(first.files["manifest.json"])
    row = next(a for a in _manifest(first)["artifacts"] if a["kind"] == "spec" and a["id"] == "prd_ok")
    assert row["content_hash"] == content_hash(BASE_WITH)
    assert row["version"] == "v1.0"


def test_nothing_earned_writes_no_manifest():
    got = render(_earned(
        lessons=(_lesson(reach="project"), _lesson(status="candidate")),
        specs=(_spec(local_only=True, baseline_body=LOCAL_SENTINEL,
                     baseline_hash=content_hash(LOCAL_SENTINEL)),
               _spec(status="draft"),
               _spec(prd_id="prd_none", has_baseline=False, baseline_body="", baseline_hash="")),
        constraints=(Constraint("pub", "DS-open", UNACCEPTED, content_hash(UNACCEPTED), accepted=False),),
    ))
    assert got.result == NOTHING_EARNED
    assert got.files == {}
    assert got.manifest_hash is None


def test_failed_pull_writes_no_tree_and_does_not_copy_the_last_decisions():
    previous = {"rules/decisions.json": '{"version":"last-good"}\n'}
    got = render(
        _earned(rules=RulesPull(state="unknown", reason="timeout"), lessons=(_lesson(),)),
        previous=previous,
    )
    assert got.result == RULES_UNKNOWN
    assert got.reason == "timeout"
    assert got.files == {}
    assert previous["rules/decisions.json"] not in got.files.values()


def test_a_blank_pull_failure_is_named_rather_than_silent():
    got = render(_earned(rules=RulesPull(state="unknown", reason="  ")))
    assert got.result == RULES_UNKNOWN
    assert got.reason == "unspecified"
    assert got.files == {}


def test_an_unclassified_pull_state_does_not_render_as_not_pulled():
    got = render(_earned(rules=RulesPull(state=""), lessons=(_lesson(),)))
    assert got.result == RULES_UNKNOWN
    assert got.files == {}


def test_not_published_is_a_manifest_entry_and_not_a_decisions_file():
    previous = {"rules/decisions.json": '{"version":"stale"}\n'}
    got = render(_earned(rules=RulesPull(state="not_published")), previous=previous)
    assert got.result == RENDERED
    assert list(got.files) == ["manifest.json"]
    assert _manifest(got)["rules"] == "not_published"
    assert _manifest(got)["artifacts"] == []
    assert "stale" not in got.files["manifest.json"]


def test_not_pulled_with_other_rows_names_the_entry_and_writes_no_decisions_file():
    got = render(_earned(lessons=(_lesson(),)))
    assert got.result == RENDERED
    assert "rules/decisions.json" not in got.files
    assert _manifest(got)["rules"] == "not_pulled"
    assert "lessons/m_org.md" in got.files


def test_approved_baseline_is_published_and_the_draft_body_is_not():
    bare = _spec(prd_id="prd_bare", baseline_body=BASE_WITHOUT,
                 baseline_hash=content_hash(BASE_WITHOUT))
    draft = _spec(prd_id="prd_draft", status="draft", baseline_body="SHOULD-NOT-DRAFT",
                  baseline_hash=content_hash("SHOULD-NOT-DRAFT"), draft_body=DRAFT_SENTINEL)
    nobase = _spec(prd_id="prd_nobase", has_baseline=False, baseline_body="", baseline_hash="",
                   draft_body="NO-BASELINE")
    got = render(_earned(rules=_rules(), specs=(_spec(), bare, draft, nobase)))
    assert got.result == RENDERED
    spec = got.files["projects/pub/specs/prd_ok.md"]
    assert "Ship the tree." in spec
    assert DRAFT_SENTINEL not in _joined(got)
    assert "SHOULD-NOT-DRAFT" not in _joined(got)
    assert "NO-BASELINE" not in _joined(got)
    ids = {a["id"] for a in _manifest(got)["artifacts"]}
    assert "prd_ok" in ids and "prd_draft" not in ids and "prd_nobase" not in ids
    front = spec.split("---\n", 2)[1]
    assert content_hash(BASE_WITH) in front
    assert "v1.0" in front
    assert "approved" in front


def test_project_local_lesson_has_no_file_and_an_org_lesson_does():
    text = "Only the org hears this."
    got = render(_earned(lessons=(
        _lesson(text=text),
        _lesson(shard_id="m_local", reach="project", text="PROJECT-LOCAL-LESSON"),
    )))
    assert got.result == RENDERED
    assert "lessons/m_org.md" in got.files
    assert "lessons/m_local.md" not in got.files
    assert "PROJECT-LOCAL-LESSON" not in _joined(got)
    fm, body = _split(got.files["lessons/m_org.md"])
    assert fm["reach"] == "org"
    assert fm["eligibility.state"] == "promoted"
    assert fm["caught_state"] == "unknown"
    assert fm["effectiveness.score"] is None
    assert fm["source"] == "lesson from GRPH-1"
    assert fm["item_id"] == "GRPH-1"
    assert text in body
    row = next(a for a in _manifest(got)["artifacts"] if a["kind"] == "lesson")
    assert row["version"] is None


def test_missing_eligibility_state_fails_and_writes_nothing():
    got = render(_earned(rules=_rules(), lessons=(_lesson(eligibility_state=""),)))
    assert got.result == FAILED
    assert got.reason == "eligibility_missing"
    assert got.files == {}


def test_dropping_eligibility_state_from_the_file_fails(monkeypatch):
    real = tree._lesson_fields

    def drop(lesson):
        return [pair for pair in real(lesson) if pair[0] != "eligibility.state"]

    monkeypatch.setattr(tree, "_lesson_fields", drop)
    got = render(_earned(lessons=(_lesson(),)))
    assert got.result == FAILED
    assert got.reason == "eligibility_missing"
    assert got.files == {}


def test_a_changed_body_with_the_old_hash_fails_and_writes_nothing():
    got = render(_earned(specs=(_spec(baseline_hash=content_hash("the old body")),)))
    assert got.result == FAILED
    assert got.reason == "hash_mismatch"
    assert got.files == {}


def test_omitting_an_earned_row_fails_and_writes_nothing(monkeypatch):
    real = tree._write

    def drop(planned):
        files = real(planned)
        return {path: text for path, text in files.items() if not path.startswith("lessons/")}

    monkeypatch.setattr(tree, "_write", drop)
    got = render(_earned(lessons=(_lesson(),), specs=(_spec(),)))
    assert got.result == FAILED
    assert got.reason == "omitted_row"
    assert got.files == {}


def test_a_baseline_without_grill_decisions_still_has_a_why_file():
    got = render(_earned(specs=(_spec(baseline_body=BASE_WITHOUT,
                                      baseline_hash=content_hash(BASE_WITHOUT)),)))
    assert got.result == RENDERED
    why = got.files["projects/pub/why/prd_ok.md"]
    assert ABSENT_WHY.strip() in why
    row = next(a for a in _manifest(got)["artifacts"] if a["kind"] == "why")
    assert row["grill_decisions"] == "absent"
    with_section = render(_earned(specs=(_spec(),)))
    present = next(a for a in _manifest(with_section)["artifacts"] if a["kind"] == "why")
    assert present["grill_decisions"] == "present"
    assert "The ledger stays the writer." in with_section.files["projects/pub/why/prd_ok.md"]
    assert "Ship the tree." not in with_section.files["projects/pub/why/prd_ok.md"]


def test_local_only_project_has_no_directory_and_rules_and_lessons_remain():
    got = render(_earned(
        rules=_rules(),
        lessons=(_lesson(),),
        specs=(_spec(project_id="loc", local_only=True, baseline_body=LOCAL_SENTINEL,
                    baseline_hash=content_hash(LOCAL_SENTINEL)),
               _spec()),
        preambles=(
            Preamble("loc", 1, "LOCAL-PREAMBLE", content_hash("LOCAL-PREAMBLE"), local_only=True),
            Preamble("pub", 2, "Org preamble.\n", content_hash("Org preamble.\n")),
        ),
        constraints=(
            Constraint("loc", "DS-loc", "LOCAL-CONSTRAINT", content_hash("LOCAL-CONSTRAINT"),
                       accepted=True, local_only=True),
            Constraint("pub", "DS-1", "Accepted.\n", content_hash("Accepted.\n"), accepted=True),
            Constraint("pub", "DS-open", UNACCEPTED, content_hash(UNACCEPTED), accepted=False),
        ),
    ))
    assert got.result == RENDERED
    assert not any(path.startswith("projects/loc/") for path in got.files)
    assert LOCAL_SENTINEL not in _joined(got)
    assert "LOCAL-PREAMBLE" not in _joined(got)
    assert "LOCAL-CONSTRAINT" not in _joined(got)
    assert UNACCEPTED not in _joined(got)
    assert "rules/decisions.json" in got.files
    assert "lessons/m_org.md" in got.files
    assert "projects/pub/specs/prd_ok.md" in got.files
    assert "projects/pub/preamble.md" in got.files
    assert "projects/pub/constraints/DS-1.md" in got.files
    decisions = json.loads(got.files["rules/decisions.json"])
    assert decisions["version"] == "ver-eq"
    assert [t["rule_id"] for t in decisions["toggles"]] == ["a", "b"]
    assert {"toggles", "params", "locks", "profile", "version", "changed_at"} <= set(decisions)
    assert _manifest(got).get("rules") is None
    preamble = got.files["projects/pub/preamble.md"]
    assert '"version": 2' in preamble or "\nversion: 2\n" in preamble


def test_rule_version_is_copied_for_equality_and_not_recomputed():
    got = render(_earned(rules=_rules(version="9")))
    body = json.loads(got.files["rules/decisions.json"])
    assert body["version"] == "9"
    later = render(_earned(rules=_rules(version="10")))
    assert json.loads(later.files["rules/decisions.json"])["version"] == "10"


def _split(text: str) -> tuple[dict, str]:
    from app.services.publication_tree import _parse

    fm, body = _parse(text)
    assert fm is not None
    return fm, body


def test_an_unreadable_row_fails_closed():
    got = render(_earned(unreadable=("preamble:pub:unreadable",), lessons=(_lesson(),)))
    assert got.result == FAILED
    assert got.reason == "preamble:pub:unreadable"
    assert got.files == {}


def test_render_deployment_of_an_empty_ledger_is_nothing_earned():
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        got = render_deployment(db)
    finally:
        db.close()
    assert got.result == NOTHING_EARNED
    assert got.files == {}


def test_loader_reads_earned_rows_and_skips_what_has_not_earned():
    from datetime import datetime, timezone

    from app.db import SessionLocal
    from app.models import (
        DesignSet, Document, DocumentChunk, LessonOutcome, MemoryShard, Organization,
        OrgRuleVersion, Prd, PrdVersion, Project, RuleDecisionPull, RuleToggle, SyncProject,
    )

    db = SessionLocal()
    try:
        db.add(Organization(id="org_1", name="Acme"))
        db.add(Project(id="pub", name="Pub", tag="PUB"))
        db.add(Project(id="loc", name="Loc", tag="LOC"))
        # Parents first. A single flush inserts memory_shards ahead of projects
        # when the schema cycle is present, and SQLite then refuses the shard.
        db.flush()
        db.add(SyncProject(local_project_id="pub"))
        db.add(Prd(id="prd_ok", project_id="pub", number=1, title="Ok", status="approved",
                   version="v0.1", body=DRAFT_SENTINEL))
        db.add(PrdVersion(prd_id="prd_ok", version="v1.0", body=BASE_WITH, is_baseline=True))
        db.add(Prd(id="prd_draft", project_id="pub", number=2, title="Draft", status="draft",
                   body="SHOULD-NOT-DRAFT"))
        db.add(PrdVersion(prd_id="prd_draft", version="v0.1", body="SHOULD-NOT-DRAFT",
                          is_baseline=True))
        db.add(Prd(id="prd_nobase", project_id="pub", number=3, title="No base",
                   status="approved", body="NO-BASELINE"))
        db.add(Prd(id="prd_loc", project_id="loc", number=1, title="Local", status="approved",
                   body=LOCAL_SENTINEL))
        db.add(PrdVersion(prd_id="prd_loc", version="v1.0", body=LOCAL_SENTINEL, is_baseline=True))
        org = MemoryShard(id="m_org", project_id="pub", text="Count the catch.", reach="org",
                          status="published", source="lesson from the loader", item_id=None)
        local = MemoryShard(id="m_local", project_id="pub", text="PROJECT-LOCAL-LESSON",
                            reach="project", status="published", source="local")
        candidate = MemoryShard(id="m_cand", project_id="pub", text="CANDIDATE-LESSON",
                                reach="org", status="candidate", source="candidate")
        db.add_all([org, local, candidate])
        db.flush()
        db.add(LessonOutcome(shard_id="m_org", kind="caught", source="human"))
        pre_body = "Stand here."
        doc = Document(id="doc_pre", project_id="pub", kind="preamble", title="Preamble",
                       content_hash=content_hash(pre_body), version=4, governing=True,
                       status="active")
        doc.chunks.append(DocumentChunk(
            project_id="pub", kind="preamble", ordinal=0, heading="", text=pre_body,
            content_hash=content_hash(pre_body), extractor_version="t", governing=True,
        ))
        db.add(doc)
        db.add(Document(
            id="doc_loc", project_id="loc", kind="preamble", title="Local",
            content_hash=content_hash("LOCAL-PREAMBLE"), version=1, governing=True,
            status="active",
        ))
        db.add(DesignSet(id="DS-1", project_id="pub", prd_id="prd_ok", status="accepted",
                         constraints_body="Accepted.\n",
                         constraints_hash=content_hash("Accepted.\n"),
                         constraints_accepted=True))
        db.add(DesignSet(id="DS-open", project_id="pub", prd_id="prd_ok", status="open",
                         constraints_body=UNACCEPTED, constraints_hash=content_hash(UNACCEPTED),
                         constraints_accepted=False))
        db.add(DesignSet(id="DS-loc", project_id="loc", prd_id="prd_loc", status="accepted",
                         constraints_body="LOCAL-CONSTRAINT",
                         constraints_hash=content_hash("LOCAL-CONSTRAINT"),
                         constraints_accepted=True))
        db.add(RuleDecisionPull(
            id="instance", org_id="org_1", applied_version="ver-ledger",
            applied_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
            profile="std", profile_version="3",
        ))
        db.add(OrgRuleVersion(
            org_id="org_1", version="ver-ledger",
            changed_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        ))
        db.add(RuleToggle(rule_id="a", org_id="org_1", on=True, params={"n": 1}, locked=True))
        db.commit()

        got = render_deployment(db)
        assert got.result == RENDERED
        assert "projects/pub/specs/prd_ok.md" in got.files
        assert "Ship the tree." in got.files["projects/pub/specs/prd_ok.md"]
        assert DRAFT_SENTINEL not in _joined(got)
        assert "SHOULD-NOT-DRAFT" not in _joined(got)
        assert "NO-BASELINE" not in _joined(got)
        assert "prd_draft" not in {a["id"] for a in _manifest(got)["artifacts"]}
        assert "prd_nobase" not in {a["id"] for a in _manifest(got)["artifacts"]}
        assert not any(path.startswith("projects/loc/") for path in got.files)
        assert LOCAL_SENTINEL not in _joined(got)
        assert "LOCAL-CONSTRAINT" not in _joined(got)
        assert UNACCEPTED not in _joined(got)
        assert "PROJECT-LOCAL-LESSON" not in _joined(got)
        assert "CANDIDATE-LESSON" not in _joined(got)
        fm, _body = _split(got.files["lessons/m_org.md"])
        assert fm["eligibility.state"] == "promoted"
        assert fm["caught_state"] == "caught"
        assert fm["effectiveness.score"] == 1.0
        assert "projects/pub/preamble.md" in got.files
        assert _split(got.files["projects/pub/preamble.md"])[0]["version"] == 4
        assert "projects/pub/constraints/DS-1.md" in got.files
        decisions = json.loads(got.files["rules/decisions.json"])
        assert decisions["version"] == "ver-ledger"
        assert decisions["changed_at"] == "2026-01-02T03:04:05+00:00"
        assert decisions["toggles"] == [{"on": True, "rule_id": "a"}]
        assert decisions["locks"] == [{"locked": True, "rule_id": "a"}]
        assert decisions["profile"] == {"id": "std", "version": "3"}

        again = render_deployment(db)
        assert again.files == got.files

        withheld = render_deployment(
            db, rules=RulesPull(state="unknown", reason="connection reset"),
            previous={"rules/decisions.json": got.files["rules/decisions.json"]},
        )
        assert withheld.result == RULES_UNKNOWN
        assert withheld.reason == "connection reset"
        assert withheld.files == {}

        unpublished = render_deployment(db, rules=RulesPull(state="not_published"))
        assert unpublished.result == RENDERED
        assert "rules/decisions.json" not in unpublished.files
        assert _manifest(unpublished)["rules"] == "not_published"
        assert "projects/pub/specs/prd_ok.md" in unpublished.files
    finally:
        db.close()
