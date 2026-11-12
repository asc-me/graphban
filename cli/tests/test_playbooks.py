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


# --- GRPH-1493: applying the pack to the checkout -------------------------------------------

SKILL = ".claude/skills/graphban-work/SKILL.md"
COPIES = [SKILL, ".grok/skills/graphban-work/SKILL.md", ".agents/skills/graphban-work/SKILL.md"]
LOOP = ".claude/agents/gb-worker.md"
PLAYBOOK = ".cursor/plugins/graphban/skills/why-graphban/SKILL.md"


def _pack(sha_, skill="S1", loop="L1", playbook="P1", extra=()):
    files = [{"path": SKILL, "kind": "skill", "body": skill},
             {"path": LOOP, "kind": "loop", "body": loop},
             {"path": PLAYBOOK, "kind": "playbook", "body": playbook}, *extra]
    return {"git_sha": sha_, "files": files}


def _apply(tmp_path, sha_, now, **kw):
    return poll(URL, tmp_path, client=Fake({"git_sha": sha_}, _pack(sha_, **kw)), now=now)


def test_a_skill_is_written_to_all_three_skill_directories(tmp_path):
    _apply(tmp_path, "1", 0)
    for rel in COPIES:
        assert (tmp_path / rel).read_text() == "S1"
        assert receipt(tmp_path)["files"][rel] == sha("S1")


def test_changed_skill_loop_and_playbook_are_replaced(tmp_path):
    _apply(tmp_path, "1", 0)
    r = _apply(tmp_path, "2", 5000, skill="S2", loop="L2", playbook="P2")
    for rel in COPIES:
        assert (tmp_path / rel).read_text() == "S2"
    assert (tmp_path / LOOP).read_text() == "L2"
    assert (tmp_path / PLAYBOOK).read_text() == "P2"
    assert sorted(r["written"]) == sorted([*COPIES, LOOP, PLAYBOOK])
    assert receipt(tmp_path)["git_sha"] == "2"


def test_matching_bytes_are_not_rewritten(tmp_path):
    _apply(tmp_path, "1", 0)
    before = {rel: (tmp_path / rel).stat().st_mtime_ns for rel in COPIES}
    r = _apply(tmp_path, "2", 5000, loop="L2")
    assert r["written"] == [LOOP]
    assert {rel: (tmp_path / rel).stat().st_mtime_ns for rel in COPIES} == before


def test_a_committed_matching_copy_is_adopted_not_rewritten(tmp_path):
    (tmp_path / SKILL).parent.mkdir(parents=True)
    (tmp_path / SKILL).write_text("S1")
    mtime = (tmp_path / SKILL).stat().st_mtime_ns
    r = _apply(tmp_path, "1", 0)
    assert SKILL not in r["written"] and (tmp_path / SKILL).stat().st_mtime_ns == mtime
    assert receipt(tmp_path)["files"][SKILL] == sha("S1")
    # Adopted, so the next pack replaces it.
    _apply(tmp_path, "2", 5000, skill="S2")
    assert (tmp_path / SKILL).read_text() == "S2"


def test_a_local_edit_is_left_and_named_while_the_rest_update(tmp_path):
    _apply(tmp_path, "1", 0)
    (tmp_path / LOOP).write_text("mine")
    r = _apply(tmp_path, "2", 5000, skill="S2", loop="L2")
    assert (tmp_path / LOOP).read_text() == "mine"
    assert r["kept"] == [LOOP] and LOOP in r["note"]
    assert (tmp_path / SKILL).read_text() == "S2"
    # The edit stays the person's: the receipt still holds what we wrote, not their bytes.
    assert receipt(tmp_path)["files"][LOOP] == sha("L1")
    r = _apply(tmp_path, "3", 10000, skill="S3", loop="L3")
    assert (tmp_path / LOOP).read_text() == "mine" and r["kept"] == [LOOP]


def test_an_uncommitted_differing_file_without_receipt_is_a_local_edit(tmp_path):
    (tmp_path / LOOP).parent.mkdir(parents=True)
    (tmp_path / LOOP).write_text("theirs")
    r = _apply(tmp_path, "1", 0)
    assert (tmp_path / LOOP).read_text() == "theirs" and LOOP in r["kept"]
    assert LOOP not in receipt(tmp_path)["files"]


def test_restoring_an_edit_is_delete_and_update(tmp_path):
    _apply(tmp_path, "1", 0)
    (tmp_path / LOOP).write_text("mine")
    (tmp_path / LOOP).unlink()
    r = _apply(tmp_path, "2", 5000)
    assert (tmp_path / LOOP).read_text() == "L1" and LOOP in r["written"]


def test_update_restores_a_deleted_edit_when_the_sha_is_unchanged(tmp_path):
    _apply(tmp_path, "1", 0)
    (tmp_path / LOOP).write_text("mine")
    r = _apply(tmp_path, "2", 5000, loop="L2")
    assert r["kept"] == [LOOP] and (tmp_path / LOOP).read_text() == "mine"
    (tmp_path / LOOP).unlink()
    # An ordinary poll past the hour at the same sha downloads nothing.
    fake = Fake({"git_sha": "2"}, _pack("2", loop="L2"))
    poll(URL, tmp_path, client=fake, now=10000)
    assert not (tmp_path / LOOP).exists()
    # `gban update` (force) applies even though the sha is unchanged.
    r = poll(URL, tmp_path, client=fake, now=10001, force=True)
    assert (tmp_path / LOOP).read_text() == "L2" and LOOP in r["written"]
    assert receipt(tmp_path)["files"][LOOP] == sha("L2")


