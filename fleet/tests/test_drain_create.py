"""`gbfleet drain create` (GRPH-1974).

Nothing here touches the real machine. `conftest.nowhere_near_your_home` already points
`service.CONFIG_DIR`, `unit_dir` and `env_path_for` at a per-test temp tree; on top of that
every test here:

- sets `$HOME` and `$GRAPHBAN_HOME` to temp directories;
- replaces `service.host()` (the seam) with launchd or systemd, and `service._run` with a fake
  supervisor that never calls `launchctl` / `systemctl`. Its "load" performs the unit's FIRST
  RUN for real — the plist's `ProgramArguments` or the unit's `ExecStart` with its
  `EnvironmentFile` — against a stub `gbfleet` that logs its argv, so the log greps below read
  what a started service would have written;
- replaces `drain.open_ledger` with a fake ledger that mints a fixed fake key, or leaves the
  real one in place with no session on disk (it refuses before any network).
"""
from __future__ import annotations

import io
import os
import plistlib
import secrets
import shlex
import stat
import subprocess
from pathlib import Path

import httpx
import pytest

from gbfleet import client as client_mod
from gbfleet import drain
from gbfleet import service
from gbfleet.cli import main

#: The fake first run executes a `/bin/sh` stub and the units are launchd / systemd, so the
#: end-to-end tests are POSIX-only; the session and atomic-write tests run everywhere.
posix_only = pytest.mark.skipif(os.name == "nt", reason="launchd / systemd units and a /bin/sh stub")

PRD = "GRPH-P96"
NAME = "grph-p96"
LIKE = "y"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


# ---- the fake supervisor ------------------------------------------------------------------

