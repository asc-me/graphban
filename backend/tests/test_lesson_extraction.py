"""Lessons extracted at item close: what they are distilled FROM, and what may publish them
(GRPH-358 / PRD-16).

Closing GRPH-354 auto-published this, on the same day the opposite shipped:

    "Discovered artifacts inherit the existing MEASURABLE_TIERS/UNMEASURABLE_TIERS logic;
     measurable ones contribute to usage_report..."

A discovered artifact is never measurable, whatever its tier — `usage_report` says so and
`test_a_discovered_artifact_is_never_measurable` pins it. The lesson was drawn from the
item's DESCRIPTION, which was written before the work and still held the original proposal;
the build revised it and nothing updated the description, because nothing needed to.

Two fixes with very different strength, and the tests are split to keep that visible:

- **The guarantee** — an extraction-derived shard may be auto-rejected but never
  auto-published, in any mode. Independent of model quality.
- **The improvement** — the extractor is handed the outcome first and the proposal second,
  labelled as possibly stale. Whether a model honours that depends on the model.

The narrowness test matters as much as the veto itself: an ordinary agent write must still
auto-publish exactly as before, or this "fix" has quietly disabled AL-227.
"""
import pytest

from app.services import insights as insights_svc
from app.services import memory as mem_svc
from tests import attest


def _mcp(client, key, tool, args):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args}},
        headers={"X-API-Key": key},
    ).json()["result"]["structuredContent"]


def _key(client, auth, **body):
    return client.post("/api/api-keys", json={"name": "mem", **body},
                       headers=auth).json()["plaintext"]


def _proj(client, auth, name):
    return client.post("/api/projects", json={"name": name}, headers=auth).json()["id"]


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


class _Item:
    """Just the fields the extraction source reads."""

    def __init__(self, description="", evidence=None, status="done",
                 github_url="", pr=None):
        self.title = "Some item"
        self.description = description
        self.evidence = evidence or []
        self.status = status
        self.github_url = github_url
        self.pr = pr


# ---- the guarantee: nothing publishes an extracted lesson without a human ----------------

def test_an_extracted_lesson_is_never_auto_published(client, auth, db):
    """The defect, closed. Recurrence lifts an ordinary agent shard over the auto-publish bar
    by its third identical write; an extraction-derived one must stay in the queue however
    confident the scorer becomes."""
    pid = _proj(client, auth, "ExtractAuto")
    client.patch(f"/api/projects/{pid}", json={"memory_write_mode": "auto"}, headers=auth)
    text = "always set a timeout on outbound http"

    statuses = [
        mem_svc.add_memory(db, text_body=text, scope="item", project_id=pid,
                           source="lesson from X-1", status="candidate",
                           origin="agent:auto-extract").status
        for _ in range(3)
    ]

    assert statuses[-1] == "candidate", "recurrence must not publish an extracted lesson"


def test_an_ordinary_agent_write_still_auto_publishes(client, auth):
    """The narrowness check, and the reason it is not optional: the veto keys on ORIGIN, and
    a version that keyed on anything broader would have switched AL-227 off for everything
    while every test above still passed."""
    pid = _proj(client, auth, "ExtractNarrow")
    client.patch(f"/api/projects/{pid}", json={"memory_write_mode": "auto"}, headers=auth)
    key = _key(client, auth, project_id=pid)
    text = "always set a timeout on outbound http"

    statuses = [_mcp(client, key, "add_memory", {"text": text})["status"] for _ in range(3)]

    assert statuses[-1] == "published"


def test_trusted_mode_does_not_publish_an_extracted_lesson(client, auth, db):
    """`trusted` (AL-280) exists so an agent can read back what it just wrote inside the same
    task. An extracted lesson has no such consumer — nobody is waiting on it — and it is the
    one write on that path whose source text is known to go stale."""
    pid = _proj(client, auth, "ExtractTrusted")
    client.patch(f"/api/projects/{pid}", json={"memory_write_mode": "trusted"}, headers=auth)

    shard = mem_svc.add_memory(db, text_body="a novel extracted claim about tiers",
                               scope="item", project_id=pid, source="lesson from X-2",
                               status="candidate", origin="agent:auto-extract")

    assert shard.status == "candidate"


def test_trusted_mode_still_publishes_an_ordinary_agent_write(client, auth):
    """Same narrowness check for the other publish path."""
    pid = _proj(client, auth, "TrustedNarrow")
    client.patch(f"/api/projects/{pid}", json={"memory_write_mode": "trusted"}, headers=auth)
    key = _key(client, auth, project_id=pid)

    s = _mcp(client, key, "add_memory", {"text": "a novel note written by an agent"})

    assert s["status"] == "published"


def test_an_extracted_lesson_can_still_be_auto_rejected(client, auth, db):
    """The veto is publish-only, and shaped that way on purpose: it can withhold an accept,
    never create one. Near-duplicate cleanup is safe here and keeps the review queue
    readable — withholding a publish is not a reason to stop discarding restatements."""
    pid = _proj(client, auth, "ExtractReject")
    key = _key(client, auth, project_id=pid)
    pub = _mcp(client, key, "add_memory", {"text": "prefer idempotency keys on writes"})
    client.post(f"/api/memory/shards/{pub['id']}/publish", headers=auth)

    dup = mem_svc.add_memory(db, text_body="prefer idempotency keys on writes", scope="item",
                             project_id=pid, source="lesson from X-3", status="candidate",
                             origin="agent:auto-extract")

    assert dup.status == "rejected"


