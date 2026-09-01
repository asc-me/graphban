"""Instance update check — three states, unknown is not current (P32)."""
from app.services import instance_update as svc


def test_placeholder_version_is_unknown_even_when_the_feed_matches():
    """0.1.0 matching a 0.1.0 tag would read as current. That is the lie this exists to stop."""
    got = svc.check(
        fetch=lambda: {"tag": "0.1.0", "url": "https://example/0.1.0"},
        run={"version": "0.1.0", "git_sha": "abc"},
        hosted=False,
    )
    assert got["state"] == "unknown"
    assert got["apply"] is False
    assert "not current" in got["note"]


def test_unreachable_feed_is_unknown_not_current():
    got = svc.check(
        fetch=lambda: None,
        run={"version": "2026.09.1", "git_sha": "d596e57"},
        hosted=False,
    )
    assert got["state"] == "unknown"
    assert got["latest"] is None
    assert "not current" in got["note"]


def test_matching_cut_is_current():
    got = svc.check(
        fetch=lambda: {"tag": "2026.09.1", "url": "https://github.com/asc-me/graphban/releases/tag/2026.09.1"},
        run={"version": "2026.09.1", "git_sha": "d596e57"},
        hosted=False,
    )
    assert got["state"] == "current"
    assert got["latest"]["tag"] == "2026.09.1"
    assert got["apply"] is False


def test_newer_tag_is_available():
    got = svc.check(
        fetch=lambda: {"tag": "2026.10.1", "url": "https://example/2026.10.1"},
        run={"version": "2026.09.1", "git_sha": "d596e57"},
        hosted=True,
    )
    assert got["state"] == "available"
    assert got["hosted"] is True
    assert got["apply"] is False


def test_v_prefix_on_the_tag_does_not_invent_a_mismatch():
    got = svc.check(
        fetch=lambda: {"tag": "v2026.09.1", "url": "https://example"},
        run={"version": "2026.09.1", "git_sha": "d596e57"},
    )
    assert got["state"] == "current"
    assert got["latest"]["tag"] == "2026.09.1"


def test_fetch_latest_strips_v_and_drops_empty(monkeypatch):
    class _Resp:
        status_code = 200
        def json(self):
            return {"tag_name": "v2026.09.1", "html_url": "https://github.com/asc-me/graphban/releases/tag/2026.09.1"}

    monkeypatch.setattr(svc.httpx, "get", lambda *a, **k: _Resp())
    got = svc.fetch_latest()
    assert got == {
        "tag": "2026.09.1",
        "url": "https://github.com/asc-me/graphban/releases/tag/2026.09.1",
    }


def test_fetch_latest_none_on_timeout(monkeypatch):
    def boom(*a, **k):
        raise svc.httpx.TimeoutException("nope")
    monkeypatch.setattr(svc.httpx, "get", boom)
    assert svc.fetch_latest() is None


def test_rest_call_returns_available_not_a_green_check(client, auth, monkeypatch):
    """Sabotage the CALL: the page consumes this JSON. A handler that always says
    current would make Settings look up to date while a newer cut exists."""
    monkeypatch.setattr(
        svc, "fetch_latest",
        lambda: {"tag": "2026.10.1", "url": "https://example/2026.10.1"},
    )
    monkeypatch.setattr(
        svc, "running",
        lambda: {"version": "2026.09.1", "git_sha": "d596e57"},
    )
    # Also pin the default lookup inside check() so a bind-time default cannot
    # quietly keep hitting GitHub (the CALL this test exists to catch).
    r = client.get("/api/platform/update-check", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "available"
    assert body["apply"] is False
    assert "Apply" not in r.text


def test_rest_call_unknown_when_feed_is_down(client, auth, monkeypatch):
    monkeypatch.setattr(svc, "fetch_latest", lambda: None)
    monkeypatch.setattr(
        svc, "running",
        lambda: {"version": "2026.09.1", "git_sha": "d596e57"},
    )
    body = client.get("/api/platform/update-check", headers=auth).json()
    assert body["state"] == "unknown"
    assert body["latest"] is None


def test_rest_requires_jwt(client):
    assert client.get("/api/platform/update-check").status_code in (401, 403)
