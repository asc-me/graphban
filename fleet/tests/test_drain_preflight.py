"""`gbfleet drain create` preflight (GRPH-1975): every refusal names its reason and writes nothing.

Built on GRPH-1974's world (`tests/test_drain_create.py`): a temp `$HOME` / `$GRAPHBAN_HOME`,
`service.host()` and `service._run` replaced by a fake supervisor, `drain.open_ledger` by a fake
ledger, stub adapter binaries on a temp `$PATH`. Nothing here touches the real machine.

Each refusal test asserts the same four absences through `_wrote_nothing`: no key minted (the
fake ledger recorded no mint), no checkout, no env file and no unit, and no supervisor call.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from gbfleet import client as client_mod
from gbfleet import drain
from gbfleet import service
from gbfleet import state as state_mod
from gbfleet.lock import hold
from tests.test_drain_create import (  # noqa: F401 — `kind` and `world` are fixtures
    LIKE,
    NAME,
    PRD,
    _clone,
    _create,
    kind,
    posix_only,
    world,
)


def _wrote_nothing(world) -> None:
    assert world["ledger"].minted == [], "a key was minted"
    assert not _clone(world).exists(), "a checkout was cut"
    assert not service.env_path_for(NAME).exists(), "an env file was written"
    assert not service.unit_path_for(NAME, world["kind"]).exists(), "a unit was written"
    assert world["fake"].calls == [], "the supervisor was asked to do something"


def _own_checkout(world) -> Path:
    mine = world["tmp"] / "mine"
    subprocess.run(["git", "clone", "-q", str(world["origin"]), str(mine)], check=True)
    return mine


# ---- the PRD (clause 1) --------------------------------------------------------------------

@posix_only
@pytest.mark.parametrize("status", ["draft", "review", "closed"])
def test_an_unapproved_prd_is_refused_and_nothing_is_written(world, capsys, status):
    world["ledger"].coverage = {"prd_id": PRD, "status": status, "shaped": True}
    code, _out, err = _create(capsys, "--like", LIKE)
    assert code == drain.EXIT_REFUSED
    assert f"{PRD} is {status}, not approved" in err
    assert world["ledger"].read == [PRD], "the refusal must come from the coverage read"
    _wrote_nothing(world)


@posix_only
def test_a_prd_coverage_reports_unshaped_is_refused(world, capsys):
    world["ledger"].coverage = {"prd_id": PRD, "status": "approved", "shaped": False}
    code, _out, err = _create(capsys, "--like", LIKE)
    assert code == drain.EXIT_REFUSED
    assert "shaped: False" in err
    _wrote_nothing(world)


@posix_only
def test_coverage_that_cannot_be_read_is_a_refusal_not_a_pass(world, capsys, monkeypatch):
    """Absent is not approved: a coverage read that failed must not install a drain."""
    def down(prd: str) -> dict:
        raise client_mod.ServerUnreachable("could not reach http://ledger.invalid:9")
    monkeypatch.setattr(world["ledger"], "prd_coverage", down)
    code, _out, err = _create(capsys, "--like", LIKE)
    assert code == drain.EXIT_REFUSED
    assert f"cannot read {PRD}'s coverage" in err
    _wrote_nothing(world)


# ---- an existing service / env file (clause 1) ---------------------------------------------

@posix_only
def test_an_installed_service_with_that_name_is_refused_pointing_at_service_status(
        world, capsys):
    unit = service.unit_path_for(NAME, world["kind"])
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_bytes(b"someone else's unit\n")
    code, _out, err = _create(capsys, "--like", LIKE)
    assert code == drain.EXIT_REFUSED
    assert f"a service named {NAME!r} is already installed" in err
    assert f"gbfleet service status --name {NAME}" in err
    assert unit.read_bytes() == b"someone else's unit\n", "the unit was overwritten"
    assert world["ledger"].opened == [] and world["ledger"].minted == []
    assert not _clone(world).exists() and not service.env_path_for(NAME).exists()
    assert world["fake"].calls == []


@posix_only
def test_an_existing_env_file_is_refused_and_left_as_it_was(world, capsys):
    env = service.env_path_for(NAME)
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text("GBFLEET_API_KEY=gb_test_not_a_real_key\n", encoding="utf-8")
    code, _out, err = _create(capsys, "--like", LIKE)
    assert code == drain.EXIT_REFUSED
    assert f"{env} already exists" in err
    assert env.read_text(encoding="utf-8") == "GBFLEET_API_KEY=gb_test_not_a_real_key\n"
    assert world["ledger"].opened == [] and world["ledger"].minted == []
    assert not _clone(world).exists()
    assert not service.unit_path_for(NAME, world["kind"]).exists()
    assert world["fake"].calls == []


# ---- the captured PATH (clause 1, and the sabotage clause) ---------------------------------

@posix_only
def test_an_empty_captured_path_is_refused(world, capsys, monkeypatch):
    monkeypatch.setenv("PATH", "")
    code, _out, err = _create(capsys, "--like", LIKE)
    assert code == drain.EXIT_REFUSED
    assert "the PATH this service would capture is empty" in err
    _wrote_nothing(world)


@posix_only
def test_an_adapter_the_captured_path_does_not_resolve_is_refused(world, capsys, monkeypatch):
    """PATH holds git and nothing else, so the stub `claude` is not on it."""
    only_git = world["tmp"] / "only-git"
    only_git.mkdir()
    git = shutil.which("git")
    assert git, "these tests need git"
    (only_git / "git").symlink_to(git)
    monkeypatch.setenv("PATH", str(only_git))
    code, _out, err = _create(capsys, "--like", LIKE)
    assert code == drain.EXIT_REFUSED
    assert "adapter 'claude' needs 'claude', which does not resolve on the PATH" in err
    assert str(only_git) in err
    _wrote_nothing(world)


@posix_only
def test_the_installed_unit_captures_the_path_preflight_checked(world, capsys):
    code, _out, err = _create(capsys, "--like", LIKE)
    assert code == 0, err
    assert service.status(NAME, kind=world["kind"]).path_env == os.environ["PATH"]


# ---- a second `until` on the checkout (clause 1) -------------------------------------------

@posix_only
def test_a_second_until_on_the_checkout_is_refused(world, capsys, monkeypatch):
    """Held through `lock.hold` — the very lock `until` takes on its repo — not a lookalike."""
    states = world["tmp"] / "state"
    states.mkdir()
    monkeypatch.setattr(state_mod, "state_root", lambda: states)
    mine = _own_checkout(world)
    with hold(mine):
        code, _out, err = _create(capsys, "--like", LIKE, "--repo", str(mine))
    assert code == drain.EXIT_REFUSED
    assert f"another `until` (or `mcp`) is running against {mine.resolve()}" in err
    assert f"pid {os.getpid()}" in err
    assert not (mine / ".git" / drain.MARKER).exists()
    _wrote_nothing(world)


@posix_only
def test_the_same_checkout_is_accepted_once_until_has_let_go(world, capsys, monkeypatch):
    """The other half: the check reads the lock, not the lock file's existence."""
    states = world["tmp"] / "state"
    states.mkdir()
    monkeypatch.setattr(state_mod, "state_root", lambda: states)
    mine = _own_checkout(world)
    with hold(mine):
        pass
    assert list(states.iterdir()), "hold() should have left its (empty) lock file"
    code, _out, err = _create(capsys, "--like", LIKE, "--repo", str(mine))
    assert code == 0, err