class FakeSupervisor:
    """Stands in for launchctl / systemctl. A load runs the unit's command once."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.calls: list[list[str]] = []
        self.exit: dict[str, int] = {}

    def __call__(self, cmd: list[str], timeout: float = 20.0) -> tuple[int, str]:
        self.calls.append(list(cmd))
        if cmd[:2] == ["launchctl", "bootstrap"]:
            self._first_run_launchd(Path(cmd[3]))
            return 0, ""
        if cmd[:2] == ["launchctl", "list"]:
            return 0, "\n".join(f"-\t{code}\t{label}" for label, code in self.exit.items())
        if cmd[:4] == ["systemctl", "--user", "enable", "--now"]:
            self._first_run_systemd(cmd[4])
            return 0, ""
        if cmd[:3] == ["systemctl", "--user", "is-active"]:
            return 3, "inactive"
        if cmd[:3] == ["systemctl", "--user", "show"]:
            if "--property=ExecMainStatus" in cmd:
                return 0, f"ExecMainStatus={self.exit.get(cmd[3], 1)}"
            return 0, "LoadState=loaded\nUnitFileState=enabled"
        return 0, ""

    @property
    def loads(self) -> list[list[str]]:
        return [c for c in self.calls
                if c[:2] == ["launchctl", "bootstrap"] or c[:4] == ["systemctl", "--user",
                                                                    "enable", "--now"]]

    def _first_run_launchd(self, plist: Path) -> None:
        job = plistlib.loads(plist.read_bytes())
        env = dict(job.get("EnvironmentVariables") or {})
        env["HOME"] = os.environ["HOME"]
        with open(job["StandardOutPath"], "ab") as so, open(job["StandardErrorPath"], "ab") as se:
            done = subprocess.run(job["ProgramArguments"], cwd=job["WorkingDirectory"],
                                  env=env, stdout=so, stderr=se, timeout=60)
        self.exit[job["Label"]] = done.returncode

    def _first_run_systemd(self, unit_name: str) -> None:
        name = unit_name[len(service.UNIT_PREFIX):-len(".service")]
        text = service.unit_path_for(name, "systemd").read_text(encoding="utf-8")
        env = {"HOME": os.environ["HOME"]}
        argv: list[str] = []
        cwd = out = errp = ""
        for line in text.splitlines():
            key, _, value = line.partition("=")
            if key == "Environment":
                k, _, v = value.partition("=")
                env[k] = v
            elif key == "EnvironmentFile":
                for row in Path(value).read_text(encoding="utf-8").splitlines():
                    k, _, v = row.partition("=")
                    if k:
                        env[k] = v
            elif key == "ExecStart":
                argv = shlex.split(value)
            elif key == "WorkingDirectory":
                cwd = value
            elif key == "StandardOutput":
                out = value.removeprefix("append:")
            elif key == "StandardError":
                errp = value.removeprefix("append:")
        with open(out, "ab") as so, open(errp, "ab") as se:
            done = subprocess.run(argv, cwd=cwd, env=env, stdout=so, stderr=se, timeout=60)
        self.exit[unit_name] = done.returncode


#: What the fake ledger's `prd_coverage` answers by default: an approved, shaped PRD with tasks.
GOOD_COVERAGE = {"prd_id": PRD, "status": "approved", "shaped": True, "total_items": 4}


class FakeLedger:
    def __init__(self, key: str) -> None:
        self.key = key
        self.opened: list[str] = []
        self.minted: list[tuple[str, str, str]] = []
        self.coverage: dict = dict(GOOD_COVERAGE)
        self.read: list[str] = []
        #: When set, `mint_prd_key` raises it — a failure after the clone step.
        self.mint_error: Exception | None = None

    def prd_coverage(self, prd: str) -> dict:
        self.read.append(prd)
        return dict(self.coverage)

    def open(self, server: str) -> "FakeLedger":
        self.opened.append(server)
        return self

    def mint_prd_key(self, prd: str, label: str, *, project: str = "") -> dict:
        self.minted.append((prd, label, project))
        if self.mint_error is not None:
            raise self.mint_error
        return {"id": "key_fake01", "plaintext": self.key, "project_id": "proj"}


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
    fake = FakeSupervisor(kind)
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
    work = tmp_path / "src" / "work"
    work.parent.mkdir()
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    monkeypatch.chdir(work)

    # Drain Y, installed the way `service install` would have left it.
    y_key = "gb_test_" + secrets.token_hex(16)
    y_plan = service.make_plan(
        ["--repo", str(work), "--server", "http://ledger.invalid:9", "--project", "proj",
         "--adapter", "claude", "--max-workers", "3", "--tier", "cheap=gbagent:small",
         "--allow", "psql", "--prd", "GRPH-P95"],
        name=LIKE, kind=kind, binary=str(stub))
    y_plan.unit_path.parent.mkdir(parents=True, exist_ok=True)
    y_plan.unit_path.write_bytes(service.render(y_plan))
    service.write_env(y_plan, y_key)

    key = "gb_test_" + secrets.token_hex(16)
    ledger = FakeLedger(key)
    monkeypatch.setattr(drain, "open_ledger", ledger.open)
    return {"kind": kind, "work": work.resolve(), "origin": origin, "fake": fake,
            "ledger": ledger, "key": key, "y_key": y_key, "y_plan": y_plan, "tmp": tmp_path}


def _create(capsys, *extra: str) -> tuple[int, str, str]:
    code = main(["drain", "create", "--prd", PRD, *extra])
    got = capsys.readouterr()
    return code, got.out, got.err


def _runs(out: str) -> list[str]:
    line = next(ln for ln in out.splitlines() if ln.startswith("runs  "))
    argv = shlex.split(line[len("runs  "):])
    assert argv[1] == "until"
    return argv[2:]


def _clone(world) -> Path:
    return world["work"].with_name(f"work-{PRD.lower()}")


def _logs() -> list[Path]:
    return [service.CONFIG_DIR / "logs" / f"{NAME}.log",
            service.CONFIG_DIR / "logs" / f"{NAME}.err.log"]


# ---- acceptance ---------------------------------------------------------------------------

@posix_only
def test_like_produces_checkout_owner_only_new_key_and_the_dry_runs_arguments(world, capsys):
    """Clause 1. Checkout, a 600 env file with a key that is not Y's, and an installed service
    whose `until` arguments equal the dry run's — read back from the unit on disk."""
    code, dry_out, _ = _create(capsys, "--like", LIKE, "--dry-run")
    assert code == 0
    dry_args = _runs(dry_out)

    code, out, err = _create(capsys, "--like", LIKE)
    assert code == 0, err
    clone = _clone(world)
    assert (clone / ".git").is_dir() and (clone / "README.md").is_file()
    assert _git(clone, "remote", "get-url", "origin") == str(world["origin"])

    env = service.env_path_for(NAME)
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert env.read_text(encoding="utf-8") == f"GBFLEET_API_KEY={world['key']}\n"
    assert world["key"] != world["y_key"]
    assert world["y_plan"].env_path.read_text(encoding="utf-8") == \
        f"GBFLEET_API_KEY={world['y_key']}\n", "Y's key file must be untouched"

    assert service.unit_path_for(NAME, world["kind"]).is_file()
    installed = service.until_args_of(NAME, world["kind"])
    assert installed == dry_args == _runs(out)
    assert installed[:4] == ["--repo", str(clone), "--prd", PRD]
    assert len(world["fake"].loads) == 1
    assert world["ledger"].minted == [(PRD, f"{NAME}@{__import__('socket').gethostname()}",
                                       "proj")]
    assert f"key: minted (key_fake01), written to {env}" in out


