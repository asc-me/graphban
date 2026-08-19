"""Phase 2: public feedback intake + auto-duplicate detection (stub embedder)."""


def test_public_duplicates_no_auth(client):
    # Near-duplicate of seeded R-31 "Two-way GitHub issue sync".
    r = client.get("/api/public/duplicates", params={"q": "Two-way GitHub issue sync"})
    assert r.status_code == 200  # no Authorization header needed
    hits = r.json()
    assert any(h["kind"] == "request" and h["id"] == "R-31" for h in hits)
    assert hits[0]["score"] >= hits[-1]["score"]


def test_public_submit_creates_request_and_flags_duplicate(client, auth):
    r = client.post(
        "/api/public/requests",
        json={"type": "feature", "title": "Two-way GitHub issue sync please",
              "detail": "sync issues both directions", "email": "x@y.com"},
    )
    assert r.status_code == 201
    body = r.json()
    new_id = body["request"]["id"]
    assert body["request"]["by"] == "x@y.com"
    assert any(d["id"] == "R-31" for d in body["duplicates"])  # surfaced the existing one
    assert all(d["id"] != new_id for d in body["duplicates"])  # never itself

    # The new request shows up in the authenticated triage queue.
    reqs = client.get("/api/requests?project_id=core", headers=auth).json()
    assert any(x["id"] == new_id for x in reqs)


def test_public_submit_captures_context(client, auth):
    r = client.post(
        "/api/public/requests",
        json={"type": "bug", "title": "Checkout button dead on mobile",
              "detail": "Tapping Pay does nothing on iOS Safari.",
              "source_url": "https://shop.example.com/checkout",
              "meta": {"app_version": "2.4.1"}},
        headers={"User-Agent": "TestBrowser/9.9"},
    )
    assert r.status_code == 201
    new_id = r.json()["request"]["id"]
    got = next(x for x in client.get("/api/requests?project_id=core", headers=auth).json() if x["id"] == new_id)
    assert got["detail"] == "Tapping Pay does nothing on iOS Safari."  # detail is now persisted
    assert got["source_url"] == "https://shop.example.com/checkout"    # page captured
    assert got["meta"]["app_version"] == "2.4.1"                       # custom meta kept
    assert got["meta"]["user_agent"] == "TestBrowser/9.9"              # UA captured server-side


_PNG_1x1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00"
    b"\x00IEND\xaeB`\x82"
)


def test_attachment_upload_link_and_serve(client, auth):
    up = client.post(
        "/api/public/attachments",
        files={"file": ("shot.png", _PNG_1x1, "image/png")},
    )
    assert up.status_code == 201, up.text
    att_id = up.json()["id"]

    # Attach it to a submission, then read it back through triage.
    sub = client.post(
        "/api/public/requests",
        json={"type": "bug", "title": "Broken layout with screenshot", "attachment_ids": [att_id]},
    )
    assert sub.status_code == 201
    new_id = sub.json()["request"]["id"]
    got = next(x for x in client.get("/api/requests?project_id=core", headers=auth).json() if x["id"] == new_id)
    assert got["attachment_ids"] == [att_id]

    served = client.get(f"/api/public/attachments/{att_id}")
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/png"
    assert served.content == _PNG_1x1


def test_attachment_rejects_non_image(client):
    r = client.post(
        "/api/public/attachments",
        files={"file": ("evil.txt", b"not an image", "text/plain")},
    )
    assert r.status_code == 422


def test_honeypot_rejects_bot(client):
    r = client.post(
        "/api/public/requests",
        json={"type": "feedback", "title": "spammy", "hp": "http://spam.example"},
    )
    assert r.status_code == 400


def test_public_submit_rejects_bad_type(client):
    r = client.post("/api/public/requests", json={"type": "banana", "title": "x"})
    assert r.status_code == 422


def test_public_rate_limit(client):
    codes = [
        client.get("/api/public/duplicates", params={"q": "spam"}).status_code
        for _ in range(25)
    ]
    assert 429 in codes  # sliding window trips after the cap
    assert codes.count(200) <= 20


