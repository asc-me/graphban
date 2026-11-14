"""An item whose work lives in another repo can reach `done` (GRPH-1619).

GRPH-1596 (rule pack 0.5.0) was built in asc-me/graphban-rules and merged there at d9ae398.
That repo's CI does not report to Graphban, so no `suite_green` can ever exist for its head:
`sign_off` refused with `pr_ci_not_green` for ever. The item declares its repo with a
`repo:<owner/name>` tag, and the reviewer cites the external run in `external_ci`; the server
records it as the reviewer's receipt under its own adapter — never as `suite_green`.

The regression half matters as much: an ordinary in-repo item with no `suite_green` is still
refused, with or without a citation.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.config import settings
from app.models import Project, utcnow
from app.services import fleet as fleet_svc
from app.services import items as items_svc

HEAD = "d9ae398"
RULES_REPO = "asc-me/graphban-rules"
RULES_PR = {"kind": "url", "url": f"https://github.com/{RULES_REPO}/pull/8",
            "detail": "PR #8"}
IN_REPO_PR = {"kind": "url", "url": "https://github.com/asc-me/graphban/pull/999",
              "detail": "PR #999"}
RUN = f"https://github.com/{RULES_REPO}/actions/runs/123456"
CITED = {"repo": RULES_REPO, "commit": HEAD, "run_url": RUN, "conclusion": "success"}
CUT_FROM = "1111222233334444555566667777888899990000"
ACCEPTANCE = ("## Acceptance\n- the pack ships\n")
TEST = {"kind": "test", "detail": "the pack ships: pack-ci green on d9ae398"}


@pytest.fixture()
def db(client):
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def cooldown(monkeypatch):
    monkeypatch.setattr(settings, "pr_cooldown_seconds", 60, raising=False)


def _in_review(db, *, tags=(), pr=RULES_PR):
    """An item in review whose PR was linked long ago — the cooldown is over, so the only
    thing between it and `done` is a CI answer."""
    it = items_svc.create_item(db, project_id="core", title="Rule pack 0.5.0",
                               description=ACCEPTANCE, tags=list(tags))
    it.evidence = items_svc.append_evidence(it.evidence, [
        {"kind": "note", "detail": f"{items_svc.CUT_FROM_MARKER}{CUT_FROM} (`gb/x`)"}, pr])
    it.pr_linked_at = utcnow() - timedelta(seconds=3600)
    it.status = "review"
    it.built_by = "builder-agent"
    db.commit()
    db.refresh(it)
    return it


def _sign(db, item, **kw):
    return fleet_svc.sign_off(db, item_id=item.id, agent_id="reviewer-1", evidence=[TEST],
                              commit=HEAD, **kw)


def test_an_external_repo_item_signs_off_on_the_cited_run(db):
    """The GRPH-1596 shape: declared repo, cited green run at the head → done."""
    item = _in_review(db, tags=[f"repo:{RULES_REPO}"])

    out = _sign(db, item, external_ci=CITED)

    assert out.status == "done"
    cited = [a for a in items_svc.attestation_receipts(out.evidence)
             if a.get("adapter") == items_svc.EXTERNAL_CI_ADAPTER]
    assert len(cited) == 1
    names = [q["name"] for q in cited[0]["predicates"]]
    # Recorded as the reviewer's external citation, never as this repo's suite_green.
    assert names == [items_svc.EXTERNAL_CI_PREDICATE]
    assert "suite_green" not in items_svc.attested_predicates(out.evidence)
    assert "reviewer-1" in cited[0]["predicates"][0]["detail"]
    assert cited[0]["run_ref"] == RUN
    assert cited[0]["commit"] == HEAD


def test_without_a_citation_the_external_item_is_still_refused(db):
    """The defect as found: nothing can post suite_green for d9ae398."""
    item = _in_review(db, tags=[f"repo:{RULES_REPO}"])

    with pytest.raises(items_svc.PRCINotGreen):
        _sign(db, item)


def test_an_in_repo_item_with_no_suite_green_is_still_refused(db):
    """REGRESSION. No `repo:` tag: the external path is refused, and so is plain sign_off."""
    item = _in_review(db, pr=IN_REPO_PR)

    with pytest.raises(fleet_svc.ExternalCIRefused):
        _sign(db, item, external_ci=dict(CITED, repo="asc-me/graphban",
                                         run_url="https://github.com/asc-me/graphban/actions/runs/1"))
    with pytest.raises(items_svc.PRCINotGreen):
        _sign(db, item)
    db.refresh(item)
    assert item.status == "review"


def test_declaring_this_projects_own_repo_is_not_external(db):
    """A `repo:` tag naming the project's own repo is an in-repo item: its CI reports here."""
    project = db.get(Project, "core")
    project.repo_url = "github.com/asc-me/graphban"
    db.commit()
    item = _in_review(db, tags=["repo:asc-me/graphban"], pr=IN_REPO_PR)

    with pytest.raises(fleet_svc.ExternalCIRefused) as exc:
        _sign(db, item, external_ci=dict(CITED, repo="asc-me/graphban",
                                         run_url="https://github.com/asc-me/graphban/actions/runs/1"))
    assert "this project's own" in str(exc.value)


