"""`gbfleet drain remove` and `gbfleet drain rekey <name>` (GRPH-1976).

Nothing here touches the real machine. `conftest.nowhere_near_your_home` points
`service.CONFIG_DIR`, `unit_dir` and `env_path_for` at a per-test temp tree, and every test:

- sets `$HOME` and `$GRAPHBAN_HOME` to temp directories;
- replaces `service.host()` with launchd or systemd (each test runs on both) and `service._run`
  with the fake supervisor from `test_drain_create`, extended to restart and unload. Its
  start runs the unit's command once for real against a stub `gbfleet`, and then makes the
  service's FIRST CALL: it reads the env file the unit points at and presents that key to the
  fake ledger. That call is the fake first-call signal `rekey` waits for (`last_used`);
- replaces `drain.open_ledger` with a fake ledger that mints `gb_test_<hex>` keys, records
  every mint, revoke and call, and refuses a call with a revoked key the way the server does.

Every checkout and every key lives under the test's `tmp_path`.
"""
from __future__ import annotations

import os
import secrets
import socket
import subprocess
from pathlib import Path

import pytest

from gbfleet import client as client_mod
from gbfleet import drain
from gbfleet import service
from gbfleet.cli import main

from test_drain_create import FakeSupervisor

posix_only = pytest.mark.skipif(os.name == "nt", reason="launchd / systemd units and a /bin/sh stub")
pytestmark = posix_only

PRD = "GRPH-P96"
#: The listing's display fragment: `gb_test_` and four hex characters, the server's shape.
PREFIX_LEN = len("gb_test_") + 4


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


# ---- fakes --------------------------------------------------------------------------------

class KeyRefused(RuntimeError):
    """What the server answers a revoked or unknown key: 401."""


class FakeLedger:
    """The server's key registry, as the session sees it. Holds plaintexts only to play the
    server's hash check; nothing it returns from `list_keys` carries one."""

    def __init__(self) -> None:
        self.keys: dict[str, dict] = {}
        self.events: list[tuple[str, str]] = []
        self.minted: list[tuple[str, str, str]] = []
        self.opened: list[str] = []

    def add(self, key: str, label: str = "shared") -> str:
        key_id = f"key_{len(self.keys) + 1:02d}"
        self.keys[key_id] = {"id": key_id, "name": label, "prefix": key[:PREFIX_LEN],
                             "revoked": False, "last_used": None, "plaintext": key}
        return key_id

    def open(self, server: str) -> "FakeLedger":
        self.opened.append(server)
        return self

    def mint_prd_key(self, prd: str, label: str, *, project: str = "") -> dict:
        key = "gb_test_" + secrets.token_hex(16)
        key_id = self.add(key, label)
        self.minted.append((prd, label, project))
        self.events.append(("mint", key_id))
        return {"id": key_id, "plaintext": key, "project_id": project}

    def list_keys(self) -> list[dict]:
        return [{k: v for k, v in row.items() if k != "plaintext"} for row in self.keys.values()]

    def revoke_key(self, key_id: str) -> None:
        self.keys[key_id]["revoked"] = True
        self.events.append(("revoke", key_id))

    def call(self, key: str) -> str:
        """An authenticated call with `key`. Refused for a revoked or unknown key."""
        for row in self.keys.values():
            if row["plaintext"] == key:
                if row["revoked"]:
                    raise KeyRefused(f"{row['id']} is revoked")
                row["last_used"] = row["last_used"] or "2026-11-27T00:00:00Z"
                self.events.append(("call", row["id"]))
                return row["id"]
        raise KeyRefused("unknown key")

    def id_of(self, key: str) -> str:
        return next(r["id"] for r in self.keys.values() if r["plaintext"] == key)

    def plaintexts(self) -> list[str]:
        return [row["plaintext"] for row in self.keys.values()]


