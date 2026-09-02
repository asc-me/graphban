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
        "asset": "",
    }


def test_fetch_latest_names_the_packed_tarball(monkeypatch):
    class _Resp:
        status_code = 200
        def json(self):
            return {
                "tag_name": "2026.09.3",
                "html_url": "https://github.com/asc-me/graphban/releases/tag/2026.09.3",
                "assets": [{
                    "name": "graphban-2026.09.3.tar.gz",
                    "browser_download_url": "https://example/graphban-2026.09.3.tar.gz",
                }],
            }
    monkeypatch.setattr(svc.httpx, "get", lambda *a, **k: _Resp())
    got = svc.fetch_latest()
    assert got["asset"] == "https://example/graphban-2026.09.3.tar.gz"


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


def test_apply_true_only_when_the_helper_socket_exists():
    got = svc.check(
        fetch=lambda: {"tag": "2026.10.1", "url": "https://example/x"},
        run={"version": "2026.09.1", "git_sha": "d596e57"},
        hosted=False,
        helper=True,
    )
    assert got["state"] == "available"
    assert got["apply"] is True


def test_hosted_never_sets_apply_even_with_a_helper():
    got = svc.check(
        fetch=lambda: {"tag": "2026.10.1", "url": "https://example/x"},
        run={"version": "2026.09.1", "git_sha": "d596e57"},
        hosted=True,
        helper=True,
    )
    assert got["apply"] is False
    assert got["hosted"] is True


def test_helper_present_is_a_socket_not_a_file(tmp_path):
    missing = tmp_path / "nope"
    assert svc.helper_present(str(missing)) is False
    regular = tmp_path / "file"
    regular.write_text("x", encoding="utf-8")
    assert svc.helper_present(str(regular)) is False


def test_apply_starts_the_advertised_tag_only():
    payload = svc.check(
        fetch=lambda: {"tag": "2026.10.1", "url": "https://example/x"},
        run={"version": "2026.09.1", "git_sha": "d596e57"},
        hosted=False,
        helper=True,
    )
    seen: list[dict] = []

    def send(msg, path=None, **kw):
        seen.append(msg)
        return {"ok": True, "started": True, "tag": msg["tag"]}

    got = svc.apply("2026.10.1", check_fn=lambda **k: payload, send=send)
    assert got["ok"] is True
    assert got["status"] == 202
    assert seen == [{"op": "apply", "tag": "2026.10.1"}]

    wrong = svc.apply("main", check_fn=lambda **k: payload, send=send)
    assert wrong["ok"] is False
    assert wrong["status"] == 409
    assert len(seen) == 1  # did not call the helper


def test_apply_hosted_is_403_and_does_not_talk():
    payload = svc.check(
        fetch=lambda: {"tag": "2026.10.1", "url": "https://example/x"},
        run={"version": "2026.09.1", "git_sha": "d596e57"},
        hosted=True,
        helper=True,
    )

    def send(msg, **kw):
        raise AssertionError("hosted must not talk to the helper")

    got = svc.apply("2026.10.1", check_fn=lambda **k: payload, send=send)
    assert got["status"] == 403


def test_apply_empty_via_is_not_compose():
    """THE CALL. apply True + empty via must not start deploy.sh."""
    payload = {
        "state": "available",
        "running": {"version": "2026.09.1", "git_sha": "d596e57"},
        "latest": {"tag": "2026.10.1", "url": "https://example/x", "asset": ""},
        "apply": True,
        "via": "",
        "hosted": False,
        "note": "",
    }

    def send(msg, **kw):
        raise AssertionError("empty via must not talk to the compose helper")

    def native_start(tag, asset, **kw):
        raise AssertionError("empty via must not start native upgrade")

    got = svc.apply(
        "2026.10.1", check_fn=lambda **k: payload, send=send, native_start=native_start)
    assert got["ok"] is False
    assert got["status"] == 503
    assert "not compose" in got["error"]


def test_rest_apply_empty_via_is_not_compose(client, auth, monkeypatch):
    """Sabotage the CALL: apply() defaulting empty via to compose would 202."""
    monkeypatch.setattr(
        svc, "check",
        lambda **k: {
            "state": "available",
            "running": {"version": "2026.09.1", "git_sha": "d596e57"},
            "latest": {"tag": "2026.10.1", "url": "https://example/x", "asset": ""},
            "apply": True,
            "via": "",
            "hosted": False,
            "note": "",
        },
    )

    def talk(msg, **kw):
        raise AssertionError("empty via must not talk to the compose helper")

    monkeypatch.setattr(svc, "talk", talk)
    r = client.post(
        "/api/platform/update-apply",
        headers=auth,
        json={"tag": "2026.10.1"},
    )
    assert r.status_code == 503, r.text
    assert "not compose" in r.json()["detail"]


def test_rest_apply_call_posts_the_tag(client, auth, monkeypatch):
    """Sabotage the CALL: deleting the router branch would 404 while unit tests pass."""
    monkeypatch.setattr(
        svc, "fetch_latest",
        lambda: {"tag": "2026.10.1", "url": "https://example/x"},
    )
    monkeypatch.setattr(
        svc, "running",
        lambda: {"version": "2026.09.1", "git_sha": "d596e57"},
    )
    monkeypatch.setattr(svc, "helper_present", lambda path=None: True)
    monkeypatch.setattr(
        svc, "talk",
        lambda msg, path=None, **kw: {"ok": True, "started": True, "tag": msg["tag"]},
    )
    r = client.post(
        "/api/platform/update-apply",
        headers=auth,
        json={"tag": "2026.10.1"},
    )
    assert r.status_code == 202, r.text
    assert r.json()["started"] is True
    assert r.json()["tag"] == "2026.10.1"


