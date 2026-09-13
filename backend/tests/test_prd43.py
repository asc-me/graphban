"""PRD-43: Live feedback kit — ingest token, identity, tracking, boards, flags."""
import hashlib
import pytest


def _get_db():
    from app.db import SessionLocal
    return SessionLocal()


def _enable_intake(client, share_token=True):
    """Enable public share + intake on the core project."""
    db = _get_db()
    try:
        from app.services.platform import get_config
        cfg = get_config(db, "core")
        cfg.public_share_enabled = share_token
        cfg.intake_enabled = True
        db.commit()
        return cfg.share_token
    finally:
        db.close()


def test_ingest_token_mint_and_verify(client, auth):
    """D1: mint returns gbfb_ prefix, verify resolves to project."""
    resp = client.post("/api/public/ingest-token?project_id=core", headers=auth)
    assert resp.status_code == 200
    data = resp.json()
    assert data["token"].startswith("gbfb_")
    assert data["prefix"].endswith("…")

    from app.services.platform import resolve_project_by_ingest_token
    db = _get_db()
    try:
        pid = resolve_project_by_ingest_token(db, data["token"])
        assert pid == "core"
    finally:
        db.close()


def test_ingest_token_auth_on_submit(client, auth):
    """D1: POST /api/public/requests accepts Bearer gbfb_... as auth."""
    mint = client.post("/api/public/ingest-token?project_id=core", headers=auth).json()
    client.put("/api/public/surface-flags?project_id=core",
               json={"intake_enabled": True}, headers=auth)

    resp = client.post("/api/public/requests", json={
        "type": "bug",
        "title": "Test bug",
        "detail": "Something broke",
    }, headers={"Authorization": f"Bearer {mint['token']}"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["request"]["title"] == "Test bug"
    assert body["track_url"]


def test_account_field_stored_in_meta_not_by(client, auth):
    """D2: account goes to meta.account, by is derived, never 'public'."""
    token = _enable_intake(client)

    resp = client.post("/api/public/requests", json={
        "type": "feature",
        "title": "App report",
        "detail": "From the app",
        "account": {"id": "user_123", "name": "Ada", "email": "ada@app"},
        "token": token or "",
    })
    assert resp.status_code == 201
    req = resp.json()["request"]
    assert req["by"] == "Ada"
    assert req["meta"]["identity"] == "present"
    assert req["meta"]["account"]["id"] == "user_123"


def test_no_identity_stores_absent(client, auth):
    """D2: no identity → by is empty, meta.identity is 'absent'."""
    token = _enable_intake(client)

    resp = client.post("/api/public/requests", json={
        "type": "feedback",
        "title": "Anonymous feedback",
        "token": token or "",
    })
    assert resp.status_code == 201
    req = resp.json()["request"]
    assert req["by"] == ""
    assert req["meta"]["identity"] == "absent"


def test_capture_identity_gate(client, auth):
    """D3: capture_identity on → 422 when no identity."""
    db = _get_db()
    try:
        from app.services.platform import get_config
        cfg = get_config(db, "core")
        cfg.public_share_enabled = True
        cfg.intake_enabled = True
        cfg.capture_identity = True
        db.commit()
        token = cfg.share_token
    finally:
        db.close()

    resp = client.post("/api/public/requests", json={
        "type": "bug",
        "title": "No identity",
        "token": token or "",
    })
    assert resp.status_code == 422
    assert "identity" in resp.json()["detail"].lower()


def test_capture_identity_passes_with_account(client, auth):
    """D3: capture_identity on → 201 when account.id is present."""
    db = _get_db()
    try:
        from app.services.platform import get_config
        cfg = get_config(db, "core")
        cfg.public_share_enabled = True
        cfg.intake_enabled = True
        cfg.capture_identity = True
        db.commit()
        token = cfg.share_token
    finally:
        db.close()

    resp = client.post("/api/public/requests", json={
        "type": "bug",
        "title": "With identity",
        "token": token or "",
        "account": {"id": "user_456"},
    })
    assert resp.status_code == 201


def test_tracking_page_resolves(client, auth):
    """D3: tracking page resolves for a real token, 404 for unknown."""
    token = _enable_intake(client)

    resp = client.post("/api/public/requests", json={
        "type": "bug",
        "title": "Trackable",
        "token": token or "",
    })
    assert resp.status_code == 201
    track_url = resp.json()["track_url"]
    assert track_url

    track_token = track_url.split("/t/")[-1]
    track_resp = client.get(f"/api/public/t/{track_token}")
    assert track_resp.status_code == 200
    data = track_resp.json()
    assert data["title"] == "Trackable"
    assert data["status"] == "new"

    assert client.get("/api/public/t/nonexistent").status_code == 404


def test_surface_flags_crud(client, auth):
    """D4: set and read back per-surface flags."""
    resp = client.put("/api/public/surface-flags?project_id=core", json={
        "intake_enabled": True,
        "public_form_enabled": True,
        "public_issues_enabled": True,
    }, headers=auth)
    assert resp.status_code == 200
    data = resp.json()
    assert data["intake_enabled"] is True
    assert data["public_form_enabled"] is True
    assert data["public_issues_enabled"] is True
    assert data["public_requests_enabled"] is False
    assert data["public_share_enabled"] is True


def test_publish_and_board(client, auth):
    """D5: publish a request, it appears on the board; unpublish removes it."""
    db = _get_db()
    try:
        from app.services.platform import get_config
        cfg = get_config(db, "core")
        cfg.public_share_enabled = True
        cfg.intake_enabled = True
        cfg.public_issues_enabled = True
        db.commit()
        token = cfg.share_token
    finally:
        db.close()

    resp = client.post("/api/public/requests", json={
        "type": "bug",
        "title": "Board bug",
        "token": token or "",
    })
    assert resp.status_code == 201
    req_id = resp.json()["request"]["id"]

    board = client.get(f"/api/public/boards/issues?token={token}")
    assert board.status_code == 200
    assert len(board.json()) == 0

    pub = client.post(f"/api/public/requests/{req_id}/publish", headers=auth)
    assert pub.status_code == 200
    assert pub.json()["published"] is True

    board = client.get(f"/api/public/boards/issues?token={token}")
    assert len(board.json()) == 1
    row = board.json()[0]
    assert row["title"] == "Board bug"
    assert "email" not in row
    assert "by" not in row

    client.post(f"/api/public/requests/{req_id}/unpublish", headers=auth)
    board = client.get(f"/api/public/boards/issues?token={token}")
    assert len(board.json()) == 0


def test_public_board_no_secrets_leak(client, auth):
    """D5: publish a request with secrets, ensure board row is clean."""
    db = _get_db()
    try:
        from app.services.platform import get_config
        cfg = get_config(db, "core")
        cfg.public_share_enabled = True
        cfg.intake_enabled = True
        cfg.public_requests_enabled = True
        db.commit()
        token = cfg.share_token
    finally:
        db.close()

    resp = client.post("/api/public/requests", json={
        "type": "feature",
        "title": "Secret feature",
        "email": "secret@example.com",
        "account": {"id": "u1", "name": "Secret User", "email": "secret@app"},
        "meta": {"user_agent": "SecretBrowser/1.0", "app_version": "9.9"},
        "source_url": "https://secret.example.com/page",
        "token": token or "",
    })
    assert resp.status_code == 201
    req_id = resp.json()["request"]["id"]
    client.post(f"/api/public/requests/{req_id}/publish", headers=auth)

    board = client.get(f"/api/public/boards/requests?token={token}")
    rows = board.json()
    assert len(rows) == 1
    row = rows[0]
    for forbidden in ("email", "by", "source_url", "attachment_ids", "meta", "account"):
        assert forbidden not in row, f"{forbidden} leaked on public board"


def test_comment_default_private(client, auth):
    """D7: comment defaults to private, only public ones appear on tracking."""
    token = _enable_intake(client)

    resp = client.post("/api/public/requests", json={
        "type": "bug",
        "title": "Commented bug",
        "token": token or "",
    })
    req_id = resp.json()["request"]["id"]
    track_url = resp.json()["track_url"]
    track_token = track_url.split("/t/")[-1]

    client.post(f"/api/public/requests/{req_id}/comments", json={
        "body": "Internal note",
    }, headers=auth)

    client.post(f"/api/public/requests/{req_id}/comments", json={
        "body": "Public update",
        "visibility": "public",
    }, headers=auth)

    track = client.get(f"/api/public/t/{track_token}")
    assert track.status_code == 200
    comments = track.json()["comments"]
    assert len(comments) == 1
    assert comments[0]["body"] == "Public update"


def test_comment_absence_is_private(client, auth):
    """D7: absence of visibility tag is private, never public."""
    token = _enable_intake(client)

    resp = client.post("/api/public/requests", json={
        "type": "bug",
        "title": "Visibility test",
        "token": token or "",
    })
    req_id = resp.json()["request"]["id"]

    client.post(f"/api/public/requests/{req_id}/comments", json={
        "body": "Should be private",
    }, headers=auth)

    db = _get_db()
    try:
        from app.services import requests as req_svc
        comments = req_svc.list_comments(db, req_id)
        assert len(comments) == 1
        assert comments[0].visibility == "private"

        pub_comments = req_svc.list_comments(db, req_id, visibility="public")
        assert len(pub_comments) == 0
    finally:
        db.close()


def test_public_vote_sets_cookie_and_is_idempotent(client, auth):
    """D6: first vote sets gb_vote; the same cookie does not increment again."""
    db = _get_db()
    try:
        from app.services.platform import get_config
        cfg = get_config(db, "core")
        cfg.public_share_enabled = True
        cfg.intake_enabled = True
        cfg.public_issues_enabled = True
        db.commit()
        token = cfg.share_token
    finally:
        db.close()

    created = client.post("/api/public/requests", json={
        "type": "bug",
        "title": "Votable",
        "token": token or "",
    })
    assert created.status_code == 201
    req_id = created.json()["request"]["id"]
    assert client.post(f"/api/public/requests/{req_id}/publish", headers=auth).status_code == 200

    first = client.post(f"/api/public/requests/{req_id}/vote?token={token}")
    assert first.status_code == 200
    assert first.json()["voted"] is True
    assert first.json()["votes"] == 1
    cookie = first.cookies.get("gb_vote")
    assert cookie, "first vote must Set-Cookie gb_vote so uniqueness can bind"
    client.cookies.set("gb_vote", cookie)

    second = client.post(f"/api/public/requests/{req_id}/vote?token={token}")
    assert second.status_code == 200
    assert second.json()["voted"] is False
    assert second.json()["votes"] == 1


def test_slug_validation(client, auth):
    """D8: slug validation rejects reserved names and bad formats."""
    # Valid slug.
    resp = client.get("/api/public/slugs/validate?slug=acme")
    assert resp.json()["valid"] is True

    # Reserved name.
    resp = client.get("/api/public/slugs/validate?slug=www")
    assert resp.json()["valid"] is False
    assert "reserved" in resp.json()["error"]

    # Bad format (uppercase).
    resp = client.get("/api/public/slugs/validate?slug=ACME")
    assert resp.json()["valid"] is False

    # Leading hyphen.
    resp = client.get("/api/public/slugs/validate?slug=-acme")
    assert resp.json()["valid"] is False


def test_slug_claim_project_path(client, auth):
    """D8: claim a project path id."""
    resp = client.post("/api/public/slugs/project-path?project_id=core",
                       json={"slug": "mobile"}, headers=auth)
    assert resp.status_code == 200
    assert resp.json()["public_path_id"] == "mobile"

    # Claiming the same slug again for the same project is fine (idempotent).
    resp = client.post("/api/public/slugs/project-path?project_id=core",
                       json={"slug": "mobile"}, headers=auth)
    assert resp.status_code == 200

    # Reserved slug rejected.
    resp = client.post("/api/public/slugs/project-path?project_id=core",
                       json={"slug": "api"}, headers=auth)
    assert resp.status_code == 409