class Supervisor(FakeSupervisor):
    """`FakeSupervisor` plus restart and unload. A start runs the unit once, then makes the
    service's first call with whatever key the unit's env file holds at that moment."""

    def __init__(self, kind: str, ledger: FakeLedger) -> None:
        super().__init__(kind)
        self.ledger = ledger
        #: Set False to model a service that never comes up on its new key.
        self.calls_home = True
        self.unloaded: list[str] = []
        self.restarted: list[str] = []

    def __call__(self, cmd: list[str], timeout: float = 20.0) -> tuple[int, str]:
        if cmd[:3] == ["launchctl", "kickstart", "-k"]:
            self.calls.append(list(cmd))
            name = cmd[3].split("/", 2)[2][len(service.LABEL_PREFIX) + 1:]
            self.restarted.append(name)
            self._first_run_launchd(service.unit_path_for(name, "launchd"))
            self._first_call(name)
            return 0, ""
        if cmd[:3] == ["systemctl", "--user", "restart"]:
            self.calls.append(list(cmd))
            name = cmd[3][len(service.UNIT_PREFIX):-len(".service")]
            self.restarted.append(name)
            self._first_run_systemd(cmd[3])
            self._first_call(name)
            return 0, ""
        if cmd[:2] == ["launchctl", "bootout"]:
            self.unloaded.append(cmd[2].split("/", 2)[2][len(service.LABEL_PREFIX) + 1:])
        if cmd[:4] == ["systemctl", "--user", "disable", "--now"]:
            self.unloaded.append(cmd[4][len(service.UNIT_PREFIX):-len(".service")])
        return super().__call__(cmd, timeout)

    def _first_call(self, name: str) -> None:
        if not self.calls_home:
            return
        key = drain._read_key(service.env_path_for(name))
        try:
            self.ledger.call(key)
        except KeyRefused:
            pass


# ---- the world ----------------------------------------------------------------------------

@pytest.fixture(params=["launchd", "systemd"])
def kind(request) -> str:
    return request.param


@pytest.fixture
def world(tmp_path, monkeypatch, kind):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GRAPHBAN_HOME", str(tmp_path / "graphban"))
    monkeypatch.setattr(service, "host", lambda: service.Host(kind))
    monkeypatch.setattr(service, "linger", lambda: True)
    monkeypatch.setattr(service, "SETTLE", 0)
    monkeypatch.setattr(service, "HEALTH_WINDOW", 0)
    monkeypatch.setattr(service, "HEALTH_POLL", 0)
    monkeypatch.setattr(drain, "FIRST_CALL_WINDOW", 0)
    monkeypatch.setattr(drain, "FIRST_CALL_POLL", 0)
    ledger = FakeLedger()
    monkeypatch.setattr(drain, "open_ledger", ledger.open)
    fake = Supervisor(kind, ledger)
    monkeypatch.setattr(service, "_run", fake)

    stub = tmp_path / "bin" / "gbfleet"
    stub.parent.mkdir()
    stub.write_text(
        "#!/bin/sh\n"
        'echo "gbfleet $*"\n'
        'if [ -n "$GBFLEET_API_KEY" ]; then echo "key: present (${#GBFLEET_API_KEY} chars)"; fi\n'
        "echo '{\"reason\": \"idle\", \"exit\": 0}'\n", encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setattr(service, "_this_gbfleet", lambda: str(stub))

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "t@example.invalid")
    _git(seed, "config", "user.name", "T")
    (seed / "README.md").write_text("x\n", encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-qm", "first")
    _git(seed, "push", "-q", str(origin), "main")
    return {"kind": kind, "tmp": tmp_path, "origin": origin, "stub": stub, "fake": fake,
            "ledger": ledger}


def _install(world, name: str, key: str, *, marker: str | None = None,
             record_id: bool = True) -> Path:
    """A drain the way `drain create` leaves it: a clone (marked `marker`), a unit, an env
    file. `record_id=False` is a drain someone set up by hand with a copied key."""
    repo = world["tmp"] / "checkouts" / name
    repo.parent.mkdir(exist_ok=True)
    subprocess.run(["git", "clone", "-q", str(world["origin"]), str(repo)], check=True)
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "T")
    if marker is not None:
        drain._write_marker(repo, marker)
    plan = service.make_plan(
        ["--repo", str(repo), "--server", "http://ledger.invalid:9", "--project", "proj",
         "--adapter", "claude", "--prd", PRD],
        name=name, kind=world["kind"], binary=str(world["stub"]))
    plan.unit_path.parent.mkdir(parents=True, exist_ok=True)
    plan.log_path.parent.mkdir(parents=True, exist_ok=True)
    plan.unit_path.write_bytes(service.render(plan))
    service.write_env(plan, key)
    if record_id:
        drain._record_key_id(name, world["ledger"].id_of(key))
    return repo


def _key(world) -> str:
    key = "gb_test_" + secrets.token_hex(16)
    world["ledger"].add(key)
    return key