@posix_only
def test_the_key_appears_in_no_output_no_unit_and_no_first_run_log(world, capsys):
    """Clause 2. Grep the fake key everywhere it could leak. The first-run log must show the
    key REACHED the process (sourced from the env file) without carrying its value."""
    code, out, err = _create(capsys, "--like", LIKE)
    assert code == 0, err
    key = world["key"]
    assert key not in out and key not in err
    unit = service.unit_path_for(NAME, world["kind"]).read_bytes()
    assert key.encode() not in unit
    logs = [p.read_text(encoding="utf-8") for p in _logs()]
    assert f"key: present ({len(key)} chars)" in logs[0], "the first run never happened"
    for text in logs:
        assert key not in text
    assert "--prd GRPH-P96" in logs[0]


@posix_only
def test_dry_run_writes_no_env_file_mints_no_key_and_installs_nothing(world, capsys):
    """Clause 3."""
    code, out, err = _create(capsys, "--like", LIKE, "--dry-run")
    assert code == 0, err
    assert "would mint a key limited to GRPH-P96" in out
    assert not service.env_path_for(NAME).exists()
    assert not service.unit_path_for(NAME, world["kind"]).exists()
    # GRPH-1984: the dry run reads the PRD through the session, and mints nothing.
    assert world["ledger"].read == [PRD] and world["ledger"].minted == []
    assert world["fake"].calls == []
    assert not _clone(world).exists()


@posix_only
def test_no_gban_session_prints_login_and_writes_nothing(world, capsys, monkeypatch):
    """Clause 4, through the REAL session reader with an empty $GRAPHBAN_HOME."""
    monkeypatch.setattr(drain, "open_ledger", client_mod.LedgerSession.open)
    code, out, err = _create(capsys, "--like", LIKE)
    assert code == drain.EXIT_NO_SESSION
    assert "gban --server http://ledger.invalid:9 login" in err
    assert not _clone(world).exists()
    assert not service.env_path_for(NAME).exists()
    assert not service.unit_path_for(NAME, world["kind"]).exists()
    assert world["fake"].calls == []


@posix_only
def test_a_session_the_server_will_not_renew_also_writes_nothing(world, capsys, monkeypatch):
    """Clause 4: stored but refused (401) is the same state as none."""
    gb = Path(os.environ["GRAPHBAN_HOME"])
    gb.mkdir()
    (gb / "session.json").write_text('{"refresh_token": "rt_fake"}', encoding="utf-8")
    seen: list[str] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(401, json={"detail": "expired"})

    monkeypatch.setattr(drain, "open_ledger", lambda server: client_mod.LedgerSession.open(
        server, transport=httpx.MockTransport(refuse)))
    code, _out, err = _create(capsys, "--like", LIKE)
    assert code == drain.EXIT_NO_SESSION
    assert "login" in err
    assert seen == ["/api/auth/refresh"], "nothing but the refresh may be attempted"
    assert not _clone(world).exists()
    assert not service.env_path_for(NAME).exists()
    assert not service.unit_path_for(NAME, world["kind"]).exists()


