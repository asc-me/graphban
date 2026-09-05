"""A bad model name is refused on save, and an ungraded round says so (GRPH-485).

Two halves of one incident, 2026-08-25. A project's `chat_model` was set to
`qwen3.6:35b-a3b-coding-mtp` — a tag the Ollama host did not have. The value saved cleanly.
Every chat call then returned `model ... not found`, and the PRD grill it broke reported
"your answers are still outstanding" three times over an hour, because a grader that cannot
run and an author who under-answered produced the same response.

So: refuse the name at the point it is written (part 2), and make an ungraded round
distinguishable from a thin answer (part 1). Either alone leaves the other failure intact.
"""
from __future__ import annotations

import pytest

from app.providers import probe, registry
from app.services import platform as platform_svc


# ---- part 2: the name is checked where it is written --------------------------------


@pytest.fixture()
def listing(monkeypatch):
    """A provider that answers, so a wrong name has something to be wrong against."""
    def fake(provider_id, base_url, api_key=""):
        return frozenset({"qwen3.6:35b-a3b-coding-mtp-q4_K_M", "bge-m3:latest"})
    monkeypatch.setattr(probe, "known_models", fake)
    monkeypatch.setattr(platform_svc.probe, "known_models", fake)


def test_the_exact_name_from_the_incident_is_refused(client, auth, listing):
    """`...-coding-mtp` is a real prefix of a real model and still not a model. That is
    what made it plausible enough to save and total enough to break everything."""
    r = client.patch(
        "/api/platform?project_id=core",
        json={"providers": {"ollama": {"base_url": "http://x:11434",
                                       "chat_model": "qwen3.6:35b-a3b-coding-mtp"}}},
        headers=auth,
    )

    assert r.status_code == 422, r.text
    assert "qwen3.6:35b-a3b-coding-mtp" in r.text


def test_the_refusal_names_a_near_miss(client, auth, listing):
    """The incident was one edit from correct. A refusal that does not say which edit
    leaves the reader doing the diff by eye."""
    r = client.patch(
        "/api/platform?project_id=core",
        json={"providers": {"ollama": {"base_url": "http://x:11434",
                                       "chat_model": "qwen3.6:35b-a3b-coding-mtp"}}},
        headers=auth,
    )

    assert "Did you mean" in r.text
    assert "qwen3.6:35b-a3b-coding-mtp-q4_K_M" in r.text


def test_a_model_the_provider_has_saves(client, auth, listing):
    r = client.patch(
        "/api/platform?project_id=core",
        json={"providers": {"ollama": {"base_url": "http://x:11434",
                                       "chat_model": "qwen3.6:35b-a3b-coding-mtp-q4_K_M"}}},
        headers=auth,
    )

    assert r.status_code == 200, r.text


def test_the_embed_model_is_checked_too(client, auth, listing):
    """A broken embedder is the quieter failure: `safe_embed` stores rows with a NULL
    vector and logs, so nothing errors while the corpus silently stops being searchable."""
    r = client.patch(
        "/api/platform?project_id=core",
        json={"providers": {"ollama": {"base_url": "http://x:11434",
                                       "embed_model": "nomic-embed-text"}}},
        headers=auth,
    )

    assert r.status_code == 422, r.text
    assert "nomic-embed-text" in r.text


def test_an_unreachable_provider_does_not_block_a_correct_edit(client, auth, monkeypatch):
    """UNCHECKED IS NOT INVALID. A host that is briefly down must not refuse a save —
    that would make the check worse than its absence."""
    monkeypatch.setattr(platform_svc.probe, "known_models", lambda *a, **k: None)

    r = client.patch(
        "/api/platform?project_id=core",
        json={"providers": {"ollama": {"base_url": "http://x:11434",
                                       "chat_model": "anything-at-all"}}},
        headers=auth,
    )

    assert r.status_code == 200, r.text


def test_a_listing_of_zero_models_does_not_refuse_everything(client, auth, monkeypatch):
    """A provider that answered and has none says "I know of no models", not "every name
    you could give is wrong". Refusing here would break an account with no entitlements
    yet — the same distinction `probe.known_models` keeps between None and empty."""
    monkeypatch.setattr(platform_svc.probe, "known_models", lambda *a, **k: frozenset())

    r = client.patch(
        "/api/platform?project_id=core",
        json={"providers": {"ollama": {"base_url": "http://x:11434",
                                       "chat_model": "anything-at-all"}}},
        headers=auth,
    )

    assert r.status_code == 200, r.text


def test_a_provider_with_no_listing_endpoint_is_never_even_asked(client, auth, monkeypatch):
    """Anthropic ships no listing endpoint, so the registry gate must stop the probe
    BEFORE it makes a request.

    Asserted on the CALL, not the outcome. Removing the gate still yields a 200, because
    the request to a provider with no `/models` fails and `known_models` correctly reports
    "cannot be asked" — so the save succeeds either way and the outcome proves nothing.
    What it would really cost is a pointless network round trip inside every save."""
    assert "anthropic" not in registry.LISTS_MODELS
    asked = []
    monkeypatch.setattr(probe, "_openai_compat", lambda *a, **k: asked.append(a) or set())
    monkeypatch.setattr(probe, "_ollama", lambda *a, **k: asked.append(a) or set())

    r = client.patch(
        "/api/platform?project_id=core",
        json={"providers": {"anthropic": {"base_url": "https://api.anthropic.com",
                                          "chat_model": "claude-something-new"}}},
        headers=auth,
    )

    assert r.status_code == 200, r.text
    assert asked == [], "a provider with no listing endpoint was asked for its models"