def _cli(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(["drain", *argv])
    got = capsys.readouterr()
    return code, got.out, got.err


def _unpushed(repo: Path) -> None:
    (repo / "work.txt").write_text("mine\n", encoding="utf-8")
    _git(repo, "add", "work.txt")
    _git(repo, "commit", "-qm", "work nobody pushed")


# ---- clause 1: remove revokes, uninstalls, keeps a checkout with unpushed work ------------

@pytest.mark.parametrize("spoil", ["unpushed", "uncommitted"])
def test_remove_revokes_uninstalls_and_keeps_a_checkout_with_unpushed_work(world, capsys, spoil):
    key = _key(world)
    repo = _install(world, "d1", key, marker="d1")
    world["ledger"].call(key)  # the key works before
    if spoil == "unpushed":
        _unpushed(repo)
    else:
        (repo / "README.md").write_text("edited, not committed\n", encoding="utf-8")

    code, out, err = _cli(capsys, "remove", "d1")
    assert code == 0, err
    with pytest.raises(KeyRefused):
        world["ledger"].call(key)
    assert ("revoke", world["ledger"].id_of(key)) in world["ledger"].events
    assert "d1" in world["fake"].unloaded
    assert not service.unit_path_for("d1", world["kind"]).exists()
    assert not service.env_path_for("d1").exists()
    assert not drain.key_id_path("d1").exists()
    assert repo.is_dir() and (repo / "README.md").exists()
    assert f"checkout: kept {repo}" in out
    assert ("unpushed commit" if spoil == "unpushed" else "uncommitted changes") in out


# ---- clause 2: remove deletes a clean checkout it owns, keeps and names any other ---------

def test_remove_deletes_a_clean_checkout_whose_marker_names_the_drain(world, capsys):
    key = _key(world)
    repo = _install(world, "d2", key, marker="d2")
    code, out, err = _cli(capsys, "remove", "d2")
    assert code == 0, err
    assert not repo.exists()
    assert f"checkout: deleted {repo}" in out
    assert repo.parent.is_dir(), "only the checkout goes, not its parent"


@pytest.mark.parametrize("marker", [None, "someone-else"])
def test_remove_keeps_and_names_a_checkout_with_no_marker_or_another_drains(
        world, capsys, marker):
    key = _key(world)
    repo = _install(world, "d3", key, marker=marker)
    code, out, err = _cli(capsys, "remove", "d3")
    assert code == 0, err
    assert repo.is_dir() and (repo / ".git").is_dir()
    assert f"checkout: kept {repo}" in out
    assert ("no .git/gbfleet-drain marker" if marker is None
            else "names drain 'someone-else'") in out
    with pytest.raises(KeyRefused):
        world["ledger"].call(key)


def test_remove_does_not_revoke_a_key_another_drain_still_holds(world, capsys):
    shared = _key(world)
    _install(world, "a", shared, marker="a", record_id=False)
    _install(world, "b", shared, marker="b", record_id=False)
    code, out, err = _cli(capsys, "remove", "a")
    assert code == 0, err
    assert "key: NOT revoked" in out and "b" in out
    assert world["ledger"].call(shared)
    assert service.env_path_for("b").exists()


def test_remove_with_no_session_changes_nothing(world, capsys, monkeypatch):
    key = _key(world)
    repo = _install(world, "d4", key, marker="d4")
    monkeypatch.setattr(drain, "open_ledger", client_mod.LedgerSession.open)
    code, _out, err = _cli(capsys, "remove", "d4")
    assert code == drain.EXIT_NO_SESSION and "login" in err
    assert service.unit_path_for("d4", world["kind"]).exists()
    assert service.env_path_for("d4").exists() and repo.exists()
    assert world["fake"].unloaded == []


# ---- clause 3: rekey leaves the service running on a new key, the old one revoked ---------

def test_rekey_runs_the_service_on_a_new_key_and_revokes_the_old_after_its_first_call(
        world, capsys):
    ledger = world["ledger"]
    old = _key(world)
    _install(world, "d5", old, marker="d5")
    old_id = ledger.id_of(old)

    code, out, err = _cli(capsys, "rekey", "d5")
    assert code == 0, err
    new = drain._read_key(service.env_path_for("d5"))
    assert new and new != old
    new_id = ledger.id_of(new)
    assert ledger.minted == [(PRD, f"d5@{socket.gethostname()}", "proj")]
    assert drain.key_id_path("d5").read_text(encoding="utf-8").strip() == new_id
    assert world["fake"].restarted == ["d5"]
    assert service.unit_path_for("d5", world["kind"]).exists()
    # The restarted service called home with the new key BEFORE the old one was revoked.
    events = ledger.events
    assert events.index(("call", new_id)) < events.index(("revoke", old_id))
    assert ledger.call(new) == new_id
    with pytest.raises(KeyRefused):
        ledger.call(old)
    assert f"old key: revoked ({old_id})" in out


def test_rekey_leaves_the_old_key_alive_when_the_new_one_is_never_used(world, capsys):
    ledger = world["ledger"]
    old = _key(world)
    _install(world, "d6", old, marker="d6")
    world["fake"].calls_home = False
    code, _out, err = _cli(capsys, "rekey", "d6")
    assert code == 1
    assert "old key is left alive" in err
    assert ledger.call(old)
    assert not any(e[0] == "revoke" for e in ledger.events)


# ---- clause 4: a copied shared key is revoked only when no other drain holds it -----------

def test_rekey_of_a_shared_key_drain_leaves_the_others_running_and_names_them(world, capsys):
    ledger = world["ledger"]
    shared = _key(world)
    for name in ("a", "b", "c"):
        _install(world, name, shared, marker=name, record_id=False)

    code, out, err = _cli(capsys, "rekey", "a")
    assert code == 0, err
    assert "old key: NOT revoked" in out and "b, c" in out
    assert ledger.call(shared), "b and c still call with the shared key"
    assert not any(e[0] == "revoke" for e in ledger.events)
    for other in ("b", "c"):
        assert _same_file_key(other, shared)
    mine = drain._read_key(service.env_path_for("a"))
    assert mine != shared
    assert ledger.keys[ledger.id_of(mine)]["name"] == f"a@{socket.gethostname()}"

    # b next: c still shares it. Then c: nobody does, but no create ever recorded the shared
    # key's id, so it is left alive and the person is sent to Settings with the candidates.
    assert _cli(capsys, "rekey", "b")[0] == 0
    assert ledger.call(shared)
    code, out, err = _cli(capsys, "rekey", "c")
    assert code == drain.EXIT_MANUAL_REVOKE, err
    assert ledger.call(shared)
    assert not any(e[0] == "revoke" for e in ledger.events)
    _assert_left_alive_guidance(out, ledger, shared)


def _same_file_key(name: str, key: str) -> bool:
    return drain._read_key(service.env_path_for(name)) == key


def test_an_unrecorded_key_whose_prefix_is_ambiguous_is_never_revoked(world, capsys):
    ledger = world["ledger"]
    old = _key(world)
    twin = old[:PREFIX_LEN] + secrets.token_hex(14)
    ledger.add(twin)
    _install(world, "d7", old, marker="d7", record_id=False)
    code, out, err = _cli(capsys, "rekey", "d7")
    assert code == drain.EXIT_MANUAL_REVOKE, err
    assert ledger.call(old) and ledger.call(twin)
    assert not any(e[0] == "revoke" for e in ledger.events)
    _assert_left_alive_guidance(out, ledger, old)


# ---- never revoke a guessed key (GRPH-1976 bounce, probes P3 / P3b / P3c) ------------------

def _assert_left_alive_guidance(out: str, ledger: FakeLedger, *alive: str) -> None:
    """The old key is said to be left alive, every live key is named by id and label (and by
    nothing else: not its prefix, not its value), and the person is sent to Settings. A key a
    drain's record names (another drain's, or the one `rekey` just minted) is never offered
    as a candidate: revoking it would stop that drain."""
    assert "old key: left ALIVE, not revoked" in out
    assert "Settings" in out
    manual = [line for line in out.splitlines() if line.startswith("manual_revoke_needed:")]
    assert len(manual) == 1, out
    minted = {p.read_text(encoding="utf-8").strip() for p in service.CONFIG_DIR.glob("*.key-id")}
    for row in ledger.keys.values():
        line = f"    {row['id']}  {row['name']}"
        if row["id"] in minted:
            assert line not in out, "the drain's new key was offered for revocation"
        elif not row["revoked"]:
            assert line in out
        assert row["prefix"] not in out
        assert row["plaintext"] not in out
    for key in alive:
        assert ledger.call(key)


@pytest.mark.parametrize("verb", ["remove", "rekey"])
def test_a_legacy_drain_holding_the_gban_setup_key_never_revokes_it(world, capsys, verb):
    """P3 / P3c: an old drain installed from $GBFLEET_API_KEY holds the person's own `gban
    setup` key. No other drain holds it, so sharers() is empty — and it is still never
    revoked, because nothing recorded which key it is."""
    ledger = world["ledger"]
    setup = "gb_test_" + secrets.token_hex(16)
    setup_id = ledger.add(setup, "gban setup")
    repo = _install(world, "old", setup, marker="old", record_id=False)

    code, out, err = _cli(capsys, verb, "old")
    assert code == drain.EXIT_MANUAL_REVOKE, err
    assert not any(e[0] == "revoke" for e in ledger.events)
    assert not ledger.keys[setup_id]["revoked"]
    _assert_left_alive_guidance(out, ledger, setup)
    if verb == "remove":
        assert not service.unit_path_for("old", world["kind"]).exists()
        assert not service.env_path_for("old").exists()
        assert not repo.exists(), "the clean checkout it owns still goes"
    else:
        new = drain._read_key(service.env_path_for("old"))
        assert new and new != setup and ledger.call(new)
        assert world["fake"].restarted == ["old"]
        assert ("call", ledger.id_of(new)) in ledger.events


@pytest.mark.parametrize("verb", ["remove", "rekey"])
def test_an_unrelated_live_key_sharing_the_prefix_is_never_revoked(world, capsys, verb):
    """P3b: the env key is not one of the person's live keys (revoked already, or not theirs),
    and an unrelated live key — box sync — shares its display prefix. Box sync stays alive."""
    ledger = world["ledger"]
    stranger = "gb_test_" + secrets.token_hex(16)  # never added: not in the person's list
    box = stranger[:PREFIX_LEN] + secrets.token_hex(14)
    box_id = ledger.add(box, "box sync")
    _install(world, "stray", stranger, marker="stray", record_id=False)

    code, out, err = _cli(capsys, verb, "stray")
    assert code == drain.EXIT_MANUAL_REVOKE, err
    assert not ledger.keys[box_id]["revoked"]
    assert not any(e[0] == "revoke" for e in ledger.events)
    _assert_left_alive_guidance(out, ledger, box)


def test_a_recorded_id_is_still_revoked_without_guidance(world, capsys):
    """The record is the one path to a revoke, and it still works (control for the above)."""
    ledger = world["ledger"]
    key = _key(world)
    _install(world, "rec", key, marker="rec")
    code, out, err = _cli(capsys, "remove", "rec")
    assert code == 0, err
    assert f"key: revoked ({ledger.id_of(key)})" in out
    assert "left ALIVE" not in out


# ---- keep any checkout holding work (GRPH-1976 bounce, probes P1 / P1b / P2 / P2b / P2c) --

def _remove_keeps(world, capsys, name: str, repo: Path, reason: str) -> str:
    code, out, err = _cli(capsys, "remove", name)
    assert code == 0, err
    assert repo.is_dir() and (repo / ".git").exists(), f"{repo} was deleted:\n{out}"
    assert f"checkout: kept {repo}" in out
    assert reason in out, out
    return out


def test_remove_keeps_a_checkout_with_a_detached_linked_worktree_holding_a_commit(world, capsys):
    """P1: the only copy of a commit is a linked worktree's detached HEAD."""
    key = _key(world)
    repo = _install(world, "w1", key, marker="w1")
    tree = world["tmp"] / "w1-linked"
    _git(repo, "worktree", "add", "-q", "--detach", str(tree))
    (tree / "only-here.txt").write_text("work\n", encoding="utf-8")
    _git(tree, "add", "only-here.txt")
    _git(tree, "commit", "-qm", "unbranched")
    sha = _git(tree, "rev-parse", "HEAD")
    _remove_keeps(world, capsys, "w1", repo, f"linked worktree(s): {tree.resolve()}")
    assert _git(repo, "cat-file", "-t", sha) == "commit"


def test_remove_keeps_a_checkout_with_a_clean_linked_worktree(world, capsys):
    """P1b: a clean linked worktree on a pushed commit still runs on this .git."""
    key = _key(world)
    repo = _install(world, "w2", key, marker="w2")
    tree = world["tmp"] / "w2-linked"
    _git(repo, "worktree", "add", "-q", "-b", "side", str(tree), "origin/main")
    _remove_keeps(world, capsys, "w2", repo, "linked worktree(s)")
    assert (tree / "README.md").exists()


def test_remove_keeps_untracked_work_that_show_untracked_files_no_hides(world, capsys):
    """P2: status.showUntrackedFiles=no must not hide an untracked file."""
    key = _key(world)
    repo = _install(world, "u1", key, marker="u1")
    _git(repo, "config", "status.showUntrackedFiles", "no")
    (repo / "notes").mkdir()
    (repo / "notes" / "draft.md").write_text("mine\n", encoding="utf-8")
    assert _git(repo, "status", "--porcelain") == "", "the setting hides it, as in the probe"
    _remove_keeps(world, capsys, "u1", repo, "untracked file(s): notes/draft.md")


def test_remove_keeps_and_names_ignored_files_that_are_not_caches(world, capsys):
    """P2b: an ignored scratch dir (.git/info/exclude) is work; it is named. A cache beside it
    is not named."""
    key = _key(world)
    repo = _install(world, "i1", key, marker="i1")
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text("scratch/\n.venv/\n", encoding="utf-8")
    (repo / "scratch").mkdir()
    (repo / "scratch" / "plan.txt").write_text("mine\n", encoding="utf-8")
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    out = _remove_keeps(world, capsys, "i1", repo,
                        "ignored file(s) that are not caches: scratch/plan.txt")
    assert ".venv" not in out


def test_remove_keeps_a_commit_only_an_unpushed_tag_reaches(world, capsys):
    """P2c: the commit is on no branch and no remote; only a tag holds it."""
    key = _key(world)
    repo = _install(world, "t1", key, marker="t1")
    _git(repo, "checkout", "-q", "--detach")
    _unpushed(repo)
    _git(repo, "tag", "v-mine")
    _git(repo, "checkout", "-q", "main")
    _remove_keeps(world, capsys, "t1", repo,
                  "its tag v-mine points at a commit that is on no remote")


def test_remove_keeps_a_commit_only_a_non_branch_ref_reaches(world, capsys):
    """A commit only `git notes` reaches: no branch, no tag, no HEAD. `--all` sees it."""
    key = _key(world)
    repo = _install(world, "n1", key, marker="n1")
    _git(repo, "notes", "add", "-m", "why this was done")
    _remove_keeps(world, capsys, "n1", repo, "unpushed commit(s)")


def test_remove_deletes_a_clean_pushed_checkout_carrying_only_caches(world, capsys):
    """Control: fully pushed, nothing untracked, and the only ignored files are the fixed
    regenerable set (.venv/, node_modules/, __pycache__/, the tool caches, *.pyc)."""
    key = _key(world)
    repo = _install(world, "c1", key, marker="c1")
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text(
        ".venv/\nnode_modules/\n__pycache__/\n.pytest_cache/\n.mypy_cache/\n.ruff_cache/\n"
        "*.pyc\n", encoding="utf-8")
    for rel in (".venv/lib/site.py", "node_modules/x/index.js", "pkg/__pycache__/m.cpython.pyc",
                ".pytest_cache/v/x", ".mypy_cache/x.json", ".ruff_cache/x", "stray.pyc"):
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("cache\n", encoding="utf-8")
    code, out, err = _cli(capsys, "remove", "c1")
    assert code == 0, err
    assert not repo.exists(), out
    assert f"checkout: deleted {repo}" in out


# ---- the recorded id must still name the env file's key (reviewer's SD gap) -----------------

@pytest.mark.parametrize("verb", ["remove", "rekey"])
def test_a_recorded_id_that_names_a_different_key_than_the_env_file_is_never_revoked(
        world, capsys, verb):
    """`<name>.key-id` names key A, but the env file holds key B (the record is stale, or was
    copied). A is someone else's live key: it is not revoked, and neither is B by a guess."""
    ledger = world["ledger"]
    a = _key(world)
    a_id = ledger.id_of(a)
    b = _key(world)
    _install(world, "mm", b, marker="mm", record_id=False)
    drain._record_key_id("mm", a_id)

    code, out, err = _cli(capsys, verb, "mm")
    assert code == drain.EXIT_MANUAL_REVOKE, err
    assert not ledger.keys[a_id]["revoked"], "the recorded id was revoked though it is not " \
                                              "the env file's key"
    assert not any(e[0] == "revoke" for e in ledger.events)
    assert f"the recorded key {a_id} is not the key in mm's env file" in out
    assert ledger.call(a) and ledger.call(b)


# ---- exit 3 and the machine-readable line when a manual revoke is needed -------------------

@pytest.mark.parametrize("verb", ["remove", "rekey"])
def test_an_unrecorded_key_left_alive_exits_3_with_a_manual_revoke_line(world, capsys, verb):
    ledger = world["ledger"]
    setup = "gb_test_" + secrets.token_hex(16)
    setup_id = ledger.add(setup, "gban setup")
    other = _key(world)
    other_id = ledger.id_of(other)
    _install(world, "legacy", setup, marker="legacy", record_id=False)

    code, out, err = _cli(capsys, verb, "legacy")
    assert code == 3 == drain.EXIT_MANUAL_REVOKE, (out, err)
    lines = [line for line in out.splitlines() if line.startswith("manual_revoke_needed:")]
    assert lines == [f"manual_revoke_needed: {setup_id},{other_id}"], out
    assert "Settings" in out, "the line comes alongside the guidance, not instead of it"
    assert not any(e[0] == "revoke" for e in ledger.events)


@pytest.mark.parametrize("verb", ["remove", "rekey"])
def test_a_recorded_revoke_exits_0_with_no_manual_revoke_line(world, capsys, verb):
    ledger = world["ledger"]
    key = _key(world)
    _install(world, "ok", key, marker="ok")
    code, out, err = _cli(capsys, verb, "ok")
    assert code == 0, (out, err)
    assert "manual_revoke_needed" not in out + err
    assert ("revoke", ledger.id_of(key)) in ledger.events


def test_a_shared_key_left_alive_for_its_other_holders_exits_0(world, capsys):
    """Left alive because another drain still runs on it is not a manual revoke: exit 0."""
    shared = _key(world)
    _install(world, "a", shared, marker="a", record_id=False)
    _install(world, "b", shared, marker="b", record_id=False)
    for verb in ("rekey", "remove"):
        code, out, err = _cli(capsys, verb, "a")
        assert code == 0, (verb, out, err)
        assert "manual_revoke_needed" not in out + err


# ---- submodules, hidden edits, reflog-only commits, symlinks (reviewer N1, N1b, N4, N6) ----

def _sub_origin(world) -> Path:
    """A bare repo to use as a submodule's remote, with one pushed commit."""
    origin = world["tmp"] / "sub-origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = world["tmp"] / "sub-seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "t@example.invalid")
    _git(seed, "config", "user.name", "T")
    (seed / "lib.txt").write_text("lib\n", encoding="utf-8")
    _git(seed, "add", "lib.txt")
    _git(seed, "commit", "-qm", "lib")
    _git(seed, "push", "-q", str(origin), "main")
    return origin