def test_rest_apply_without_helper_is_503(client, auth, monkeypatch):
    monkeypatch.setattr(
        svc, "fetch_latest",
        lambda: {"tag": "2026.10.1", "url": "https://example/x"},
    )
    monkeypatch.setattr(
        svc, "running",
        lambda: {"version": "2026.09.1", "git_sha": "d596e57"},
    )
    monkeypatch.setattr(svc, "helper_present", lambda path=None: False)
    monkeypatch.setattr(svc, "native_present", lambda root=None: False)
    r = client.post(
        "/api/platform/update-apply",
        headers=auth,
        json={"tag": "2026.10.1"},
    )
    assert r.status_code == 503


def test_rest_apply_requires_jwt(client):
    assert client.post("/api/platform/update-apply", json={"tag": "x"}).status_code in (401, 403)


def test_native_sets_apply_without_a_helper():
    got = svc.check(
        fetch=lambda: {"tag": "2026.10.1", "url": "https://example/x",
                       "asset": "https://example/graphban-2026.10.1.tar.gz"},
        run={"version": "2026.09.1", "git_sha": "d596e57"},
        hosted=False,
        helper=False,
        native=True,
    )
    assert got["apply"] is True
    assert got["via"] == "native"


def test_compose_helper_wins_over_native():
    got = svc.check(
        fetch=lambda: {"tag": "2026.10.1", "url": "https://example/x"},
        run={"version": "2026.09.1", "git_sha": "d596e57"},
        hosted=False,
        helper=True,
        native=True,
    )
    assert got["via"] == "compose"


def test_hosted_never_sets_apply_even_when_native():
    got = svc.check(
        fetch=lambda: {"tag": "2026.10.1", "url": "https://example/x"},
        run={"version": "2026.09.1", "git_sha": "d596e57"},
        hosted=True,
        helper=False,
        native=True,
    )
    assert got["apply"] is False
    assert got["via"] == ""


def test_native_apply_starts_upgrade_not_the_compose_helper(tmp_path):
    """THE CALL. Native Install must popen graphban_host.py upgrade with the
    unpacked tree. A path that talks to the compose socket, or that unpacks
    GitHub's source zip, is the wrong topology wearing a 202.
    """
    root = tmp_path / "opt"
    (root / "current" / "scripts").mkdir(parents=True)
    (root / "current" / "backend").mkdir()
    (root / "current" / "scripts" / "graphban_host.py").write_text("# host\n", encoding="utf-8")
    packed = tmp_path / "graphban-2026.10.1"
    packed.mkdir()
    (packed / "GIT_SHA").write_text("deadbee\n", encoding="utf-8")
    (packed / "backend").mkdir()
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(packed, arcname="graphban-2026.10.1")
    blob = buf.getvalue()
    seen: list[list[str]] = []

    def popen(cmd, **kw):
        seen.append(list(cmd))
        return None

    payload = svc.check(
        fetch=lambda: {"tag": "2026.10.1", "url": "https://example/x",
                       "asset": "https://example/graphban-2026.10.1.tar.gz"},
        run={"version": "2026.09.1", "git_sha": "d596e57"},
        hosted=False, helper=False, native=True,
    )

    def starter(tag, asset):
        return svc.start_native(
            tag, asset, root=str(root), download=lambda url: blob, popen=popen)

    got = svc.apply("2026.10.1", check_fn=lambda **k: payload, native_start=starter)
    assert got["ok"] is True
    assert got["via"] == "native"
    assert got["sha"] == "deadbee"
    assert seen, "upgrade was not started"
    cmd = seen[0]
    assert "upgrade" in cmd
    assert "--root" in cmd and str(root) in cmd
    assert "--release" in cmd
    assert "--sha" in cmd and "deadbee" in cmd
    assert "docker.sock" not in " ".join(cmd)


def test_native_apply_without_a_tarball_is_not_the_source_zip():
    payload = svc.check(
        fetch=lambda: {"tag": "2026.10.1", "url": "https://example/x", "asset": ""},
        run={"version": "2026.09.1", "git_sha": "d596e57"},
        hosted=False, helper=False, native=True,
    )
    got = svc.apply("2026.10.1", check_fn=lambda **k: payload,
                    native_start=lambda tag, asset: svc.start_native(tag, asset))
    assert got["ok"] is False
    assert got["status"] == 502
    assert "tarball" in got["error"]


def test_rest_native_apply_call(client, auth, monkeypatch):
    monkeypatch.setattr(
        svc, "fetch_latest",
        lambda: {"tag": "2026.10.1", "url": "https://example/x",
                 "asset": "https://example/graphban-2026.10.1.tar.gz"},
    )
    monkeypatch.setattr(svc, "running", lambda: {"version": "2026.09.1", "git_sha": "d596e57"})
    monkeypatch.setattr(svc, "helper_present", lambda path=None: False)
    monkeypatch.setattr(svc, "native_present", lambda root=None: True)
    monkeypatch.setattr(
        svc, "start_native",
        lambda tag, asset, **kw: {"ok": True, "started": True, "tag": tag, "sha": "deadbee"},
    )
    r = client.post("/api/platform/update-apply", headers=auth, json={"tag": "2026.10.1"})
    assert r.status_code == 202, r.text
    assert r.json()["via"] == "native"
    assert r.json()["sha"] == "deadbee"