@posix_only
def test_a_checkout_create_made_carries_the_marker(world, capsys):
    """Clause 5, first half."""
    code, _out, err = _create(capsys, "--like", LIKE)
    assert code == 0, err
    marker = _clone(world) / ".git" / drain.MARKER
    assert marker.read_text(encoding="utf-8") == f"{NAME}\n"


@posix_only
def test_a_repo_the_person_supplied_gets_no_marker(world, capsys):
    """Clause 5, second half."""
    mine = world["tmp"] / "mine"
    subprocess.run(["git", "clone", "-q", str(world["origin"]), str(mine)], check=True)
    code, _out, err = _create(capsys, "--like", LIKE, "--repo", str(mine))
    assert code == 0, err
    assert not (mine / ".git" / drain.MARKER).exists()
    assert service.until_args_of(NAME, world["kind"])[:2] == ["--repo", str(mine.resolve())]
    assert not _clone(world).exists()


@pytest.mark.parametrize("spoil", ["dirty", "other-remote", "not-a-checkout"])
def test_an_existing_path_that_is_not_a_clean_checkout_of_the_remote_is_refused(
        world, capsys, spoil):
    path = world["tmp"] / "mine"
    if spoil == "not-a-checkout":
        path.mkdir()
    else:
        subprocess.run(["git", "clone", "-q", str(world["origin"]), str(path)], check=True)
        if spoil == "dirty":
            (path / "README.md").write_text("changed\n", encoding="utf-8")
        else:
            _git(path, "remote", "set-url", "origin", "https://example.invalid/other.git")
    code, _out, err = _create(capsys, "--like", LIKE, "--repo", str(path))
    assert code == drain.EXIT_REFUSED
    assert world["ledger"].opened == [] and world["fake"].calls == []
    assert not service.env_path_for(NAME).exists()


@posix_only
def test_like_flags_are_copied_and_explicit_flags_override_them(world, capsys):
    code, out, err = _create(capsys, "--like", LIKE, "--dry-run", "--adapter", "codex",
                             "--max-workers", "5", "--tier", "frontier=claude:opus", "--merge")
    assert code == 0, err
    args = _runs(out)
    assert service.value_of(args, "--adapter") == "codex"
    assert service.value_of(args, "--max-workers") == "5"
    assert service.value_of(args, "--server") == "http://ledger.invalid:9"
    assert service.value_of(args, "--project") == "proj"
    assert service.value_of(args, "--prd") == PRD and "GRPH-P95" not in args
    assert [args[i + 1] for i, a in enumerate(args) if a == "--tier"] == ["frontier=claude:opus"]
    assert [args[i + 1] for i, a in enumerate(args) if a == "--allow"] == ["psql"]
    assert "--merge" in args and args.count("--repo") == 1


@posix_only
def test_an_existing_drain_name_is_never_overwritten(world, capsys):
    code, _out, err = _create(capsys, "--like", LIKE, "--name", LIKE)
    assert code == drain.EXIT_REFUSED and "already exists" in err
    assert world["y_plan"].env_path.read_text(encoding="utf-8").endswith(world["y_key"] + "\n")
    assert world["ledger"].opened == []


# ---- GRPH-1984: the dry run refuses what the real run refuses -----------------------------

@posix_only
@pytest.mark.parametrize("coverage,reason", [
    ({"status": "draft"}, "GRPH-P96 is draft, not approved"),
    ({"status": "review"}, "GRPH-P96 is review, not approved"),
    ({"shaped": False}, "GRPH-P96 is not shaped"),
    ({"total_items": 0}, "GRPH-P96 has no tasks"),
])
def test_dry_run_refuses_an_unready_prd_exactly_as_the_real_run_does(
        world, capsys, coverage, reason):
    world["ledger"].coverage.update(coverage)
    dry_code, dry_out, dry_err = _create(capsys, "--like", LIKE, "--dry-run")
    code, out, err = _create(capsys, "--like", LIKE)
    assert dry_code == code == drain.EXIT_REFUSED
    assert reason in dry_err
    assert dry_err == err, "the dry run must give the real run's reason, word for word"
    assert dry_out == out == ""
    assert world["ledger"].minted == []
    assert world["fake"].calls == []
    assert not _clone(world).exists()
    assert not service.env_path_for(NAME).exists()
    assert not service.unit_path_for(NAME, world["kind"]).exists()