# ---- PRD-21 screen 14: the triage queue ----------------------------------------
def test_triage_queue_is_only_what_is_undispositioned(client, auth):
    """A linked request has been triaged and belongs to the tracker, not the queue. If
    it stayed, the queue would never empty and would stop meaning anything."""
    r = client.post("/api/requests", json={
        "type": "bug", "title": "Sidebar collapses on resize", "project_id": "core",
    }, headers=auth)
    assert r.status_code == 201, r.text
    new_id = r.json()["id"]

    queued = client.get("/api/requests/triage?project_id=core", headers=auth).json()
    assert any(row["request"]["id"] == new_id for row in queued)
    assert all(row["request"]["status"] == "new" for row in queued)

    client.post(f"/api/requests/{new_id}/accept", headers=auth)
    queued = client.get("/api/requests/triage?project_id=core", headers=auth).json()
    assert all(row["request"]["id"] != new_id for row in queued)


def test_triage_row_carries_a_duplicate_hint_or_an_explicit_none(client, auth):
    """Null means "compared, nothing matched" — the comparison always runs, so a row
    without a hint is never "we did not look"."""
    r = client.post("/api/requests", json={
        "type": "feature", "title": "Two-way GitHub issue sync", "project_id": "core",
    }, headers=auth)
    dupe_id = r.json()["id"]

    rows = client.get("/api/requests/triage?project_id=core", headers=auth).json()
    row = next(x for x in rows if x["request"]["id"] == dupe_id)
    # Seeded R-31 is the same request, so this one must surface it.
    assert row["duplicate"] is not None
    assert row["duplicate"]["id"] == "R-31"
    assert "duplicate" in row  # present as a key even when null, never omitted


def test_accept_creates_the_item_and_links_it_together(client, auth):
    r = client.post("/api/requests", json={
        "type": "bug", "title": "Crash on empty project", "detail": "steps here",
        "project_id": "core",
    }, headers=auth)
    req_id = r.json()["id"]

    got = client.post(f"/api/requests/{req_id}/accept", headers=auth)
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["item"]["title"] == "Crash on empty project"
    assert body["item"]["description"] == "steps here"
    assert "bug" in body["item"]["tags"]  # the type a triager sorted by survives
    assert body["request"]["status"] == "linked"
    assert body["request"]["linked_to"] == body["item"]["id"]


def test_accepting_twice_does_not_fork_one_report_into_two_items(client, auth):
    """A double-click must not mint a second item. Idempotent on the existing link."""
    r = client.post("/api/requests", json={
        "type": "bug", "title": "Double click me", "project_id": "core",
    }, headers=auth)
    req_id = r.json()["id"]

    first = client.post(f"/api/requests/{req_id}/accept", headers=auth).json()
    second = client.post(f"/api/requests/{req_id}/accept", headers=auth).json()
    assert first["item"]["id"] == second["item"]["id"]

    items = client.get("/api/items?project_id=core", headers=auth).json()
    assert len([i for i in items if i["title"] == "Double click me"]) == 1


def test_accept_leaves_no_orphan_item_when_the_request_is_gone(client, auth):
    assert client.post("/api/requests/R-9999/accept", headers=auth).status_code == 404


def test_accept_creates_the_item_inside_the_link_transaction(client, auth, monkeypatch):
    """The reason `create_item(commit=False)` exists.

    If the item commits on its own and the link then fails, the board gains work whose
    request still shows as untriaged — an item nobody asked for, beside a queue entry
    saying nobody has looked.

    Asserted white-box, on the argument, rather than by simulating a crash: patching
    `Session.commit` kills whichever commit comes first, so a two-commit implementation
    rolls back at its first one and looks identical to a one-commit implementation. The
    thing that actually differs is the flag, so the flag is what this pins.
    """
    from app.services import requests as req_svc

    seen: dict = {}
    real_create = req_svc.items_svc.create_item

    def spy(db, **kwargs):
        seen.update(kwargs)
        return real_create(db, **kwargs)

    monkeypatch.setattr(req_svc.items_svc, "create_item", spy)

    r = client.post("/api/requests", json={
        "type": "bug", "title": "One transaction", "project_id": "core",
    }, headers=auth)
    got = client.post(f"/api/requests/{r.json()['id']}/accept", headers=auth)
    assert got.status_code == 200, got.text

    assert seen["commit"] is False, "the item must not commit ahead of its link"
    # And the visible result is still correct end to end.
    assert got.json()["request"]["linked_to"] == got.json()["item"]["id"]
