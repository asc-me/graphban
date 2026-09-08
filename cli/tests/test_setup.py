"""`gban setup` — everything between a session and a delegating agent (GRPH-792).

Three of these tests exist because the failure they describe is invisible when it happens.

- **The shadowed write.** Claude Code reads `~/.claude.json`'s per-project `mcpServers` in
  preference to a repository `.mcp.json`. A setup that wrote the repository file while a stale
  entry shadowed it would report success and leave the agent authenticating with the old key —
  and the harness reports that as a JSON parse error, because it is parsing a 401 body.
- **The committed credential.** This command's job is to write a key into a file. Pointed at a
  tracked file that becomes "commit a key", and a warning attached to it still does it.
- **The tier that is not a permission.** A manifest missing `fleet` does not refuse `delegate`;
  it does not advertise it. The symptom is an agent reporting that the tool does not exist.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from gban import config, doctor as doctor_mod, setup as setup_mod
from gban.client import Refused

URL = "http://gb.invalid"


@pytest.fixture(autouse=True)
def no_supervisor(monkeypatch):
    """The local half is a separate question; every test here is about the ledger and the
    config. Left real, a machine with `gbfleet` installed and one without would disagree."""
    monkeypatch.setattr(doctor_mod, "find_supervisor", lambda: "")
    monkeypatch.setattr(doctor_mod, "offer_to_install", lambda *a, **k: False)


class Server:
    """Mints, and answers `get_context` as the deployed one does."""

    def __init__(self, *, writable=("core",), missing=(), refuse=None, issued=()):
        self.writable, self.missing, self.refuse = list(writable), list(missing), refuse
        self.minted = []
        #: Every key this deployment knows. Anything else 401s, as a real one does.
        self.issued = set(issued)

    def call(self, method, path, body=None):
        if path == "/api/api-keys":
            if self.refuse is not None:
                raise self.refuse
            self.minted.append(body)
            fresh = f"gb_sk_new{len(self.minted)}"
            self.issued.add(fresh)
            return {"id": f"key_{len(self.minted)}", "plaintext": fresh}
        raise AssertionError(f"unexpected {method} {path}")


def _context(server):
    """`get_context` as the deployment answers it — INCLUDING for a key it has never heard of.

    A mock that answered every key identically would make the stale-credential case
    untestable, which is the case this command exists for: the key in the config is not
    invalid in some abstract way, it is a credential the server does not know.
    """
    def call(self, method, path, body=None):
        if self.api_key not in server.issued:
            raise Refused(401, "invalid api key", "")
        ctx = {"project_id": "core", "writable_projects": server.writable,
               "missing_tiers": [{"name": t} for t in server.missing]}
        return {"result": {"content": [{"text": json.dumps(ctx)}]}}
    return call


@pytest.fixture()
def wired(monkeypatch):
    def _wire(server):
        monkeypatch.setattr(setup_mod.Client, "call", _context(server))
        return server
    return _wire


def _run(server, repo, home, scope="user", wired=None):
    wired(server)
    return setup_mod.run(server, URL, "core", repo, scope=scope, install=False, home=home)


# ---- the happy path ---------------------------------------------------------------------------

def test_one_run_writes_both_servers_and_a_key_that_never_expires(tmp_path, wired):
    repo, home = tmp_path / "repo", tmp_path / ".claude.json"
    repo.mkdir()
    lines, code, made = _run(Server(), repo, home, wired=wired)

    assert code == 0, lines
    blob = json.loads(home.read_text())
    servers = blob["projects"][str(repo.resolve())]["mcpServers"]
    assert set(servers) == {"graphban", "gbfleet"}
    assert servers["graphban"]["headers"]["X-API-Key"].startswith("gb_sk_")
    assert servers["gbfleet"]["command"] == "gbfleet"
    assert "--project" in servers["gbfleet"]["args"]


def test_the_credential_is_minted_with_no_expiry_and_the_fleet_tier(tmp_path, wired):
    """The decision this command exists to get right. `gban keys mint` posts to
    /api/fleet/keys, which sets FLEET_KEY_DAYS = 1 — correct for a wave, wrong for a project.
    Only seats expire."""
    repo, home = tmp_path / "repo", tmp_path / ".claude.json"
    repo.mkdir()
    server = Server()
    _run(server, repo, home, wired=wired)

    body = server.minted[0]
    assert body["expires_in_days"] is None, "the delegation credential must not expire"
    assert "fleet" in body["tool_tiers"]
    assert body["project_id"] == "core"


def test_the_supervisor_key_travels_in_the_environment_not_the_arguments(tmp_path, wired):
    """`ps` is world-readable, and a credential in argv is a credential every process on the
    box can read."""
    repo, home = tmp_path / "repo", tmp_path / ".claude.json"
    repo.mkdir()
    _run(Server(), repo, home, wired=wired)

    fleet = json.loads(home.read_text())["projects"][str(repo.resolve())]["mcpServers"]["gbfleet"]
    assert doctor_mod.SUPERVISOR_KEY_ENV in fleet["env"]
    assert not any(a.startswith("gb_sk_") for a in fleet["args"])


# ---- the refusals ------------------------------------------------------------------------------

def test_a_git_tracked_target_is_refused_not_warned(tmp_path, wired, monkeypatch):
    """Writing a key into a tracked file is one commit from publishing it, and doing it with a
    warning attached still does it."""
    repo, home = tmp_path / "repo", tmp_path / ".claude.json"
    repo.mkdir()
    monkeypatch.setattr(setup_mod, "tracked_by_git", lambda path: True)

    server = Server()
    lines, code, _ = _run(server, repo, home, scope="project", wired=wired)

    assert code == 1
    assert server.minted == [], "minted a key it then refused to write"
    assert not (repo / ".mcp.json").exists()
    assert any("git tracks" in l["detail"] for l in lines)
    assert any("--scope user" in l["detail"] for l in lines)


def test_a_shadowed_project_write_is_redirected_to_the_file_that_wins(tmp_path, wired):
    """THE INVISIBLE ONE. Writing .mcp.json under a shadowing user entry reports success and
    leaves the agent on the old key."""
    repo, home = tmp_path / "repo", tmp_path / ".claude.json"
    repo.mkdir()
    home.write_text(json.dumps({"projects": {str(repo.resolve()): {
        "mcpServers": {"graphban": {"headers": {"X-API-Key": "gb_sk_stale"}}}}}}))

    lines, code, _ = _run(Server(), repo, home, scope="project", wired=wired)

    assert code == 0
    assert not (repo / ".mcp.json").exists(), "wrote a file the harness would never read"
    servers = json.loads(home.read_text())["projects"][str(repo.resolve())]["mcpServers"]
    assert servers["graphban"]["headers"]["X-API-Key"] != "gb_sk_stale"
    assert any("outranks" in l["detail"] for l in lines)


def test_a_dead_key_in_the_config_is_replaced(tmp_path, wired):
    """The shape this session opened with: the configured credential 401s, and the harness
    reports a JSON parse error because it is parsing the 401 body."""
    repo, home = tmp_path / "repo", tmp_path / ".claude.json"
    repo.mkdir()
    home.write_text(json.dumps({"projects": {str(repo.resolve()): {
        "mcpServers": {"graphban": {"headers": {"X-API-Key": "gb_sk_dead"}}}}}}))

    server = Server()
    lines, code, _ = _run(server, repo, home, wired=wired)

    assert code == 0
    assert len(server.minted) == 1
    servers = json.loads(home.read_text())["projects"][str(repo.resolve())]["mcpServers"]
    assert servers["graphban"]["headers"]["X-API-Key"] in server.issued


def test_a_key_that_cannot_write_the_project_fails(tmp_path, wired):
    repo, home = tmp_path / "repo", tmp_path / ".claude.json"
    repo.mkdir()
    lines, code, _ = _run(Server(writable=()), repo, home, wired=wired)

    assert code == 1
    assert any("cannot write" in l["detail"] for l in lines)


def test_a_missing_fleet_tier_fails_and_says_what_it_looks_like(tmp_path, wired):
    """A tier decides what is ADVERTISED, never what may be called — so the symptom is an
    agent reporting the tool does not exist, which reads as our bug rather than a grant."""
    repo, home = tmp_path / "repo", tmp_path / ".claude.json"
    repo.mkdir()
    lines, code, _ = _run(Server(missing=("fleet",)), repo, home, wired=wired)

    assert code == 1
    detail = " ".join(l["detail"] for l in lines)
    assert "fleet tier is missing" in detail
    assert "not existing" in detail


def test_a_refused_mint_writes_nothing(tmp_path, wired):
    repo, home = tmp_path / "repo", tmp_path / ".claude.json"
    repo.mkdir()
    lines, code, _ = _run(Server(refuse=Refused(403, "no", "")), repo, home, wired=wired)

    assert code == 1
    assert not home.exists()


# ---- running it twice ---------------------------------------------------------------------------

def test_a_second_run_reuses_the_working_credential(tmp_path, wired):
    """Re-minting on every run would leave a live orphan credential behind each time."""
    repo, home = tmp_path / "repo", tmp_path / ".claude.json"
    repo.mkdir()
    server = Server()
    _run(server, repo, home, wired=wired)
    lines, code, made = _run(server, repo, home, wired=wired)

    assert code == 0
    assert len(server.minted) == 1, "minted a second key for an already-working setup"
    assert made.get("reused") is True
    assert any("already configured" in l["detail"] for l in lines)


def test_a_configured_key_that_cannot_delegate_is_replaced_and_the_old_one_named(tmp_path, wired):
    """Perms repair is a re-mint: tool_tiers are fixed at mint and there is no PATCH."""
    repo, home = tmp_path / "repo", tmp_path / ".claude.json"
    repo.mkdir()
    home.write_text(json.dumps({"projects": {str(repo.resolve()): {
        "mcpServers": {"graphban": {"headers": {"X-API-Key": "gb_sk_weak"}}}}}}))
    server = Server(missing=("fleet",), issued={"gb_sk_weak"})
    lines, code, _ = _run(server, repo, home, wired=wired)

    # The configured key authenticates fine — it simply cannot see `delegate`. So it is
    # replaced rather than patched, and the one it replaces is named rather than orphaned
    # silently: `tool_tiers` are fixed at mint and no route changes them.
    assert any("still live" in l["detail"] for l in lines)
    assert len(server.minted) == 1
    servers = json.loads(home.read_text())["projects"][str(repo.resolve())]["mcpServers"]
    assert servers["graphban"]["headers"]["X-API-Key"] != "gb_sk_weak"
    # …and the new one is refused too, because this server hands out the same weak manifest.
    assert code == 1


def test_other_servers_in_the_file_survive(tmp_path, wired):
    """These files hold other people's servers, and everything else the harness keeps."""
    repo, home = tmp_path / "repo", tmp_path / ".claude.json"
    repo.mkdir()
    home.write_text(json.dumps({"theme": "dark", "projects": {str(repo.resolve()): {
        "mcpServers": {"context7": {"url": "http://elsewhere"}}}}}))

    _run(Server(), repo, home, wired=wired)

    blob = json.loads(home.read_text())
    assert blob["theme"] == "dark"
    servers = blob["projects"][str(repo.resolve())]["mcpServers"]
    assert "context7" in servers and "graphban" in servers