# ---- GRPH-1984: a failed create removes the clone it made ---------------------------------

def _fail_install(monkeypatch) -> None:
    def refuse(plan, key):
        raise service.Refused("install refused for the test")
    monkeypatch.setattr(service, "install", refuse)


@posix_only
@pytest.mark.parametrize("failure", ["mint", "install"])
def test_a_create_that_fails_after_cloning_removes_its_clone_and_a_rerun_succeeds(
        world, capsys, monkeypatch, failure):
    clone = _clone(world)
    real_install = service.install
    if failure == "mint":
        world["ledger"].mint_error = client_mod.ToolFailed(
            "/api/api-keys", code="500", message="boom")
    else:
        _fail_install(monkeypatch)
    code, _out, err = _create(capsys, "--like", LIKE)
    assert code == drain.EXIT_REFUSED
    assert not clone.exists(), "the checkout this run cloned must be removed"
    assert f"removed {clone}" in err
    assert not service.env_path_for(NAME).exists()

    world["ledger"].mint_error = None
    monkeypatch.setattr(service, "install", real_install)
    code, _out, err = _create(capsys, "--like", LIKE)
    assert code == 0, err
    assert (clone / ".git" / drain.MARKER).read_text(encoding="utf-8") == f"{NAME}\n"
    assert service.unit_path_for(NAME, world["kind"]).is_file()


@posix_only
def test_a_failed_create_never_removes_a_directory_that_was_already_there(
        world, capsys, monkeypatch):
    mine = world["tmp"] / "mine"
    subprocess.run(["git", "clone", "-q", str(world["origin"]), str(mine)], check=True)
    _fail_install(monkeypatch)
    code, _out, err = _create(capsys, "--like", LIKE, "--repo", str(mine))
    assert code == drain.EXIT_REFUSED
    assert (mine / "README.md").is_file()
    assert "removed" not in err


@posix_only
def test_a_failed_create_keeps_a_clone_that_does_not_carry_this_drains_marker(
        world, capsys, monkeypatch):
    """The marker is the proof of ownership: without it (or naming another drain) it stays."""
    clone = _clone(world)

    def other_marker(checkout, name):
        (checkout / ".git" / drain.MARKER).write_text("someone-else\n", encoding="utf-8")
    monkeypatch.setattr(drain, "_write_marker", other_marker)
    _fail_install(monkeypatch)
    code, _out, err = _create(capsys, "--like", LIKE)
    assert code == drain.EXIT_REFUSED
    assert clone.is_dir()
    assert f"kept {clone}" in err and "removed" not in err


# ---- unit rendering through the host() seam (clause 6) ------------------------------------

@posix_only
def test_both_unit_kinds_render_from_the_host_seam_and_keep_the_key_out(world, capsys):
    """launchd → plist that sources the env file; systemd → `EnvironmentFile=`."""
    code, _out, err = _create(capsys, "--like", LIKE)
    assert code == 0, err
    unit = service.unit_path_for(NAME, world["kind"])
    env = service.env_path_for(NAME)
    if world["kind"] == "launchd":
        assert unit.name == f"{service.LABEL_PREFIX}.{NAME}.plist"
        job = plistlib.loads(unit.read_bytes())
        assert job["ProgramArguments"][:2] == ["/bin/sh", "-c"]
        assert f". {shlex.quote(str(env))}" in job["ProgramArguments"][2]
        assert "GBFLEET_API_KEY" not in job["EnvironmentVariables"]
    else:
        assert unit.name == f"gbfleet-{NAME}.service"
        text = unit.read_text(encoding="utf-8")
        assert f"EnvironmentFile={env}" in text
        assert "GBFLEET_API_KEY" not in text
    assert service.secrets_in(unit.read_bytes()) == []


