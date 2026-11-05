"""Discover newer rule packs on the releases feed and offer one by version (GRPH-1334)."""
from __future__ import annotations

import pytest

from app.config import settings
from app.services import rule_pack as P
from test_rule_pack import (  # noqa: F401  (fixtures reused: _clean is autouse there)
    FIX, TRUST, _body, _clean, _login, build, trusted,
)

FEED = "https://feed.test/releases"


def _pack(version, *, min_server="2026.10.1", api="1.0.0"):
    return build(FIX / "api1", {"pack.toml": (
        f'name = "graphban-rules-fixture"\nversion = "{version}"\n'
        f'primitive_api = "{api}"\nmin_server = "{min_server}"\n')})


class _R:
    def __init__(self, status=200, body=None, content=b""):
        self.status_code, self._b, self.content = status, body, content

    def json(self):
        if self._b is None:
            raise ValueError("not json")
        return self._b


class Feed:
    """A local stand-in for the GitHub releases feed and its asset downloads."""

    def __init__(self):
        self.releases, self.files, self.fail = [], {}, False

    def add(self, version, pack=None, notes="", **kw):
        art, sig = pack or _pack(version, **kw)
        base = f"https://dl.test/{version}"
        self.files[f"{base}/a.tar"], self.files[f"{base}/a.sig"] = art, sig
        self.releases.append({"tag_name": f"pack-v{version}", "body": notes, "assets": [
            {"name": f"graphban-rules-{version}.tar", "browser_download_url": f"{base}/a.tar"},
            {"name": f"graphban-rules-{version}.tar.sig",
             "browser_download_url": f"{base}/a.sig"}]})

    def get(self, url, **_):
        if self.fail:
            return _R(503)
        if url == FEED:
            return _R(body=self.releases)
        return _R(content=self.files[url]) if url in self.files else _R(404)


@pytest.fixture()
def feed(monkeypatch, trusted):
    f = Feed()
    monkeypatch.setattr(settings, "rule_pack_feed_url", FEED)
    monkeypatch.setattr(P.httpx, "get", f.get)
    P._available_cache.clear()
    yield f
    P._available_cache.clear()


def _avail(client, h):
    return client.get("/api/rules/pack/available?refresh=true", headers=h)


def test_available_current_and_unknown_and_available(client, feed):
    h = _login(client)
    feed.add("1.0.0")
    # nothing installed, a loadable pack on the feed
    r = _avail(client, h).json()
    assert r["state"] == "available" and r["version"] == "1.0.0"
    art, sig = feed.files["https://dl.test/1.0.0/a.tar"], feed.files["https://dl.test/1.0.0/a.sig"]
    assert client.post("/api/rules/pack/accept", json=_body(art, sig, sha256=P.sha256_of(art)),
                       headers=h).status_code == 200
    r = _avail(client, h).json()
    assert r["state"] == "current" and r["installed_version"] == "1.0.0"
    feed.add("1.1.0", notes="what changed")
    r = _avail(client, h).json()
    assert r["state"] == "available" and r["version"] == "1.1.0"
    assert r["notes"] == "what changed" and r["installed_version"] == "1.0.0"
    feed.fail = True
    r = _avail(client, h).json()
    assert r["state"] == "unknown" and r["feed_error"]


def test_feed_error_is_never_current(client, feed):
    h = _login(client)
    feed.fail = True
    r = _avail(client, h).json()
    assert r["state"] != "current" and r["state"] == "unknown" and "503" in r["feed_error"]


def test_pack_needing_a_newer_server_is_not_offered(client, feed):
    h = _login(client)
    feed.add("1.0.0")
    feed.add("2.0.0", min_server="2099.1.1")
    r = _avail(client, h).json()
    assert r["state"] == "available" and r["version"] == "1.0.0"


def test_incompatible_primitive_api_is_not_offered(client, feed):
    h = _login(client)
    feed.add("1.0.0")
    feed.add("2.0.0", api="2.0.0")
    assert _avail(client, h).json()["version"] == "1.0.0"


def test_offer_by_version_matches_an_upload_of_the_same_bytes(client, feed):
    h = _login(client)
    feed.add("1.0.0")
    art, sig = feed.files["https://dl.test/1.0.0/a.tar"], feed.files["https://dl.test/1.0.0/a.sig"]
    up = client.post("/api/rules/pack/offer", json=_body(art, sig), headers=h).json()
    by = client.post("/api/rules/pack/offer", json={"version": "1.0.0"}, headers=h)
    assert by.status_code == 200, by.text
    assert by.json() == up and by.json()["sha256"] == P.sha256_of(art)
    assert client.get("/api/rules/pack", headers=h).json()["installed"] is False


def test_tampered_feed_asset_is_refused_and_stale_sha_is_409(client, feed):
    h = _login(client)
    feed.add("1.0.0")
    k = "https://dl.test/1.0.0/a.tar"
    good = feed.files[k]
    offered = client.post("/api/rules/pack/offer", json={"version": "1.0.0"}, headers=h).json()
    ok = client.post("/api/rules/pack/accept",
                     json={"version": "1.0.0", "sha256": "0" * 64}, headers=h)
    assert ok.status_code == 409
    feed.files[k] = good[:-1] + bytes([good[-1] ^ 1])
    r = client.post("/api/rules/pack/offer", json={"version": "1.0.0"}, headers=h)
    assert r.status_code == 422 and "does not verify" in r.text
    r = client.post("/api/rules/pack/accept",
                    json={"version": "1.0.0", "sha256": offered["sha256"]}, headers=h)
    assert r.status_code in (409, 422)
    feed.files[k] = good
    r = client.post("/api/rules/pack/accept",
                    json={"version": "1.0.0", "sha256": offered["sha256"]}, headers=h)
    assert r.status_code == 200 and r.json()["version"] == "1.0.0"


def test_api_key_gets_401_on_all_three_routes(client, feed):
    h = _login(client)
    pid = client.get("/api/projects", headers=h).json()[0]["id"]
    key = client.post("/api/api-keys", json={"name": "k", "project_id": pid},
                      headers=h).json()["plaintext"]
    k = {"X-API-Key": key}
    assert client.get("/api/rules/pack/available", headers=k).status_code == 401
    assert client.post("/api/rules/pack/offer", json={"version": "1.0.0"},
                       headers=k).status_code == 401
    assert client.post("/api/rules/pack/accept", json={"version": "1.0.0", "sha256": "x"},
                       headers=k).status_code == 401