def test_the_veto_is_expressed_as_a_predicate_on_origin(db):
    """Unit-level, so the rule is readable without standing a project up — and so a future
    origin has to be classified deliberately rather than inheriting publishability."""
    from app.models import MemoryShard

    assert mem_svc.may_auto_publish(MemoryShard(origin="agent:auto-extract")) is False
    assert mem_svc.may_auto_publish(MemoryShard(origin="agent:eval-sample")) is False
    assert mem_svc.may_auto_publish(MemoryShard(origin="agent:some-key")) is True
    assert mem_svc.may_auto_publish(MemoryShard(origin="user:alex")) is True
    assert mem_svc.may_auto_publish(MemoryShard(origin="")) is True


# ---- the improvement: distil from the outcome, not the proposal --------------------------

def test_the_outcome_comes_before_the_proposal(db):
    """Order, not just presence. A truncating model sees the beginning of what it is given,
    so the record of what happened has to arrive before the plan that may have been reversed."""
    item = _Item(description="We will make discovered artifacts inherit the per-tier rule.",
                 evidence=[{"kind": "test", "detail": "discovered artifacts are never measurable"}])

    doc = insights_svc._extraction_source(item)

    assert doc.index("never measurable") < doc.index("inherit the per-tier rule")


def test_the_proposal_is_labelled_as_possibly_stale(db):
    """The description is kept, because the outcome fields this tracker holds are mostly
    links and test counts and dropping it would usually leave nothing durable to learn from.
    What changes is that it is no longer presented as settled fact."""
    item = _Item(description="The plan as filed.",
                 evidence=[{"kind": "note", "detail": "what shipped"}])

    doc = insights_svc._extraction_source(item)

    assert "ORIGINAL PROPOSAL" in doc and "may have revised or reversed" in doc
    assert "The plan as filed." in doc


def test_evidence_details_and_links_reach_the_extractor(db):
    item = _Item(description="d",
                 evidence=[{"kind": "test", "detail": "1459 passed", "url": "https://ci/1"}],
                 github_url="https://github.com/x/y/pull/165")

    doc = insights_svc._extraction_source(item)

    assert "1459 passed" in doc and "https://ci/1" in doc
    assert "https://github.com/x/y/pull/165" in doc
    assert "final status: done" in doc


def test_an_item_with_no_outcome_falls_back_to_the_description_alone(db):
    """Most items carry no evidence. Wrapping a bare description in headings that promise an
    outcome section would be worse than not wrapping it — it would assert a record exists."""
    item = _Item(description="Only ever had a description.")

    doc = insights_svc._extraction_source(item)

    assert doc == "Only ever had a description."


def test_extraction_uses_the_outcome_document(client, auth, db, monkeypatch):
    """The seam, wired. Asserting on `_extraction_source` alone would pass just as well if
    `extract_lessons` never called it — which is exactly how this defect shipped."""
    from app.services import platform as platform_svc

    seen = {}

    class Extractor:
        def extract(self, *, title, description):
            seen["description"] = description
            return ["a lesson"]

    monkeypatch.setattr(platform_svc, "extractor_for", lambda db, pid: Extractor())
    pid = _proj(client, auth, "ExtractWiring")
    item = client.post("/api/items", json={
        "title": "An item", "description": "The plan as filed.", "project_id": pid,
    }, headers=auth).json()
    client.patch(f"/api/items/{item['id']}", headers=auth, json=attest.complete_body(
        evidence=[{"kind": "test", "detail": "what actually shipped"}]))

    insights_svc.extract_lessons(db, item["id"])

    doc = seen["description"]
    assert "what actually shipped" in doc
    assert doc.index("what actually shipped") < doc.index("The plan as filed.")


def test_closing_an_item_uses_the_outcome_document(client, auth, db, monkeypatch):
    """The path that ACTUALLY fires, and the one the first version of this fix missed.

    There were two extraction paths: the explicit `extract_lessons` MCP tool, and
    `items._auto_extract_lessons` on the done transition. The second is what runs when a
    human closes an item — so it is the one that produced the wrong GRPH-354 lesson. The
    original fix only changed the tool, and the test above called that tool directly, so it
    passed while this path still distilled the raw description.

    Driving it through the STATUS TRANSITION rather than by calling the function is the
    whole point: it is the only way to catch a caller that quietly does its own thing.
    """
    from app.services import platform as platform_svc

    seen = {}

    class Extractor:
        def extract(self, *, title, description):
            seen["description"] = description
            return ["a lesson from the close path"]

    monkeypatch.setattr(platform_svc, "extractor_for", lambda db, pid: Extractor())
    pid = _proj(client, auth, "ClosePath")
    item = client.post("/api/items", json={
        "title": "An item", "description": "The plan as filed.", "project_id": pid,
    }, headers=auth).json()

    client.patch(f"/api/items/{item['id']}", headers=auth, json=attest.complete_body(
        evidence=[{"kind": "test", "detail": "what actually shipped"}]))

    assert "description" in seen, "closing the item must extract at all"
    doc = seen["description"]
    assert "what actually shipped" in doc, "the close path must read the outcome too"
    assert doc.index("what actually shipped") < doc.index("The plan as filed.")


def test_a_lesson_from_closing_an_item_is_not_published(client, auth):
    """End to end on the real path: close an item, and whatever it distilled is waiting in
    the queue rather than sitting in the trusted retrieval path."""
    pid = _proj(client, auth, "ClosePathStatus")
    client.patch(f"/api/projects/{pid}", json={"memory_write_mode": "auto"}, headers=auth)
    item = client.post("/api/items", json={
        "title": "An item", "description": "Some plan.", "project_id": pid,
    }, headers=auth).json()

    client.patch(f"/api/items/{item['id']}", headers=auth, json=attest.complete_body())

    shards = client.get(f"/api/memory/shards?project_id={pid}", headers=auth).json()
    lessons = [s for s in shards if s["source"] == f"lesson from {item['id']}"]
    assert all(s["status"] != "published" for s in lessons)