def _with_submodule(world, repo: Path) -> Path:
    """Add `sub` as a submodule, commit and push the superproject. Returns the submodule."""
    origin = _sub_origin(world)
    _git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(origin), "sub")
    _git(repo, "commit", "-qm", "add sub")
    _git(repo, "push", "-q", "origin", "main")
    sub = repo / "sub"
    _git(sub, "config", "user.email", "t@example.invalid")
    _git(sub, "config", "user.name", "T")
    return sub


def _sub_commit(sub: Path, text: str) -> str:
    (sub / "mine.txt").write_text(text, encoding="utf-8")
    _git(sub, "add", "mine.txt")
    _git(sub, "commit", "-qm", "only in the submodule")
    return _git(sub, "rev-parse", "HEAD")


def test_remove_keeps_a_checkout_whose_submodule_holds_a_commit_on_a_side_branch(world, capsys):
    """N1: the submodule has a commit on a submodule-only branch and is checked back out to
    its recorded commit. The superproject is clean and fully pushed."""
    key = _key(world)
    repo = _install(world, "s1", key, marker="s1")
    sub = _with_submodule(world, repo)
    recorded = _git(sub, "rev-parse", "HEAD")
    _git(sub, "checkout", "-q", "-b", "side")
    sha = _sub_commit(sub, "side work\n")
    _git(sub, "checkout", "-q", recorded)
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == ""
    assert _git(repo, "rev-list", "--all", "--not", "--remotes") == ""

    _remove_keeps(world, capsys, "s1", repo, "submodule(s): sub")
    assert _git(sub, "cat-file", "-t", sha) == "commit"