def test_saving_other_fields_does_not_probe(client, auth, monkeypatch):
    """A probe costs a network round trip inside a form submit. Only a model name pays."""
    calls = []
    monkeypatch.setattr(platform_svc.probe, "known_models",
                        lambda *a, **k: calls.append(a) or frozenset({"x"}))

    client.patch("/api/platform?project_id=core",
                 json={"providers": {"ollama": {"base_url": "http://y:11434"}}}, headers=auth)

    assert calls == [], "a base_url-only edit asked the provider for its models"


# ---- part 1: an ungraded round is distinguishable from a thin answer -----------------


def test_probe_returns_none_when_it_cannot_ask():
    """The contract that keeps 'cannot be asked' apart from 'has none' — the same shape
    `gbfleet.adapters.known_models` uses, deliberately."""
    assert probe.known_models("anthropic", "https://api.anthropic.com") is None
    assert probe.known_models("ollama", "") is None
    assert probe.known_models("ollama", "http://127.0.0.1:9") is None



@pytest.fixture()
def db(client):
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _grilled_prd(client, auth, db) -> "object":
    """A PRD with one recorded answer, so there is something to grade."""
    from app.models import Prd
    from app.services import prds as prd_svc

    prd_id = client.post("/api/prds", json={"title": "Spec", "body": "## D1\n\nwork",
                                            "project_id": "core"}, headers=auth).json()["id"]
    prd_svc.record_grill_turns(db, prd_id, [{"role": "user", "text": "an answer"}],
                               via="test", actor="test")
    db.commit()
    return db.get(Prd, prd_id)


def test_an_ungraded_round_says_so_instead_of_reading_as_a_thin_answer(client, auth, monkeypatch, db):
    """THE HOUR THIS COST.

    When the grader cannot be asked, `classify_grill` correctly leaves the dimensions
    alone — falling back to the stub's weaker bar would grade with a standard the real
    model just refused. But it used to return the SAME payload as a successful grading
    that found gaps, so "the grader is down" and "your answer was too thin" were one
    response. An author answered three times against a chat model whose name did not
    exist on the host, was told `outstanding` each time, and nothing moved.
    """
    from app.services import prds as prd_svc

    prd = _grilled_prd(client, auth, db)
    monkeypatch.setattr(prd_svc, "_classify_dimensions", lambda *a, **k: None)
    monkeypatch.setattr(prd_svc, "_grader_id", lambda *a, **k: "ollama")

    out = prd_svc.classify_grill(db, prd)

    assert out["graded"] is False, "an ungraded round is indistinguishable from a graded one"
    assert "could not be asked" in out["ungraded_reason"]
    assert "chat model" in out["ungraded_reason"], "the reason must point at the likely cause"


def test_the_ungraded_fact_outlives_the_round_that_produced_it(client, auth, monkeypatch, db):
    """The half GRPH-485 left open: every LATER read said nothing.

    `classify_grill` returned `graded=False` to whoever triggered the round, and
    `answer_grill` relays it — but the fact lived only in that response. The PRD editor
    does not read that response; it polls `GET /prds/{id}/grill`, which re-derived
    completion from the dimension rows and so reported a confident `graded` with the
    previous round's outcomes. Same loop as the original incident, one surface over:
    the author answers, the panel does not move, and nothing says a grader failed.
    """
    from app.services import prds as prd_svc

    prd = _grilled_prd(client, auth, db)
    monkeypatch.setattr(prd_svc, "_classify_dimensions", lambda *a, **k: None)
    monkeypatch.setattr(prd_svc, "_grader_id", lambda *a, **k: "ollama")
    prd_svc.classify_grill(db, prd)

    # A plain read, with no grading in flight — the request the editor actually makes.
    state = client.get(f"/api/prds/{prd.id}/grill", headers=auth).json()

    assert state["graded"] is False, (
        "grill_state reports the round as graded; an author cannot tell a dead grader "
        "from a thin answer on the surface they are actually looking at"
    )
    assert "could not be asked" in state["ungraded_reason"]
    # The outcomes are still served — stale, and now labelled as such, rather than hidden.
    assert set(state["dimensions"]) == set(prd_svc.DIMENSIONS)


def test_a_grader_that_comes_back_stops_being_accused(client, auth, monkeypatch, db):
    """The clearing half. A flag that only ever gets set would leave every PRD that once
    met a broken grader permanently claiming its outcomes are stale, which is the same
    defect pointed the other way."""
    from app.services import prds as prd_svc

    prd = _grilled_prd(client, auth, db)
    monkeypatch.setattr(prd_svc, "_classify_dimensions", lambda *a, **k: None)
    monkeypatch.setattr(prd_svc, "_grader_id", lambda *a, **k: "ollama")
    prd_svc.classify_grill(db, prd)
    assert client.get(f"/api/prds/{prd.id}/grill", headers=auth).json()["graded"] is False

    # The grader answers this time.
    monkeypatch.setattr(prd_svc, "_grader_id", lambda *a, **k: "stub")
    prd_svc.classify_grill(db, prd)

    state = client.get(f"/api/prds/{prd.id}/grill", headers=auth).json()
    assert state["graded"] is True
    assert state["ungraded_reason"] == ""


def test_a_graded_round_reports_graded_true(client, auth, monkeypatch, db):
    """The control. Without it `graded` could be hardcoded false and the test above
    would still pass."""
    from app.services import prds as prd_svc

    prd = _grilled_prd(client, auth, db)
    monkeypatch.setattr(prd_svc, "_grader_id", lambda *a, **k: "stub")

    out = prd_svc.classify_grill(db, prd)

    assert out["graded"] is True
    assert out["ungraded_reason"] == ""