def test_a_path_the_pack_dropped_is_left_on_disk_and_named(tmp_path):
    extra = [{"path": ".claude/agents/gb-old.md", "kind": "loop", "body": "old"}]
    _apply(tmp_path, "1", 0, extra=extra)
    r = _apply(tmp_path, "2", 5000)
    assert (tmp_path / ".claude/agents/gb-old.md").read_text() == "old"
    assert r["gone"] == [".claude/agents/gb-old.md"] and "gb-old.md" in r["note"]


def test_an_unreadable_entry_writes_nothing(tmp_path):
    pack = _pack("1")
    pack["files"].append({"path": "../escape.md", "kind": "loop", "body": "x"})
    r = poll(URL, tmp_path, client=Fake({"git_sha": "1"}, pack), now=0)
    assert r.get("failed")
    assert not any(p.is_file() for p in tmp_path.rglob("*"))


def test_doctor_with_a_fresh_stamp_reports_the_stored_poll_not_a_check(tmp_path):
    from gban import doctor
    _apply(tmp_path, "abcdef1234567890", 1000)
    c = Fake({"git_sha": "other"})
    r = poll(URL, tmp_path, client=c, now=1500)
    assert c.calls == []
    line = doctor.playbooks_line(r)
    assert line["status"] == doctor.UNKNOWN
    assert playbooks.when(1000) in line["detail"] and "abcdef123456" in line["detail"]
    assert "not checked" in line["detail"] and "just now" not in line["detail"]
    fresh = doctor.playbooks_line(_apply(tmp_path, "b", 9000))
    assert fresh["status"] == doctor.PASS and "just now" in fresh["detail"]


def test_gban_doctor_prints_the_playbooks_line(tmp_path, monkeypatch, capsys):
    root = _git_repo(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setattr(playbooks, "poll", lambda url, repo, **kw: {
        "polled": False, "skipped": True, "polled_at": 1000, "git_sha": "abc",
        "note": "last poll 1970-01-01 00:16:40Z at abc; not checked against the server"})
    monkeypatch.setattr(cli.doctor_mod, "resolve_supervisor_key", lambda *a, **k: "")
    monkeypatch.setattr(cli.doctor_mod, "run", lambda *a, **k: ([], 0))
    assert cli.main(["--server", URL, "doctor", "--no-report"]) == 0
    out = capsys.readouterr()
    assert "playbooks" in out.out and "at abc" in out.out and "just now" not in out.out
    assert "last poll" not in out.err


# --- GRPH-1506: hook files ride the pack, each to its own path -------------------------------

HOOK = ".cursor/hooks/gb_stop.py"
HOOKS_JSON = ".cursor/hooks.json"


def _hook_pack(sha_, *extra):
    files = [{"path": SKILL, "kind": "skill", "body": "S1"},
             {"path": HOOK, "kind": "hook", "body": "H1"}, *extra]
    return {"git_sha": sha_, "files": files}


def test_a_hook_is_written_once_at_its_own_path_and_a_skill_still_fans_out(tmp_path):
    poll(URL, tmp_path, client=Fake({"git_sha": "1"}, _hook_pack("1")), now=0)
    assert (tmp_path / HOOK).read_text() == "H1"
    assert playbooks.targets({"path": HOOK, "kind": "hook"}) == [HOOK]
    assert sorted(receipt(tmp_path)["files"]) == sorted([*COPIES, HOOK])
    for rel in COPIES:
        assert (tmp_path / rel).read_text() == "S1"
    assert not (tmp_path / ".claude/hooks").exists()


def test_a_foreign_hooks_json_is_left_and_named_while_the_scripts_are_written(tmp_path):
    (tmp_path / ".cursor").mkdir()
    (tmp_path / HOOKS_JSON).write_text("theirs")
    pack = _hook_pack("1", {"path": HOOKS_JSON, "kind": "hook", "body": "{}"})
    r = poll(URL, tmp_path, client=Fake({"git_sha": "1"}, pack), now=0)
    assert (tmp_path / HOOKS_JSON).read_text() == "theirs"
    assert HOOKS_JSON in r["kept"] and HOOKS_JSON in r["note"]
    assert HOOKS_JSON not in receipt(tmp_path)["files"]
    assert (tmp_path / HOOK).read_text() == "H1"


def _planted(tmp_path, monkeypatch):
    from gban import setup as setup_mod
    src = tmp_path / "wheel" / "SKILL.md"
    src.parent.mkdir()
    src.write_text("wheel v1")
    monkeypatch.setattr(setup_mod, "skill_source", lambda slug="graphban-work": src)
    repo = tmp_path / "repo"
    repo.mkdir()
    setup_mod.install_skill(repo, "graphban-work")
    return repo


def test_a_skill_setup_planted_is_refreshed_by_the_pack(tmp_path, monkeypatch):
    repo = _planted(tmp_path, monkeypatch)
    assert receipt(repo)["files"][SKILL] == sha("wheel v1")
    r = _apply(repo, "2", 5000, skill="S2")
    assert r["kept"] == []
    for rel in COPIES:
        assert (repo / rel).read_text() == "S2"
        assert receipt(repo)["files"][rel] == sha("S2")


def test_a_skill_setup_planted_then_edited_is_left_and_named(tmp_path, monkeypatch):
    repo = _planted(tmp_path, monkeypatch)
    (repo / SKILL).write_text("mine")
    r = _apply(repo, "2", 5000, skill="S2")
    assert r["kept"] == [SKILL]
    assert (repo / SKILL).read_text() == "mine"
    assert (repo / COPIES[2]).read_text() == "S2"
