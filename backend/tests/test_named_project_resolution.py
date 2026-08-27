"""A named project id is not a fallback (GRPH-427).

Verified live on `ubuntu-srv`: `GET /api/agent/code/health?project_id=does-not-exist` returned
**HTTP 200**, carrying the caller's default project's health. Ask about one thing, receive an
authoritative-looking answer about another — and `health` is precisely the read whose numbers
a human uses to decide whether the graph is worth trusting.

The bounded fallback itself is correct and stays. `resolve_project_id` deliberately never lets
an omitted id reach another tenant's project (AL-71), and its docstring already draws the right
line:

> A named-but-existing project is returned as-is; authorization is the caller's job so a
> named-but-forbidden id is rejected there, not silently swapped. **Only the fallback is
> bounded by allowed_ids.**

The code honoured half of it. A named-but-**nonexistent** id is not a fallback: the caller said
which project they meant. So a named id now resolves to `None`, and every caller already turns
that into 404 — `require_readable` fails closed on None by design, and the key path's
`pid not in readable` catches it.

**The omitted case is asserted just as hard as the fixed one**, because it is the behaviour
being preserved rather than changed, and a fix that 404'd an omitted id would break every
default-project caller in the product.
"""
from __future__ import annotations

import pytest

CODE_READS = ("health", "map", "analysis")


@pytest.fixture()
def key(client, auth):
    return client.post("/api/api-keys", json={"name": "resolver"},
                       headers=auth).json()["plaintext"]


# ── the defect ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("read", CODE_READS)
def test_a_misspelled_project_is_not_answered_about_another(client, auth, read):
    """THE bug, on a JWT. 200 with someone else's numbers is worse than an error: the caller
    has no way to tell the substitution happened."""
    r = client.get(f"/api/agent/code/{read}?project_id=does-not-exist", headers=auth)
    assert r.status_code == 404, f"{read} answered about a different project: {r.status_code}"


def test_the_same_for_an_agent_key(client, key):
    """GRPH-405 widened this read to accept a key; the two paths must not disagree about
    what a named id means."""
    r = client.get("/api/agent/code/health?project_id=does-not-exist",
                   headers={"X-API-Key": key})
    assert r.status_code == 404, r.status_code


# ── what must NOT change ──────────────────────────────────────────────────────

@pytest.mark.parametrize("read", CODE_READS)
def test_an_omitted_project_still_resolves_to_the_default(client, auth, read):
    """The bounded fallback is the feature, not the bug (AL-71). Every default-project
    caller in the product depends on this, so it is asserted rather than assumed."""
    r = client.get(f"/api/agent/code/{read}", headers=auth)
    assert r.status_code == 200, f"the omitted case broke: {r.status_code} {r.text[:200]}"


def test_an_empty_project_id_is_omitted_not_named(client, auth):
    """`?project_id=` is how a client that has no project selected actually calls this.
    Reading it as a NAMED unknown id would 404 the commonest request in the product."""
    r = client.get("/api/agent/code/health?project_id=", headers=auth)
    assert r.status_code == 200, r.status_code


def test_a_real_project_is_still_returned_as_is(client, auth):
    """The other half of the docstring: a named id that exists and is readable is honoured,
    not swapped for the default."""
    pid = client.post("/api/projects", json={"name": "Named"}, headers=auth).json()["id"]
    r = client.get(f"/api/agent/code/health?project_id={pid}", headers=auth)
    assert r.status_code == 200, r.text[:200]


# ── the resolver itself ───────────────────────────────────────────────────────

def test_resolve_distinguishes_omitted_from_named_and_unknown(client):
    """Directly on the function, because the distinction IS the fix and the routers only
    inherit it."""
    from app.db import SessionLocal
    from app.services.projects import default_project_id, resolve_project_id

    db = SessionLocal()
    try:
        assert resolve_project_id(db, "no-such-project") is None
        assert resolve_project_id(db, None) == default_project_id(db)
        assert resolve_project_id(db, "") == default_project_id(db)
    finally:
        db.close()


def test_an_unknown_named_id_is_not_rescued_by_allowed_ids(client):
    """`allowed_ids` bounds the FALLBACK. It must not be read as a second chance for a name
    that does not exist — that would reintroduce the substitution for exactly the callers
    that pass a scope."""
    from app.db import SessionLocal
    from app.services.projects import default_project_id, resolve_project_id

    db = SessionLocal()
    try:
        allowed = [default_project_id(db)]
        assert resolve_project_id(db, "no-such-project", allowed_ids=allowed) is None
    finally:
        db.close()
