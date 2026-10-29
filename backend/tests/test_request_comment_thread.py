"""PRD-60 D9 / GRPH-1139: request comment thread, submitter replies, notification."""
from app.services import email as email_svc


def _enable_intake():
    from app.db import SessionLocal
    from app.services.platform import get_config
    db = SessionLocal()
    try:
        cfg = get_config(db, "core")
        cfg.public_share_enabled = True
        cfg.intake_enabled = True
        db.commit()
        return cfg.share_token
    finally:
        db.close()


def _submit(client, email=""):
    r = client.post("/api/public/requests", json={
        "type": "bug", "title": "Thread bug", "token": _enable_intake() or "", "email": email,
    })
    assert r.status_code == 201
    return r.json()["request"]["id"], r.json()["track_url"].split("/track/")[-1]


def _op(client, auth, rid, body, visibility):
    r = client.post(f"/api/public/requests/{rid}/comments",
                    json={"body": body, "visibility": visibility}, headers=auth)
    assert r.status_code == 201
    return r


def _events(rid):
    from app.db import SessionLocal
    from app.models import Event
    db = SessionLocal()
    try:
        return db.query(Event).filter(Event.action == "request_comment", Event.target_id == rid).all()
    finally:
        db.close()


def test_operator_list_roles_and_public_never_leaks_internal(client, auth):
    rid, tok = _submit(client)
    _op(client, auth, rid, "secret note", "private")
    _op(client, auth, rid, "hello there", "public")
    assert client.post(f"/api/public/t/{tok}/comments", json={"body": "thanks"}).status_code == 201

    thread = client.get(f"/api/requests/{rid}/comments", headers=auth).json()
    assert [c["role"] for c in thread] == ["internal", "team", "submitter"]
    assert all(c["author"] for c in thread[:2])

    track = client.get(f"/api/public/t/{tok}").json()
    assert [c["body"] for c in track["comments"]] == ["hello there", "thanks"]
    assert "secret note" not in str(track)


def test_submitter_comment_needs_valid_token(client):
    _submit(client)
    assert client.post("/api/public/t/not-a-token/comments", json={"body": "x"}).status_code == 404


def test_public_reply_notifies_once_internal_none(client, auth):
    rid, tok = _submit(client, email="sub@example.com")
    email_svc.outbox.clear()
    _op(client, auth, rid, "internal only", "private")
    assert email_svc.outbox == []
    _op(client, auth, rid, "we fixed it", "public")
    assert [m.to for m in email_svc.outbox] == ["sub@example.com"]
    client.post(f"/api/public/t/{tok}/comments", json={"body": "great"})
    assert len(email_svc.outbox) == 1


def test_no_email_still_records_event(client, auth):
    rid, tok = _submit(client)
    email_svc.outbox.clear()
    _op(client, auth, rid, "reply", "public")
    assert email_svc.outbox == []
    client.post(f"/api/public/t/{tok}/comments", json={"body": "ok"})
    evs = _events(rid)
    assert len(evs) == 2
    assert [e.meta["notified"] for e in evs] == [False, False]