def test_remove_keeps_a_checkout_whose_pushed_gitlink_names_an_unpushed_submodule_commit(
        world, capsys):
    """N1b: the gitlink bump is pushed on the superproject; the submodule commit it names
    was never pushed. This checkout holds the only copy."""
    key = _key(world)
    repo = _install(world, "s2", key, marker="s2")
    sub = _with_submodule(world, repo)
    sha = _sub_commit(sub, "bumped\n")
    _git(repo, "add", "sub")
    _git(repo, "commit", "-qm", "bump sub")
    _git(repo, "push", "-q", "origin", "main")
    assert _git(repo, "rev-list", "--all", "--not", "--remotes") == ""

    _remove_keeps(world, capsys, "s2", repo, "submodule(s): sub")
    assert _git(sub, "cat-file", "-t", sha) == "commit"


def test_remove_keeps_a_checkout_with_only_a_leftover_git_modules_dir(world, capsys):
    """Anything under .git/modules/ is a submodule's store, even with no .gitmodules."""
    key = _key(world)
    repo = _install(world, "s3", key, marker="s3")
    (repo / ".git" / "modules" / "old").mkdir(parents=True)
    (repo / ".git" / "modules" / "old" / "HEAD").write_text("ref: refs/heads/main\n",
                                                            encoding="utf-8")
    _remove_keeps(world, capsys, "s3", repo, "submodule(s): .git/modules/old")