# ---- the session client -------------------------------------------------------------------

def test_the_session_mints_a_prd_limited_key_on_its_two_paths_only(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPHBAN_HOME", str(tmp_path))
    (tmp_path / "session.json").write_text('{"refresh_token": "rt_fake"}', encoding="utf-8")
    seen: list[tuple[str, dict, str]] = []

    def server(request: httpx.Request) -> httpx.Response:
        import json
        body = json.loads(request.content)
        seen.append((request.url.path, body, request.headers.get("authorization", "")))
        if request.url.path == "/api/auth/refresh":
            return httpx.Response(200, json={"access_token": "at_fake"})
        return httpx.Response(201, json={"id": "key_1", "plaintext": "gb_test_x"})

    session = client_mod.LedgerSession.open("http://ledger.invalid/",
                                            transport=httpx.MockTransport(server))
    got = session.mint_prd_key(PRD, "grph-p96@box", project="proj")
    assert got["id"] == "key_1"
    assert seen[0] == ("/api/auth/refresh", {"refresh_token": "rt_fake"}, "")
    path, body, auth = seen[1]
    assert path == "/api/api-keys" and auth == "Bearer at_fake"
    assert body["prd_id"] == PRD and body["name"] == "grph-p96@box"
    assert body["project_id"] == "proj" and body["expires_in_days"] is None
    assert client_mod.SESSION_PATHS == {"/api/auth/refresh", "/api/api-keys"}
    with pytest.raises(client_mod.NotPermitted):
        session._post("/api/items", {})


def test_the_session_reads_prd_coverage_by_get_and_nothing_else(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPHBAN_HOME", str(tmp_path))
    (tmp_path / "session.json").write_text('{"refresh_token": "rt_fake"}', encoding="utf-8")
    seen: list[tuple[str, str, str]] = []

    def server(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("authorization", "")))
        if request.url.path == "/api/auth/refresh":
            return httpx.Response(200, json={"access_token": "at_fake"})
        return httpx.Response(200, json=GOOD_COVERAGE)

    session = client_mod.LedgerSession.open("http://ledger.invalid/",
                                            transport=httpx.MockTransport(server))
    assert session.prd_coverage(PRD)["status"] == "approved"
    assert seen[1] == ("GET", f"/api/prds/{PRD}/coverage", "Bearer at_fake")
    for path in ("/api/items", "/api/prds/X", "/api/prds/X/coverage/more", "/api/prds//coverage"):
        with pytest.raises(client_mod.NotPermitted):
            session._get(path)
    with pytest.raises(client_mod.NotPermitted):
        session.prd_coverage("../items")
    assert len(seen) == 2


def test_no_session_file_refuses_before_any_network(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPHBAN_HOME", str(tmp_path))

    def boom(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("network was used")

    with pytest.raises(client_mod.NoSession):
        client_mod.LedgerSession.open("http://ledger.invalid",
                                      transport=httpx.MockTransport(boom))


# ---- the env file is written atomically ---------------------------------------------------

def test_write_env_replaces_atomically_and_leaves_the_old_key_on_failure(tmp_path, monkeypatch):
    plan = service.Plan(name="atom", kind="systemd", label="x", unit_path=tmp_path / "u",
                        env_path=tmp_path / "cfg" / "atom.env", log_path=tmp_path / "l",
                        err_path=tmp_path / "e", binary="/bin/true", until_args=[],
                        repo=tmp_path, common_dir=tmp_path, path_env="")
    service.write_env(plan, "gb_test_old_value")
    if os.name != "nt":
        assert stat.S_IMODE(plan.env_path.stat().st_mode) == 0o600

    def fail(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(service.os, "replace", fail)
    with pytest.raises(OSError):
        service.write_env(plan, "gb_test_new_value")
    assert plan.env_path.read_text(encoding="utf-8") == "GBFLEET_API_KEY=gb_test_old_value\n"
    assert sorted(p.name for p in plan.env_path.parent.iterdir()) == ["atom.env"]