def test_every_route_setup_calls_is_documented(tmp_path, wired, monkeypatch):
    """The check `test_acts.py` makes for the verbs it drives, made here for the one it
    cannot: a verb pointed at an invented path is a verb that 404s in front of somebody
    following the README."""
    reference = (Path(__file__).resolve().parents[2] / "docs" / "api-reference.md"
                 ).read_text(encoding="utf-8")
    seen = []

    class Watching(Server):
        def call(self, method, path, body=None):
            seen.append(path)
            return super().call(method, path, body)

    def context(self, method, path, body=None):
        seen.append(path)
        return _context(Server(issued={self.api_key}))(self, method, path, body)

    monkeypatch.setattr(setup_mod.Client, "call", context)
    repo, home = tmp_path / "repo", tmp_path / ".claude.json"
    repo.mkdir()
    setup_mod.run(Watching(), URL, "core", repo, scope="user", install=False, home=home)

    assert seen, "setup made no HTTP call"
    for path in seen:
        assert f"`{path}`" in reference, f"{path} is not in docs/api-reference.md"


# ---- the skill ----------------------------------------------------------------------------------

def test_setup_installs_the_skill_where_the_product_says_skills_live(tmp_path, wired):
    """`backend/app/services/artifacts.py` maps the skill tier to this exact path. A skill
    written somewhere else is one the inventory scan reports as an unknown artifact."""
    repo, home = tmp_path / "repo", tmp_path / ".claude.json"
    repo.mkdir()
    _run(Server(), repo, home, wired=wired)

    dest = repo / ".claude" / "skills" / "graphban-delegation" / "SKILL.md"
    assert dest.exists()
    assert dest.read_text().startswith("---\nname: graphban-delegation\n")