@pytest.mark.parametrize("flag,tag", [("--assume-unchanged", "h"), ("--skip-worktree", "S")])
def test_remove_keeps_a_tracked_edit_hidden_from_status(world, capsys, flag, tag):
    """N6: `update-index --assume-unchanged` / `--skip-worktree` hide an edit from status."""
    key = _key(world)
    repo = _install(world, "h1", key, marker="h1")
    _git(repo, "update-index", flag, "README.md")
    (repo / "README.md").write_text("edited, hidden\n", encoding="utf-8")
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == ""
    assert _git(repo, "ls-files", "-v").startswith(f"{tag} ")
    _remove_keeps(world, capsys, "h1", repo,
                  "--assume-unchanged or --skip-worktree, so their edits are hidden: README.md")
    assert (repo / "README.md").read_text(encoding="utf-8") == "edited, hidden\n"


def test_remove_keeps_a_commit_only_a_reflog_reaches(world, capsys):
    """N4: commit, then `reset --hard` back to the pushed commit. No ref reaches it; the
    reflog does."""
    key = _key(world)
    repo = _install(world, "r1", key, marker="r1")
    _unpushed(repo)
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "reset", "-q", "--hard", "origin/main")
    assert _git(repo, "rev-list", "--all", "--not", "--remotes") == ""
    _remove_keeps(world, capsys, "r1", repo, "1 commit(s) only a reflog reaches")
    assert _git(repo, "cat-file", "-t", sha) == "commit"


