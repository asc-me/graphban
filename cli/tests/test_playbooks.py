import hashlib
import json
import subprocess

import pytest

from gban import cli, playbooks
from gban.client import Client, NoSession, Refused, Unreachable

URL = "http://x"


class Fake:
    def __init__(self, health, pack=None):
        self.health, self.pack, self.calls = health, pack, []

    def call(self, method, path, body=None):
        self.calls.append(path)
        res = self.health if path == "/health" else self.pack
        if isinstance(res, Exception):
            raise res
        return res


def poll(url, repo, *, client, **kw):
    """The Fake serves both halves unless a test hands a separate credentialed client."""
    kw.setdefault("auth", lambda: client)
    return playbooks.poll(url, repo, client=client, **kw)


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def receipt(tmp_path):
    return json.loads((tmp_path / playbooks.RECEIPT).read_text())


def test_second_poll_within_hour_skips(tmp_path):
    c = Fake({"git_sha": "a"}, {"git_sha": "a", "files": []})
    poll(URL, tmp_path, client=c, now=1000)
    r = poll(URL, tmp_path, client=c, now=1500)
    assert c.calls == ["/health", "/api/playbooks"]
    assert not r["polled"] and "last poll" in r["note"]


def test_after_hour_and_other_server_and_force_poll(tmp_path):
    c = Fake({"git_sha": "a"}, {"git_sha": "a", "files": []})
    poll(URL, tmp_path, client=c, now=1000)
    poll(URL, tmp_path, client=c, now=1000 + 3601)
    assert c.calls.count("/health") == 2
    poll("http://y", tmp_path, client=c, now=4700)
    assert c.calls.count("/health") == 3
    poll("http://y", tmp_path, client=c, now=4701, force=True)
    assert c.calls.count("/health") == 4


def test_dropped_connection_keeps_polled_at(tmp_path):
    poll(URL, tmp_path, client=Fake(Unreachable("down")), now=1000)
    assert not (tmp_path / playbooks.RECEIPT).exists()


def test_unstamped_writes_no_file_sets_polled_at(tmp_path):
    r = poll(URL, tmp_path, client=Fake({"git_sha": ""}), now=1000)
    assert "unstamped" in r["note"] and receipt(tmp_path)["polled_at"] == 1000
    assert receipt(tmp_path)["files"] == {}


def test_404_pack(tmp_path):
    r = poll(URL, tmp_path, client=Fake({"git_sha": "a"}, Refused(404, "nf")), now=1000)
    assert "no pack" in r["note"] and receipt(tmp_path)["polled_at"] == 1000
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == [tmp_path / playbooks.RECEIPT]


def test_same_sha_does_not_download(tmp_path):
    c = Fake({"git_sha": "a"}, {"git_sha": "a", "files": []})
    poll(URL, tmp_path, client=c, now=1000)
    c.calls.clear()
    poll(URL, tmp_path, client=c, now=1000 + 4000)
    assert c.calls == ["/health"] and receipt(tmp_path)["polled_at"] == 5000


def test_ownership(tmp_path):
    def pack(sha_, body):
        return {"git_sha": sha_, "files": [{"path": "s/a.md", "body": body},
                                           {"path": "s/b.md", "body": "B"}]}
    poll(URL, tmp_path, client=Fake({"git_sha": "1"}, pack("1", "A1")), now=0)
    (tmp_path / "s/b.md").write_text("edited")
    r = poll(URL, tmp_path, client=Fake({"git_sha": "2"}, pack("2", "A2")), now=5000)
    assert (tmp_path / "s/a.md").read_text() == "A2"
    assert (tmp_path / "s/b.md").read_text() == "edited" and "s/b.md" in r["note"]


def test_pack_is_fetched_with_the_credential_not_the_bare_client(tmp_path):
    bare = Fake({"git_sha": "a"}, Refused(401, "authentication required"))
    authed = Fake(None, {"git_sha": "a", "files": [{"path": "s/a.md", "body": "A"}]})
    r = playbooks.poll(URL, tmp_path, client=bare, auth=lambda: authed, now=1000)
    assert bare.calls == ["/health"] and authed.calls == ["/api/playbooks"]
    assert (tmp_path / "s/a.md").read_text() == "A"
    assert r["polled"] and receipt(tmp_path)["git_sha"] == "a"


def test_a_401_on_the_pack_leaves_polled_at_alone(tmp_path):
    bare = Fake({"git_sha": "a"}, Refused(401, "authentication required"))
    r = playbooks.poll(URL, tmp_path, client=bare, auth=lambda: bare, now=1000)
    assert r.get("failed") and not (tmp_path / playbooks.RECEIPT).exists()


def _no_session(*a, **k):
    raise NoSession("no stored session")


def test_credentialed_falls_back_to_the_api_key(monkeypatch):
    monkeypatch.setattr(playbooks, "authenticated", _no_session)
    monkeypatch.setenv(playbooks.config.API_KEY_ENV, "gbk_env")
    made = playbooks.credentialed(URL, "proj")
    assert isinstance(made, Client) and made.api_key == "gbk_env" and not made.token


def test_credentialed_uses_the_stored_project_key(monkeypatch):
    monkeypatch.setattr(playbooks, "authenticated", _no_session)
    monkeypatch.delenv(playbooks.config.API_KEY_ENV, raising=False)
    monkeypatch.setattr(playbooks.config, "stored_supervisor_key",
                        lambda p: "gbk_stored" if p == "proj" else "")
    assert playbooks.credentialed(URL, "proj").api_key == "gbk_stored"


def test_no_credential_is_a_failed_poll_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(playbooks, "authenticated", _no_session)
    monkeypatch.delenv(playbooks.config.API_KEY_ENV, raising=False)
    monkeypatch.setattr(playbooks.config, "stored_supervisor_key", lambda p: "")
    r = playbooks.poll(URL, tmp_path, client=Fake({"git_sha": "a"}), now=1000)
    assert r.get("failed") and "credential" in r["note"]
    assert not (tmp_path / playbooks.RECEIPT).exists()


def _git_repo(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path.resolve()


def test_checkout_is_the_git_toplevel(tmp_path):
    root = _git_repo(tmp_path)
    (root / "cli" / "src").mkdir(parents=True)
    assert playbooks.checkout(root / "cli" / "src").resolve() == root
    outside = tmp_path.parent / (tmp_path.name + "-bare")
    outside.mkdir()
    assert playbooks.checkout(outside) is None


def test_command_from_a_subdirectory_polls_the_checkout_root(tmp_path, monkeypatch):
    root = _git_repo(tmp_path)
    (root / "cli").mkdir()
    monkeypatch.chdir(root / "cli")
    seen = {}

    def fake_poll(url, repo, **kw):
        seen.update(url=url, repo=repo, **kw)
        return {"note": "ok"}
    monkeypatch.setattr(playbooks, "poll", fake_poll)
    assert cli.main(["--server", URL, "update"]) == 0
    assert seen["repo"].resolve() == root and seen["force"] is True
