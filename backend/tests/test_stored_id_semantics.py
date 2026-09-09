"""What `stored_id` is, and why it can carry another project's prefix (GRPH-815).

Reported as "stored_id cross-project leakage: SA-145 -> AL-145, SA-18 -> AL-18", with the note
that the two AL-tagged items were the two that behaved worst — "either coincidence or a lead".

**It is not leakage.** This file is the measurement rather than the assurance, because a
finding answered with "trust me, authz holds" is worth nothing to the next person to read it.

Three things were checked:

1. A stored id is frozen at mint as the rendered key OF THAT DAY (`keys.mint`), and PRD-13
   accepts the divergence: "only the rendering is user-facing — that divergence is the
   accepted cost of never rewriting an id". So an `AL-` stored id on a `GRPH-` item is the
   design working, not a leak.
2. Resolution ends in a raw primary-key lookup with no project scoping — and every MCP caller
   reaches an item through `_scoped_item`, which refuses one outside the key's scope. That
   refusal is real: it is the same one CI hit when a PR body mentioned super-arc ids.
3. `stored_id` is contractual, not incidental — `test_fleet_holding_phase` asserts it on a
   holding, and it is kept for the web UI. Deleting it to tidy the confusion would break a
   caller to fix a misreading.

What is genuinely left is CONFUSABILITY: a field shaped exactly like another project's key,
emitted beside one, with nothing saying which is which. Worth knowing; not worth "fixing" by
deleting a field somebody depends on.

NO MECHANISM was found linking an `AL-` prefix to the two items behaving badly. Recorded as
looked-at rather than dismissed: the prefix says when an item was minted relative to a retag,
and nothing reads it for any other purpose.
"""
import pytest

from app.services import keys as keys_svc


@pytest.fixture()
def session(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _item(client, auth, title, project_id="core"):
    r = client.post("/api/items", json={"title": title, "project_id": project_id}, headers=auth)
    assert r.status_code in (200, 201), r.text
    return r.json()


def test_a_fresh_stored_id_is_its_rendered_key(client, auth, session):
    """They agree until a retag, and only then diverge. This is the "before" half of the
    divergence PRD-13 accepts."""
    made = _item(client, auth, "fresh")

    assert keys_svc.resolve_item(session, made["id"]) == made["id"]


def test_a_key_naming_nothing_resolves_to_nothing(client, auth, session):
    """None is the caller's 404 — "no such entity", not "malformed input"."""
    assert keys_svc.resolve_item(session, "ZZ9-99999") is None


def test_an_item_outside_the_keys_scope_is_refused(client, auth):
    """THE ONE THAT MATTERS for the leakage claim. Resolution ends in an unscoped primary-key
    lookup; authz is what makes that safe, and it is checked here rather than assumed."""
    client.post("/api/projects", json={"name": "Faraway"}, headers=auth)
    other = _item(client, auth, "elsewhere", project_id="faraway")
    scoped = client.post("/api/api-keys", json={"name": "scoped", "project_id": "core"},
                         headers=auth).json()["plaintext"]

    r = client.post("/api/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "get_item_details", "arguments": {"id": other["id"]}}},
        headers={"X-API-Key": scoped})

    assert "outside this key's project scope" in str(r.json()), r.text


def test_a_scoped_key_still_reaches_its_own_items(client, auth):
    """The control. A refusal that also refused the caller's own work would satisfy the test
    above while breaking every agent."""
    mine = _item(client, auth, "mine")
    scoped = client.post("/api/api-keys", json={"name": "scoped2", "project_id": "core"},
                         headers=auth).json()["plaintext"]

    r = client.post("/api/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "get_item_details", "arguments": {"id": mine["id"]}}},
        headers={"X-API-Key": scoped})

    assert "outside this key's project scope" not in str(r.json())