def test_remove_keeps_a_checkout_reached_through_a_symlinked_path(world, capsys):
    """The unit's --repo is a symlink to the real checkout: kept with a reason, no traceback,
    and neither the link nor its target is touched."""
    key = _key(world)
    repo = _install(world, "l1", key, marker="l1")
    real = world["tmp"] / "real-l1"
    repo.rename(real)
    repo.symlink_to(real, target_is_directory=True)

    code, out, err = _cli(capsys, "remove", "l1")
    assert code == 0, err
    assert repo.is_symlink() and (real / ".git" / "gbfleet-drain").exists()
    assert f"checkout: kept {repo}: its path resolves through a symlink" in out
    assert "Traceback" not in out + err


def test_remove_keeps_a_checkout_whose_marker_is_a_symlink(world, capsys):
    """The marker resolves through a symlink to a file elsewhere that names the drain."""
    key = _key(world)
    repo = _install(world, "l2", key, marker=None)
    elsewhere = world["tmp"] / "elsewhere-marker"
    elsewhere.write_text("l2\n", encoding="utf-8")
    (repo / ".git" / "gbfleet-drain").symlink_to(elsewhere)

    out = _remove_keeps(world, capsys, "l2", repo, "marker is a symlink")
    assert elsewhere.read_text(encoding="utf-8") == "l2\n"
    assert "Traceback" not in out


