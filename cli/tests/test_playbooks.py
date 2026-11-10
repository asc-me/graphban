import hashlib
import json

from gban import playbooks
from gban.client import Refused, Unreachable

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


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def receipt(tmp_path):
    return json.loads((tmp_path / playbooks.RECEIPT).read_text())


def test_second_poll_within_hour_skips(tmp_path):
    c = Fake({"git_sha": "a"}, {"git_sha": "a", "files": []})
    playbooks.poll(URL, tmp_path, client=c, now=1000)
    r = playbooks.poll(URL, tmp_path, client=c, now=1500)
    assert c.calls == ["/health", "/api/playbooks"]
    assert not r["polled"] and "last poll" in r["note"]


def test_after_hour_and_other_server_and_force_poll(tmp_path):
    c = Fake({"git_sha": "a"}, {"git_sha": "a", "files": []})
    playbooks.poll(URL, tmp_path, client=c, now=1000)
    playbooks.poll(URL, tmp_path, client=c, now=1000 + 3601)
    assert c.calls.count("/health") == 2
    playbooks.poll("http://y", tmp_path, client=c, now=4700)
    assert c.calls.count("/health") == 3
    playbooks.poll("http://y", tmp_path, client=c, now=4701, force=True)
    assert c.calls.count("/health") == 4


def test_dropped_connection_keeps_polled_at(tmp_path):
    playbooks.poll(URL, tmp_path, client=Fake(Unreachable("down")), now=1000)
    assert not (tmp_path / playbooks.RECEIPT).exists()


def test_unstamped_writes_no_file_sets_polled_at(tmp_path):
    r = playbooks.poll(URL, tmp_path, client=Fake({"git_sha": ""}), now=1000)
    assert "unstamped" in r["note"] and receipt(tmp_path)["polled_at"] == 1000
    assert receipt(tmp_path)["files"] == {}


def test_404_pack(tmp_path):
    r = playbooks.poll(URL, tmp_path, client=Fake({"git_sha": "a"}, Refused(404, "nf")), now=1000)
    assert "no pack" in r["note"] and receipt(tmp_path)["polled_at"] == 1000
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == [tmp_path / playbooks.RECEIPT]


def test_same_sha_does_not_download(tmp_path):
    c = Fake({"git_sha": "a"}, {"git_sha": "a", "files": []})
    playbooks.poll(URL, tmp_path, client=c, now=1000)
    c.calls.clear()
    playbooks.poll(URL, tmp_path, client=c, now=1000 + 4000)
    assert c.calls == ["/health"] and receipt(tmp_path)["polled_at"] == 5000


def test_ownership(tmp_path):
    def pack(sha_, body):
        return {"git_sha": sha_, "files": [{"path": "s/a.md", "body": body},
                                           {"path": "s/b.md", "body": "B"}]}
    playbooks.poll(URL, tmp_path, client=Fake({"git_sha": "1"}, pack("1", "A1")), now=0)
    (tmp_path / "s/b.md").write_text("edited")
    r = playbooks.poll(URL, tmp_path, client=Fake({"git_sha": "2"}, pack("2", "A2")), now=5000)
    assert (tmp_path / "s/a.md").read_text() == "A2"
    assert (tmp_path / "s/b.md").read_text() == "edited" and "s/b.md" in r["note"]