# ---- dry-run flags vs install flags (clause 2) ---------------------------------------------

@posix_only
def test_install_flags_that_drift_from_the_dry_run_are_refused_naming_the_flag(
        world, capsys, monkeypatch):
    real = service.install_args

    def drifted(args: list[str]) -> list[str]:
        out = real(args)
        i = out.index("--max-workers")
        return [*out[:i + 1], "9", *out[i + 2:]]

    monkeypatch.setattr(service, "install_args", drifted)
    code, _out, err = _create(capsys, "--like", LIKE)
    assert code == drain.EXIT_REFUSED
    assert "differ from the dry run's at --max-workers (dry run: '3', install: '9')" in err
    _wrote_nothing(world)


def test_flag_drift_names_a_flag_only_one_side_has():
    dry = ["--repo", "/r", "--prd", PRD, "--merge"]
    assert drain.flag_drift(dry, list(dry)) == ""
    assert drain.flag_drift(dry, dry[:-1]).startswith("--merge (dry run: '', install: 'absent')")
    assert drain.flag_drift(dry, [*dry, "--", "x"]).startswith("-- ")
    assert drain.flag_drift(["--x=1"], ["--x", "1"]).startswith("the argv spelling")


# ---- a plan for another PRD (clause 3) -----------------------------------------------------

@posix_only
def test_a_plan_for_a_different_prd_is_refused_naming_both(world, capsys, monkeypatch):
    real = drain.until_args_for

    def other(opts, copied, repo):
        args = real(opts, copied, repo)
        return [a if a != PRD else "GRPH-P95" for a in args]

    monkeypatch.setattr(drain, "until_args_for", other)
    code, _out, err = _create(capsys, "--like", LIKE)
    assert code == drain.EXIT_REFUSED
    assert f"the plan is for GRPH-P95, not the requested {PRD}" in err
    _wrote_nothing(world)


def test_prds_in_reads_both_spellings_and_ignores_the_child_tail():
    assert drain._prds_in(["--prd", "A", "--prd=B", "--", "--prd", "C"]) == ["A", "B"]


# ---- the platform (clause 4) ---------------------------------------------------------------

@posix_only
@pytest.mark.parametrize("found", [service.Host("schtasks"),
                                   service.Host("", "no launchctl on PATH")])
def test_an_unsupported_platform_is_refused_with_a_named_reason(
        world, capsys, monkeypatch, found):
    monkeypatch.setattr(service, "host", lambda: found)
    code, _out, err = _create(capsys, "--like", LIKE)
    assert code == drain.EXIT_REFUSED
    assert "macOS (launchd) or Linux (systemd --user)" in err
    assert (found.kind or found.why) in err
    _wrote_nothing(world)
    assert world["ledger"].opened == []


# ---- the session's coverage read -----------------------------------------------------------

def test_the_session_reads_coverage_with_a_get_and_its_bearer(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPHBAN_HOME", str(tmp_path))
    (tmp_path / "session.json").write_text('{"refresh_token": "rt_fake"}', encoding="utf-8")
    seen: list[tuple[str, str, str]] = []

    def server(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("authorization", "")))
        if request.url.path == "/api/auth/refresh":
            return httpx.Response(200, json={"access_token": "at_fake"})
        return httpx.Response(200, json={"prd_id": PRD, "status": "approved", "shaped": True})

    session = client_mod.LedgerSession.open("http://ledger.invalid",
                                            transport=httpx.MockTransport(server))
    assert session.prd_coverage(PRD)["status"] == "approved"
    assert seen[1] == ("GET", f"/api/prds/{PRD}/coverage", "Bearer at_fake")
    assert client_mod.SESSION_PATHS == {"/api/auth/refresh", "/api/api-keys"}, \
        "the coverage read must not widen what the session may POST"