# ---- clause 5: no key value on stdout or stderr --------------------------------------------

def test_neither_command_prints_a_key_value(world, capsys):
    ledger = world["ledger"]
    shared = _key(world)
    _install(world, "a", shared, marker="a", record_id=False)
    _install(world, "b", shared, marker="b", record_id=False)
    solo = _key(world)
    _install(world, "s", solo, marker="s")

    seen = ""
    # rekey b: a no longer shares the copied key and nothing recorded its id, so it is left
    # alive for the person to revoke (exit 3). Every other run is a plain success.
    for argv, want in ((("rekey", "a"), 0), (("rekey", "b"), drain.EXIT_MANUAL_REVOKE), (("rekey", "s"), 0),
                       (("remove", "a"), 0), (("remove", "s"), 0)):
        code, out, err = _cli(capsys, *argv)
        assert code == want, (argv, out, err)
        seen += out + err
    assert len(ledger.plaintexts()) == 5
    for key in ledger.plaintexts():
        assert key not in seen
        assert key[len("gb_test_"):] not in seen
    assert "revoked" in seen and "minted" in seen, "the commands said nothing at all"


# ---- the session client ---------------------------------------------------------------------

def test_the_session_lists_and_revokes_on_its_own_paths_only(tmp_path, monkeypatch):
    import httpx

    monkeypatch.setenv("GRAPHBAN_HOME", str(tmp_path))
    (tmp_path / "session.json").write_text('{"refresh_token": "rt_fake"}', encoding="utf-8")
    seen: list[tuple[str, str]] = []

    def server(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/api/auth/refresh":
            return httpx.Response(200, json={"access_token": "at_fake"})
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": "key_1", "prefix": "gb_sk_ab12",
                                              "last_used": None, "revoked": False}])
        return httpx.Response(204)

    session = client_mod.LedgerSession.open("http://ledger.invalid/",
                                            transport=httpx.MockTransport(server))
    assert session.list_keys()[0]["id"] == "key_1"
    session.revoke_key("key_1")
    assert seen == [("POST", "/api/auth/refresh"), ("GET", "/api/api-keys"),
                    ("DELETE", "/api/api-keys/key_1")]