def test_an_edited_skill_is_left_alone(tmp_path, wired):
    """A setup command that silently replaced somebody's edit would take it away without
    saying so."""
    repo, home = tmp_path / "repo", tmp_path / ".claude.json"
    repo.mkdir()
    dest = repo / ".claude" / "skills" / "graphban-delegation" / "SKILL.md"
    dest.parent.mkdir(parents=True)
    dest.write_text("mine\n")

    lines, _, _ = _run(Server(), repo, home, wired=wired)

    assert dest.read_text() == "mine\n"
    assert any("left alone" in l["detail"] for l in lines)


def test_the_skill_tells_the_agent_the_two_things_it_cannot_work_out(tmp_path):
    """Both are failures that read as something else. A missing tier reads as the tool not
    existing; a missing session reads as something to retry, when the remedy is a person at a
    terminal."""
    # Whitespace-normalised: these are prose assertions, and a reflow that moved a line break
    # would fail them while the skill still said exactly the same thing.
    body = " ".join(setup_mod.skill_source().read_text(encoding="utf-8").split())

    assert "missing_tiers" in body
    assert "not to exist" in body or "not exist" in body
    assert "gban login" in body and "tty" in body
    # It drives the CLI rather than restating the setup, which is what keeps it from becoming
    # a fourth copy of the procedure that drifts from the other three.
    assert "gban setup" in body
    assert "restart the harness" in body


def test_the_skill_ships_in_the_wheel(tmp_path):
    """Package DATA, not a .py file — the one class of file a packaging config silently drops.
    A skill that exists in the repo and not in the wheel installs nothing for anyone."""
    import subprocess
    import sys
    import zipfile

    root = Path(__file__).resolve().parents[1]
    out = tmp_path / "dist"
    done = None
    for argv in ([sys.executable, "-m", "build", "--wheel", "--outdir", str(out), str(root)],
                 ["uv", "build", "--wheel", "--out-dir", str(out), str(root)]):
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode == 0:
            break
    if done is None or done.returncode != 0:
        pytest.skip("no wheel builder available")
    wheel = next(out.glob("*.whl"))
    names = zipfile.ZipFile(wheel).namelist()
    assert any(n.endswith("skills/graphban-delegation/SKILL.md") for n in names), names[:20]
