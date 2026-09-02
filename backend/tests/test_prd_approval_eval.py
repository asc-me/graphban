"""Pre-approval PRD quality (GRPH-80).

Mechanical completeness and coverage gaps always run. The LLM half is on
POST only. Stub / not-asked is ungraded, never a fabricated fail. A judged
ready cannot paper over missing sections.
"""
from app.services import prds as prd_svc

# Long enough that none of the four standard sections count as placeholders.
_FULL = (
    "# Spec\n\n"
    "## Problem\n\n"
    "Agents guess the git model of a repo because Graphban never tells them "
    "what the six fields are or who is allowed to write them.\n\n"
    "## Goals\n\n"
    "Named models write the six fields. Migration assist files tracker items. "
    "Graphban does not run git itself.\n\n"
    "## Non-Goals\n\n"
    "No git hosting, no CI orchestration, and no merge-on-green. The supervisor "
    "does not push, rebase, or merge on anyone's behalf.\n\n"
    "## Acceptance criteria\n\n"
    "Given a named model, the six fields are populated without a human editing "
    "git config, and a missing model is a named gap rather than a silent default.\n"
)


def _prd(client, auth, title, body, **extra):
    payload = {"title": title, "body": body, **extra}
    return client.post("/api/prds", json=payload, headers=auth).json()


def test_missing_acceptance_is_named_not_a_quiet_ok(client, auth):
    prd = _prd(client, auth, "NoDoneWhen", (
        "# NoDoneWhen\n\n"
        "## Problem\n\nAgents guess the git model of a repo because nobody told them.\n\n"
        "## Goals\n\nNamed models write the six fields and Graphban does not run git.\n\n"
        "## Non-Goals\n\nNo hosting, no CI, no merge-on-green.\n"
    ))
    r = client.get(f"/api/prds/{prd['id']}/evaluate", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["mechanical_ready"] is False
    assert "acceptance" in body["missing"]
    assert any("acceptance" in c.lower() for c in body["callouts"])
    assert body["judged"] is False
    assert body["ready"] is None
    assert "not been asked" in body["ungraded_reason"]


def test_standard_template_is_thin_not_present(client, auth):
    """The skeleton is four headings of italic placeholders. That is not a spec."""
    prd = client.post("/api/prds", json={"title": "Skeleton", "template": "standard"},
                      headers=auth).json()
    body = client.get(f"/api/prds/{prd['id']}/evaluate", headers=auth).json()
    assert body["mechanical_ready"] is False
    assert body["missing"] == []
    assert set(body["thin"]) >= {"problem", "scope", "non_goals", "acceptance"}
    assert any("placeholder" in c.lower() for c in body["callouts"])


def test_evaluate_uses_coverage_for_gaps(client, auth, monkeypatch):
    """Sabotage the CALL: a helper that invents gaps would miss a planted coverage()."""
    prd = _prd(client, auth, "Planted", (
        "# Planted\n\n## Problem\n\n"
        "Collision clusters still serialize because there is no adapter.\n"
    ))
    real = prd_svc.coverage

    def planted(db, p):
        out = real(db, p)
        out = dict(out)
        out["gaps"] = ["PlantedGap"]
        out["implementable_sections"] = max(1, out.get("implementable_sections") or 0)
        return out

    monkeypatch.setattr(prd_svc, "coverage", planted)
    body = client.get(f"/api/prds/{prd['id']}/evaluate", headers=auth).json()
    assert "PlantedGap" in body["coverage_gaps"]
    assert any("PlantedGap" in c for c in body["callouts"])


def test_get_evaluate_does_not_call_the_judge(client, auth, monkeypatch):
    prd = _prd(client, auth, "NoChat", "# NoChat\n\n## Problem\n\nEnough prose to be a section.\n")

    def boom(*a, **k):
        raise AssertionError("GET evaluate must not call the approval judge")

    monkeypatch.setattr(prd_svc, "approval_judge", boom)
    r = client.get(f"/api/prds/{prd['id']}/evaluate", headers=auth)
    assert r.status_code == 200
    assert r.json()["judged"] is False
    assert r.json()["cause"] == "not_asked"


def test_post_evaluate_is_ungraded_on_stub(client, auth):
    prd = _prd(client, auth, "StubJudge", _FULL)
    r = client.post(f"/api/prds/{prd['id']}/evaluate", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["judged"] is False
    assert body["ready"] is None
    assert body["cause"] == "no_provider"
    assert "no independent chat model" in body["ungraded_reason"]
    assert body["mechanical_ready"] is True


def test_post_evaluate_calls_the_judge(client, auth, monkeypatch):
    """Sabotage the CALL: attaching ready without asking would look judged."""
    prd = _prd(client, auth, "JudgeCall", _FULL)
    called = {"n": 0}

    def fake(db, prd, **k):
        called["n"] += 1
        return {"ready": True, "ambiguous": [], "untestable": [],
                "reason": "specific enough to implement", "samples": 3}, "ok"

    monkeypatch.setattr(prd_svc, "approval_judge", fake)
    body = client.post(f"/api/prds/{prd['id']}/evaluate", headers=auth).json()
    assert called["n"] == 1
    assert body["judged"] is True
    assert body["ready"] is True
    assert body["ungraded_reason"] == ""


def test_judged_ready_cannot_paper_over_missing_sections(client, auth, monkeypatch):
    prd = _prd(client, auth, "PaperOver", (
        "# PaperOver\n\n"
        "## Problem\n\nAgents guess the git model of a repo because nobody told them.\n"
    ))

    def cheerful(db, prd, **k):
        return {"ready": True, "ambiguous": [], "untestable": [],
                "reason": "looks fine", "samples": 3}, "ok"

    monkeypatch.setattr(prd_svc, "approval_judge", cheerful)
    body = client.post(f"/api/prds/{prd['id']}/evaluate", headers=auth).json()
    assert body["judged"] is True
    assert body["mechanical_ready"] is False
    assert body["ready"] is False
    assert "acceptance" in body["missing"]


def test_post_evaluate_does_not_change_status(client, auth, monkeypatch):
    prd = _prd(client, auth, "NoMutate", "# NoMutate\n\n## Problem\n\nEnough prose to be a section.\n")
    monkeypatch.setattr(
        prd_svc, "approval_judge",
        lambda db, prd, **k: ({"ready": False, "ambiguous": ["vague"], "untestable": [],
                               "reason": "guesswork", "samples": 3}, "ok"),
    )
    client.post(f"/api/prds/{prd['id']}/evaluate", headers=auth)
    after = client.get(f"/api/prds/{prd['id']}", headers=auth).json()
    assert after["status"] == "draft"


def test_resolve_chat_is_not_used_on_get(client, auth, monkeypatch):
    prd = _prd(client, auth, "NoResolve", "# NoResolve\n\n## Problem\n\nEnough prose to be a section.\n")
    from app.services import platform as platform_svc
    calls = {"n": 0}
    real = platform_svc.resolve_chat

    def wrapped(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(platform_svc, "resolve_chat", wrapped)
    client.get(f"/api/prds/{prd['id']}/evaluate", headers=auth)
    assert calls["n"] == 0, "GET evaluate resolved a chat model"