@pytest.mark.parametrize("bad, said", [
    ({"conclusion": "failure"}, "bounce"),
    ({"commit": "abcdef0"}, "must be the commit"),
    ({"repo": "asc-me/other"}, "is not the repo"),
    ({"run_url": "https://github.com/asc-me/graphban-rules-fork/actions/runs/1"}, "run in"),
    ({"run_url": "not a url"}, "run in"),
])
def test_a_citation_that_does_not_match_is_refused(db, bad, said):
    item = _in_review(db, tags=[f"repo:{RULES_REPO}"])

    with pytest.raises(fleet_svc.ExternalCIRefused) as exc:
        _sign(db, item, external_ci=dict(CITED, **bad))
    assert said in str(exc.value)
    db.refresh(item)
    assert item.status == "review"


def test_a_hand_posted_external_receipt_does_not_open_an_in_repo_item(db):
    """The receipt counts only for an item declared external: posting one on an in-repo item
    (a `gate`-scoped writer, or a tag removed after sign-off) lets nothing through."""
    item = _in_review(db, pr=IN_REPO_PR)
    forged = {"kind": "attestation", "adapter": items_svc.EXTERNAL_CI_ADAPTER, "commit": HEAD,
              "predicates": [{"name": items_svc.EXTERNAL_CI_PREDICATE, "passed": True,
                              "detail": "forged"}]}

    with pytest.raises(items_svc.PRCINotGreen):
        fleet_svc.sign_off(db, item_id=item.id, agent_id="reviewer-1",
                           evidence=[TEST, forged], commit=HEAD)


def test_required_suite_green_is_met_by_the_citation_for_an_external_item_only(db, monkeypatch):
    """With `suite_green` required, the external item completes on its citation and an
    in-repo item carrying the same receipt is refused for the missing predicate."""
    monkeypatch.setattr(settings, "required_predicates", "suite_green", raising=False)
    monkeypatch.setattr(settings, "pr_cooldown_seconds", 0, raising=False)

    ext = _in_review(db, tags=[f"repo:{RULES_REPO}"])
    assert _sign(db, ext, external_ci=CITED).status == "done"

    local = _in_review(db, pr=IN_REPO_PR)
    forged = {"kind": "attestation", "adapter": items_svc.EXTERNAL_CI_ADAPTER, "commit": HEAD,
              "predicates": [{"name": items_svc.EXTERNAL_CI_PREDICATE, "passed": True,
                              "detail": "forged"}]}
    with pytest.raises(items_svc.MissingAttestation):
        fleet_svc.sign_off(db, item_id=local.id, agent_id="reviewer-1",
                           evidence=[TEST, forged], commit=HEAD)


def test_declared_repo_reads_owner_name_and_full_forms():
    class _I:
        tags = ["bug", "repo:https://github.com/Asc-Me/graphban-rules.git"]
    assert items_svc.declared_repo(_I()) == "github.com/asc-me/graphban-rules"
    _I.tags = [f"repo:{RULES_REPO}"]
    assert items_svc.declared_repo(_I()) == "github.com/asc-me/graphban-rules"
    _I.tags = ["repo:nonsense"]
    assert items_svc.declared_repo(_I()) is None
