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

    # b next: c still shares it. Then c: nobody does, so the shared key is revoked — found by
    # its prefix, since no create ever recorded its id.
    assert _cli(capsys, "rekey", "b")[0] == 0
    assert ledger.call(shared)
    code, out, err = _cli(capsys, "rekey", "c")
    assert code == 0, err
    with pytest.raises(KeyRefused):
        ledger.call(shared)
    assert f"old key: revoked ({ledger.id_of(shared)})" in out


def _same_file_key(name: str, key: str) -> bool:
    return drain._read_key(service.env_path_for(name)) == key


def test_an_unrecorded_key_whose_prefix_is_ambiguous_is_never_revoked(world, capsys):
    ledger = world["ledger"]
    old = _key(world)
    twin = old[:PREFIX_LEN] + secrets.token_hex(14)
    ledger.add(twin)
    _install(world, "d7", old, marker="d7", record_id=False)
    code, _out, err = _cli(capsys, "rekey", "d7")
    assert code == 1 and "share its prefix" in err
    assert ledger.call(old) and ledger.call(twin)


# ---- clause 5: no key value on stdout or stderr --------------------------------------------

def test_neither_command_prints_a_key_value(world, capsys):
    ledger = world["ledger"]
    shared = _key(world)
    _install(world, "a", shared, marker="a", record_id=False)
    _install(world, "b", shared, marker="b", record_id=False)
    solo = _key(world)
    _install(world, "s", solo, marker="s")

    seen = ""
    for argv in (("rekey", "a"), ("rekey", "b"), ("rekey", "s"), ("remove", "a"),
                 ("remove", "s")):
        code, out, err = _cli(capsys, *argv)
        assert code == 0, err
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
