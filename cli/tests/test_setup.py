"""`gban setup` — everything between a session and a delegating agent (GRPH-792, GRPH-825).

These tests exist because the failure they describe is invisible when it happens.

- **The shadowed write.** Claude Code reads `~/.claude.json`'s per-project `mcpServers` in
  preference to a repository `.mcp.json`. A setup that wrote the repository file while a stale
  entry shadowed it would report success and leave the agent authenticating with the old key —
  and the harness reports that as a JSON parse error, because it is parsing a 401 body.
- **The committed credential.** This command's job is to write a key into a file. Pointed at a
  tracked file that becomes "commit a key", and a warning attached to it still does it.
- **The tier that is not a permission.** A manifest missing `fleet` does not refuse `delegate`;
  it does not advertise it. The symptom is an agent reporting that the tool does not exist.
- **The other harness.** Grok reads `~/.grok/config.toml` (`mcp_servers`, TOML). Writing only
  Claude's file while Grok holds a different key reports success and leaves `delegate`
  unadvertised (GRPH-825). Reuse that skips the write leaves `gbfleet` missing even on Claude.
"""
from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

import pytest

from gban import config, doctor as doctor_mod, gitignore, setup as setup_mod
from gban.doctor import _line
from gban.client import Refused

URL = "http://gb.invalid"


@pytest.fixture(autouse=True)
def no_supervisor(monkeypatch):
    """The local half is a separate question; every test here is about the ledger and the
    config. Left real, a machine with `gbfleet` installed and one without would disagree."""
    monkeypatch.setattr(doctor_mod, "find_supervisor", lambda: "")
    monkeypatch.setattr(doctor_mod, "offer_to_install", lambda *a, **k: False)


@pytest.fixture(autouse=True)
def no_grok_config(tmp_path_factory, monkeypatch):
    """A test that does not create this file is a Claude-only setup, which is still the
    common case. Tests that cover Grok write the file first. Pointed at the real
    `~/.grok/config.toml`, every test here would rewrite the developer's harness."""
    path = tmp_path_factory.mktemp("grokhome") / "config.toml"
    monkeypatch.setattr(setup_mod, "grok_home", lambda: path)
    return path


@pytest.fixture(autouse=True)
def gban_home(tmp_path, monkeypatch):
    """setup now stores the minted key next to session.json. Without this, every test
    here would write `~/.graphban/supervisor.json` on the developer's machine."""
    monkeypatch.setenv(config.HOME_ENV, str(tmp_path / "gbanhome"))


class Server:
    """Mints, and answers `get_context` as the deployed one does."""

    def __init__(self, *, writable=("core",), missing=(), refuse=None, issued=(),
                 missing_for=None):
        self.writable, self.missing, self.refuse = list(writable), list(missing), refuse
        self.missing_for = dict(missing_for or {})
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

    Live `missing_tiers` objects use `tier`, not `name` (`mcp_server.py`). Spelling it
    `name` here made verify PASS against a key that could not see `delegate`.
    """
    def call(self, method, path, body=None):
        if self.api_key not in server.issued:
            raise Refused(401, "invalid api key", "")
        missing = server.missing_for.get(self.api_key, server.missing)
        ctx = {"project_id": "core", "writable_projects": server.writable,
               "missing_tiers": [{"tier": t} for t in missing]}
        return {"result": {"content": [{"text": json.dumps(ctx)}]}}
    return call


@pytest.fixture()
def wired(monkeypatch):
    def _wire(server):
        monkeypatch.setattr(setup_mod.Client, "call", _context(server))
        return server
    return _wire


def _run(server, repo, home, scope="user", wired=None, install=False):
    wired(server)
    return setup_mod.run(server, URL, "core", repo, scope=scope, install=install, home=home)


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
    stored = config.stored_supervisor_key("core")
    assert stored == servers["graphban"]["headers"]["X-API-Key"]
    assert config.is_private(config.home() / config.SUPERVISOR_KEYS_FILE), (
        "a credential at rest is owner-only, same as session.json"
    )


def test_setup_refuses_to_store_a_refresh_token_as_the_supervisor_key(tmp_path):
    """session.json is a different credential. Mixing them is the remaining GRPH-782 hole."""
    assert config.save_supervisor_key("core", "refresh-token") is None
    assert config.stored_supervisor_key("core") == ""
    config.save_supervisor_key("core", "gb_sk_ok")
    config.save_supervisor_key("core", "refresh-token")
    assert config.stored_supervisor_key("core") == "gb_sk_ok", "a bad write must not clobber"


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
    servers = json.loads(home.read_text())["projects"][str(repo.resolve())]["mcpServers"]
    assert set(servers) == {"graphban", "gbfleet"}


def test_reuse_still_writes_a_missing_gbfleet_entry(tmp_path, wired):
    """THE SECOND DEFECT. Reuse skipped write_entries, so a Claude file that only had
    `graphban` stayed that way while setup reported PASS (GRPH-825)."""
    repo, home = tmp_path / "repo", tmp_path / ".claude.json"
    repo.mkdir()
    home.write_text(json.dumps({"projects": {str(repo.resolve()): {
        "mcpServers": {"graphban": {"type": "http",
                                    "url": URL + "/api/mcp",
                                    "headers": {"X-API-Key": "gb_sk_ok"}}}}}}))

    server = Server(issued={"gb_sk_ok"})
    lines, code, made = _run(server, repo, home, wired=wired)

    assert code == 0, lines
    assert made.get("reused") is True
    assert server.minted == [], "repaired by writing, which is not a reason to mint"
    servers = json.loads(home.read_text())["projects"][str(repo.resolve())]["mcpServers"]
    assert "gbfleet" in servers
    assert doctor_mod.SUPERVISOR_KEY_ENV in servers["gbfleet"]["env"]


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


# ---- Grok (GRPH-825) ---------------------------------------------------------------------------

def test_setup_writes_grok_toml_mcp_servers_when_the_file_exists(
        tmp_path, wired, no_grok_config):
    """THE MEASURED ONE. Grok reads `mcp_servers` in TOML. `mcpServers` parses and loads
    nothing; JSON in a `.toml` path is the same silence (GRPH-575, now for the parent)."""
    repo, home = tmp_path / "repo", tmp_path / ".claude.json"
    repo.mkdir()
    no_grok_config.write_text("# keep me\n[ui]\nmax_thoughts_width = 120\n")

    lines, code, _ = _run(Server(), repo, home, wired=wired)

    assert code == 0, lines
    text = no_grok_config.read_text()
    assert "# keep me" in text
    parsed = tomllib.loads(text)
    assert "mcp_servers" in parsed
    assert "mcpServers" not in parsed
    gb = parsed["mcp_servers"]["graphban"]
    assert gb["url"].endswith("/api/mcp")
    assert gb["headers"]["X-API-Key"].startswith("gb_sk_")
    fleet = parsed["mcp_servers"]["gbfleet"]
    assert fleet["command"] == "gbfleet"
    assert "--project" in fleet["args"]
    assert "--workspace" in fleet["args"]
    ws = Path(fleet["args"][fleet["args"].index("--workspace") + 1])
    assert ws == no_grok_config.parent / "gbfleet-wt" / repo.name, (
        "Grok worktrees must sit next to Grok's config, not as a sibling of the repo "
        "(GRPH-826)"
    )
    assert doctor_mod.SUPERVISOR_KEY_ENV in fleet["env"]
    assert parsed["ui"]["max_thoughts_width"] == 120
    claude = json.loads(home.read_text())["projects"][str(repo.resolve())]["mcpServers"]
    assert claude["graphban"]["headers"]["X-API-Key"] == gb["headers"]["X-API-Key"]
    assert "--workspace" not in claude["gbfleet"]["args"], (
        "Claude can write the sibling default; moving it would be fixing the wrong harness"
    )


def test_a_working_claude_key_is_copied_onto_a_grok_key_that_cannot_delegate(
        tmp_path, wired, no_grok_config):
    """Claude's key can see `fleet`. Grok's cannot. Verifying only Claude PASSed and left
    Grok on the weak key — which is how this session opened."""
    repo, home = tmp_path / "repo", tmp_path / ".claude.json"
    repo.mkdir()
    home.write_text(json.dumps({"projects": {str(repo.resolve()): {
        "mcpServers": {"graphban": {"headers": {"X-API-Key": "gb_sk_good"}},
                       "gbfleet": {"command": "gbfleet"}}}}}))
    no_grok_config.write_text(
        '[mcp_servers.graphban]\nurl = "http://gb.invalid/api/mcp"\nenabled = true\n\n'
        '[mcp_servers.graphban.headers]\nX-API-Key = "gb_sk_weak"\n'
    )
    server = Server(issued={"gb_sk_good", "gb_sk_weak"},
                    missing_for={"gb_sk_weak": ("fleet",)})

    lines, code, made = _run(server, repo, home, wired=wired)

    assert code == 0, lines
    assert made.get("reused") is True
    assert server.minted == [], "the working Claude key is the one to write, not a third"
    grok = tomllib.loads(no_grok_config.read_text())
    assert grok["mcp_servers"]["graphban"]["headers"]["X-API-Key"] == "gb_sk_good"
    assert "gbfleet" in grok["mcp_servers"]


def test_setup_does_not_invent_a_grok_config_when_grok_has_never_run(
        tmp_path, wired, no_grok_config):
    repo, home = tmp_path / "repo", tmp_path / ".claude.json"
    repo.mkdir()
    _run(Server(), repo, home, wired=wired)
    assert not no_grok_config.exists()


def test_a_git_tracked_grok_project_file_is_refused(tmp_path, wired, monkeypatch):
    repo, home = tmp_path / "repo", tmp_path / ".claude.json"
    repo.mkdir()
    grok_proj = repo / ".grok" / "config.toml"
    grok_proj.parent.mkdir()
    grok_proj.write_text("[ui]\n")
    monkeypatch.setattr(setup_mod, "tracked_by_git",
                        lambda path: path == grok_proj)

    server = Server()
    lines, code, _ = _run(server, repo, home, scope="project", wired=wired)

    assert any("git tracks" in l["detail"] and ".grok/config.toml" in l["detail"]
               for l in lines)
    # Claude's project file is not tracked, so it is still written. The grok FAIL is
    # a real finding and so the exit is 1 — a PASS report would hide the refusal.
    assert code == 1
    assert (repo / ".mcp.json").exists()
    assert tomllib.loads(grok_proj.read_text()) == {"ui": {}}


def test_reuse_repairs_a_gbfleet_entry_missing_workspace(tmp_path, wired, no_grok_config):
    """THE FOLLOW-ON. A Grok gbfleet written before GRPH-826 has no --workspace. Reuse
    that skipped the write would leave spawn dying as git 128."""
    repo, home = tmp_path / "repo", tmp_path / ".claude.json"
    repo.mkdir()
    home.write_text(json.dumps({"projects": {str(repo.resolve()): {
        "mcpServers": {"graphban": {"headers": {"X-API-Key": "gb_sk_ok"}}}}}}))
    no_grok_config.write_text(
        '[mcp_servers.graphban]\nurl = "http://gb.invalid/api/mcp"\nenabled = true\n\n'
        '[mcp_servers.graphban.headers]\nX-API-Key = "gb_sk_ok"\n\n'
        '[mcp_servers.gbfleet]\ncommand = "gbfleet"\n'
        'args = ["mcp", "--repo", "/old", "--server", "http://gb.invalid", '
        '"--project", "core"]\n'
        'env = { GBFLEET_API_KEY = "gb_sk_ok" }\nenabled = true\n'
    )
    server = Server(issued={"gb_sk_ok"})
    _, code, made = _run(server, repo, home, wired=wired)

    assert code == 0
    assert made.get("reused") is True
    assert server.minted == []
    assert config.stored_supervisor_key("core") == "gb_sk_ok"
    args = tomllib.loads(no_grok_config.read_text())["mcp_servers"]["gbfleet"]["args"]
    assert "--workspace" in args


def test_an_unwritable_grok_workspace_is_not_a_pass(tmp_path, wired, no_grok_config, monkeypatch):
    """gbfleet doctor already FAILs this. Setup reporting PASS is how spawn looked like git."""
    repo, home = tmp_path / "repo", tmp_path / ".claude.json"
    repo.mkdir()
    no_grok_config.write_text("[ui]\n")
    monkeypatch.setattr(setup_mod, "workspace_writable",
                        lambda path: (False, "Operation not permitted"))

    lines, code, _ = _run(Server(), repo, home, wired=wired)

    assert code == 1
    assert any("not writable" in l["detail"] for l in lines)
    assert "mcp_servers" not in tomllib.loads(no_grok_config.read_text())


def test_args_arrays_are_not_mistaken_for_table_headers(tmp_path, wired, no_grok_config):
    """`args = ["mcp", ...]` contains `[`. Walking until the next `[` stops there and
    leaves garbage that Grok cannot parse."""
    repo, home = tmp_path / "repo", tmp_path / ".claude.json"
    repo.mkdir()
    no_grok_config.write_text(
        '[mcp_servers.gbfleet]\ncommand = "gbfleet"\n'
        'args = ["mcp", "--repo", "/old"]\n'
        'env = { GBFLEET_API_KEY = "gb_sk_old" }\nenabled = true\n'
    )
    _run(Server(), repo, home, wired=wired)
    parsed = tomllib.loads(no_grok_config.read_text())
    assert parsed["mcp_servers"]["gbfleet"]["args"][0] == "mcp"
    assert "--project" in parsed["mcp_servers"]["gbfleet"]["args"]


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
    # THE CALL: skipping `skill()` in run() would leave this dest missing the
    # watcher paragraph while skill_source() tests still passed.
    body = dest.read_text()
    assert "Do not wait for the user to ask how the wave is doing" in body
    assert "watch-wave" in body


def test_setup_installs_every_shipped_skill(tmp_path, wired):
    """A second skill that exists only in the wheel is one setup never teaches.
    Naming graphban-delegation in install_skill is how that happens."""
    repo, home = tmp_path / "repo", tmp_path / ".claude.json"
    repo.mkdir()
    lines, _, _ = _run(Server(), repo, home, wired=wired)

    slugs = setup_mod.shipped_slugs()
    assert "graphban-delegation" in slugs
    assert "watch-wave" in slugs
    for slug in slugs:
        dest = repo / ".claude" / "skills" / slug / "SKILL.md"
        assert dest.exists(), slug
        assert dest.read_text() == setup_mod.skill_source(slug).read_text()
    assert sum(1 for l in lines if l.get("name") == "skill" and l.get("status") == "PASS") == len(slugs)


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
    # Editing one skill does not skip the others.
    other = repo / ".claude" / "skills" / "watch-wave" / "SKILL.md"
    assert other.exists()
    assert other.read_text() == setup_mod.skill_source("watch-wave").read_text()


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
    # Two parent harnesses. Naming only one is how GRPH-825 shipped.
    assert "claude.json" in body
    assert "config.toml" in body
    assert "mcp_servers" in body
    assert "gbfleet-wt" in body
    # Spawn going silent is not a reason for the parent to go idle (GRPH-856).
    assert "Do not wait for the user to ask how the wave is doing" in body
    assert "watch-wave" in body
    assert "scheduler_create" in body


def test_the_skill_hands_the_person_a_runnable_line_and_carries_the_rest(tmp_path):
    """The division of labour, asserted because getting it wrong is silent in both
    directions: an agent that asks a person to run everything is useless, and one that tries
    to run `gban login` itself will hunt for a password."""
    body = " ".join(setup_mod.skill_source().read_text(encoding="utf-8").split())

    # The two lines a person types are given verbatim, in the form their harness can run.
    assert "! gban login" in body
    assert "! uv tool install graphban-fleet" in body
    # …and everything either side of them is the agent's.
    assert "Run this yourself" in body
    assert "resume at `gban setup` yourself" in body
    # The restart is a stopping point, not a retry loop.
    assert "read at startup" in body
    assert "do not retry" in body.lower()


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
    assert any(n.endswith("skills/watch-wave/SKILL.md") for n in names), names[:20]


def test_watch_wave_names_the_scheduler_and_cancels_on_idle():
    """The Grok procedure has to live in the shipped skill, not only in a laptop
    copy under ~/.grok. A skill that never says WAVE_IDLE leaves the loop running
    on an idle wave, which is the spam this exists to stop."""
    body = " ".join(setup_mod.skill_source("watch-wave").read_text(encoding="utf-8").split())
    assert "scheduler_create" in body
    assert "WAVE_IDLE" in body
    assert "scheduler_delete" in body
    assert "Do not wait to be asked" in body
    assert "in_progress" in body


# ---- installing the supervisor -------------------------------------------------------------------

def test_setup_installs_the_supervisor_without_asking(tmp_path, wired, monkeypatch):
    """`offer_to_install` prompts because it is reached from a READ-ONLY command. `gban setup`
    already mints a credential and rewrites config: typing it is the consent, and an agent
    driving this could not answer a prompt anyway."""
    repo, home = tmp_path / "repo", tmp_path / ".claude.json"
    repo.mkdir()
    calls = []
    monkeypatch.setattr(doctor_mod, "install_supervisor",
                        lambda *a, **k: (calls.append(1), (True, ""))[1])
    monkeypatch.setattr(doctor_mod, "find_supervisor", lambda: "" if not calls else "/bin/gbfleet")

    lines, code, _ = _run(Server(), repo, home, wired=wired, install=True)

    assert calls, "setup did not install the supervisor"
    assert code == 0
    assert any(l["name"] == "supervisor" and l["status"] == "PASS" for l in lines)


def test_no_install_is_honoured(tmp_path, wired, monkeypatch):
    repo, home = tmp_path / "repo", tmp_path / ".claude.json"
    repo.mkdir()
    monkeypatch.setattr(doctor_mod, "install_supervisor",
                        lambda *a, **k: pytest.fail("installed under --no-install"))

    lines, code, _ = _run(Server(), repo, home, wired=wired, install=False)

    assert code == 0
    assert any("--no-install was given" in l["detail"] for l in lines)


def test_a_failed_install_is_unknown_and_carries_the_reason(tmp_path, wired, monkeypatch):
    """Three different next actions hide behind "no supervisor": it is missing, uv is missing,
    or it installed and is not on PATH. A bare failure would send somebody to the wrong one."""
    repo, home = tmp_path / "repo", tmp_path / ".claude.json"
    repo.mkdir()
    monkeypatch.setattr(doctor_mod, "install_supervisor",
                        lambda *a, **k: (False, "uv is not on PATH"))

    lines, code, _ = _run(Server(), repo, home, wired=wired, install=True)

    assert code == 0, "a missing supervisor is not a failed setup"
    line = next(l for l in lines if l["name"] == "supervisor")
    assert line["status"] == "UNKNOWN"
    assert "uv is not on PATH" in line["detail"]


# ---- --auto --------------------------------------------------------------------------------------

def _repo(root: Path, name: str) -> Path:
    path = root / name
    (path / ".git").mkdir(parents=True)
    return path


def test_auto_matches_a_project_to_a_sibling_repository(tmp_path):
    work = tmp_path / "work"
    _repo(work, "super-arc")
    here = _repo(work, "graphban")

    found, notes = setup_mod.match([{"id": "super-arc", "name": "Super Arc"},
                                    {"id": "core", "name": "GraphBan"}], here)

    assert found["super-arc"].name == "super-arc"
    assert found["core"].name == "graphban", "matched on name as well as id"
    assert notes == []


def test_auto_matches_the_directory_you_are_standing_in(tmp_path):
    work = tmp_path / "work"
    here = _repo(work, "super-arc")

    found, _ = setup_mod.match([{"id": "super-arc", "name": "Super Arc"}], here)

    assert found["super-arc"].resolve() == here.resolve()


def test_auto_ignores_a_directory_that_is_not_a_repository(tmp_path):
    work = tmp_path / "work"
    (work / "super-arc").mkdir(parents=True)
    here = _repo(work, "graphban")

    found, notes = setup_mod.match([{"id": "super-arc", "name": "Super Arc"}], here)

    assert "super-arc" not in found
    assert any("no directory" in n["detail"] for n in notes)


def test_auto_finds_a_worktree_whose_git_is_a_file(tmp_path):
    """A fleet spends its life in worktrees, where `.git` is a FILE. Testing for a directory
    would skip exactly the working copies this tool serves."""
    work = tmp_path / "work"
    wt = work / "super-arc"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: /elsewhere\n")
    here = _repo(work, "graphban")

    found, _ = setup_mod.match([{"id": "super-arc", "name": "Super Arc"}], here)

    assert found["super-arc"].name == "super-arc"


def test_auto_refuses_when_two_directories_answer_to_one_project(tmp_path):
    """A credential minted into the wrong repository is not a mistake anybody notices
    quickly, so ambiguity is refused rather than broken by a rule."""
    work = tmp_path / "work"
    _repo(work, "super-arc")
    _repo(work / "nested", "super-arc")
    here = _repo(work, "graphban")
    # The nested copy is a sibling of nothing; put it where `candidates` looks.
    found, notes = setup_mod.match([{"id": "super-arc", "name": "super arc"}], here)
    assert found.get("super-arc") is not None  # only one is in scope

    # …now make the second one visible, and it must refuse.
    (work / "Super_Arc" / ".git").mkdir(parents=True)
    found, notes = setup_mod.match([{"id": "super-arc", "name": "Super Arc"}], here)
    assert "super-arc" not in found
    assert any("several directories" in n["detail"] for n in notes)


def test_auto_refuses_when_one_directory_answers_to_two_projects(tmp_path):
    work = tmp_path / "work"
    _repo(work, "atlas")
    here = _repo(work, "graphban")

    found, notes = setup_mod.match([{"id": "atlas", "name": "Atlas"},
                                    {"id": "other", "name": "atlas"}], here)

    assert found == {} or "atlas" not in found
    assert any("several projects" in n["detail"] for n in notes)


def test_auto_says_which_projects_it_could_not_place(tmp_path):
    """A sweep that silently skipped a project would leave somebody believing delegation is
    enabled everywhere."""
    here = _repo(tmp_path / "work", "graphban")

    found, notes = setup_mod.match([{"id": "core", "name": "GraphBan"},
                                    {"id": "elsewhere", "name": "Elsewhere"}], here)

    assert list(found) == ["core"]
    assert any(n["name"] == "elsewhere" for n in notes)


def test_auto_does_not_walk_the_whole_tree(tmp_path):
    """Nothing deeper than one level. A recursive walk would be slow, surprising, and would
    start matching vendored copies and worktrees."""
    work = tmp_path / "work"
    _repo(work / "deep" / "deeper", "super-arc")
    here = _repo(work, "graphban")

    found, _ = setup_mod.match([{"id": "super-arc", "name": "Super Arc"}], here)

    assert "super-arc" not in found


# ---- --auto through the command, not just the matcher --------------------------------------------

def test_the_auto_command_sets_up_each_match_and_reports_the_rest(tmp_path, monkeypatch, capsys):
    """`match` being right is not the same as the verb wiring it correctly, and a wiring
    error here would present as a sweep that quietly did nothing."""
    import json as _json

    from gban import cli as cli_mod

    work = tmp_path / "work"
    _repo(work, "super-arc")
    here = _repo(work, "graphban")
    monkeypatch.chdir(here)
    monkeypatch.setenv(config.HOME_ENV, str(tmp_path / "gbanhome"))
    config.save_settings(url=URL)
    config.save_session("r", user="a@b.c")

    class Listing:
        def call(self, method, path, body=None):
            assert path == "/api/projects"
            return [{"id": "super-arc", "name": "Super Arc"},
                    {"id": "core", "name": "GraphBan"},
                    {"id": "nowhere", "name": "Nowhere"}]

    ran = []
    monkeypatch.setattr(cli_mod, "authenticated", lambda url, act="": Listing())
    monkeypatch.setattr(setup_mod, "run",
                        lambda client, url, project, repo, **kw: (
                            ran.append((project, repo.name)),
                            ([_line("config", "PASS", "written", "x")], 0, {}))[1])

    code = cli_mod.main(["--json", "setup", "--auto"])

    assert code == 0
    assert sorted(ran) == [("core", "graphban"), ("super-arc", "super-arc")]
    payload = _json.loads(capsys.readouterr().out)
    assert set(payload["projects"]) == {"core", "super-arc"}
    # The one it could not place is reported, not dropped.
    assert any(l["name"] == "nowhere" for l in payload["lines"])


def test_the_auto_command_refuses_when_it_matches_nothing(tmp_path, monkeypatch, capsys):
    work = tmp_path / "work"
    here = _repo(work, "graphban")
    monkeypatch.chdir(here)
    monkeypatch.setenv(config.HOME_ENV, str(tmp_path / "gbanhome"))
    config.save_settings(url=URL)
    config.save_session("r", user="a@b.c")

    from gban import cli as cli_mod

    class Listing:
        def call(self, method, path, body=None):
            return [{"id": "elsewhere", "name": "Elsewhere"}]

    monkeypatch.setattr(cli_mod, "authenticated", lambda url, act="": Listing())
    monkeypatch.setattr(setup_mod, "run",
                        lambda *a, **k: pytest.fail("set up a project it never matched"))

    assert cli_mod.main(["setup", "--auto"]) == 1
    assert "--project" in capsys.readouterr().err


def test_setup_with_no_session_says_no_session(tmp_path, monkeypatch, capsys):
    """Found by running the built wheel, which every test here could not: they all start from
    a stored session. Resolving the project first reported "no project" and exit 1 to somebody
    who had simply never logged in — a true statement about the wrong thing, and the wrong
    remedy for anyone reading the exit code rather than the prose."""
    from gban import cli as cli_mod
    from gban.client import EXIT_NO_SESSION, NoSession

    monkeypatch.setenv(config.HOME_ENV, str(tmp_path / "empty"))
    monkeypatch.delenv(config.PROJECT_ENV, raising=False)

    def refuse(url, act=""):
        raise NoSession("no stored session", act=act)

    monkeypatch.setattr(cli_mod, "authenticated", refuse)

    assert cli_mod.main(["--server", URL, "setup"]) == EXIT_NO_SESSION
    assert "login" in capsys.readouterr().err


# ---- resolving the project from the directory (GRPH-795) -----------------------------------------

class Listing:
    def __init__(self, rows):
        self.rows = rows

    def call(self, method, path, body=None):
        assert path == "/api/projects", path
        return self.rows


def test_the_directory_names_the_project(tmp_path):
    here = tmp_path / "super-arc"
    here.mkdir()

    pid, how = setup_mod.resolve_project(
        Listing([{"id": "super-arc", "name": "Super Arc"}, {"id": "core", "name": "GraphBan"}]),
        here)

    assert pid == "super-arc"
    assert "super-arc" in how


def test_the_directory_can_match_on_the_project_name_too(tmp_path):
    here = tmp_path / "graphban"
    here.mkdir()

    pid, _ = setup_mod.resolve_project(Listing([{"id": "core", "name": "GraphBan"}]), here)

    assert pid == "core"


def test_an_explicit_project_skips_the_lookup(tmp_path):
    """Naming one says the thing the match exists to work out."""
    class Refusing:
        def call(self, *a, **k):
            pytest.fail("asked the server after being told the project")

    pid, how = setup_mod.resolve_project(Refusing(), tmp_path / "anything", asked="mine")

    assert (pid, how) == ("mine", "named")


def test_no_match_is_an_error_not_a_fallback(tmp_path):
    here = tmp_path / "somewhere-else"
    here.mkdir()

    with pytest.raises(setup_mod.Unresolved) as exc:
        setup_mod.resolve_project(Listing([{"id": "core", "name": "GraphBan"}]), here)

    assert "unable to resolve a project" in str(exc.value)
    assert "core" in str(exc.value), "did not say what it could have matched"


def test_a_stored_default_is_refused_and_named(tmp_path):
    """THE HAZARD THIS REPLACES. `gban login` stores a default so reading verbs have one.
    Using it here would mean logging in once inside project A silently mints a credential for
    A while you are standing in B's repository — and a key in the wrong project is not a
    mistake anybody notices quickly."""
    here = tmp_path / "b-repo"
    here.mkdir()

    with pytest.raises(setup_mod.Unresolved) as exc:
        setup_mod.resolve_project(Listing([{"id": "a", "name": "A"}]), here, stored="a")

    said = str(exc.value)
    assert "deliberately NOT used" in said
    assert "--project a" in said, "refused without saying how to proceed"


def test_a_directory_matching_two_projects_is_refused(tmp_path):
    here = tmp_path / "atlas"
    here.mkdir()

    with pytest.raises(setup_mod.Unresolved) as exc:
        setup_mod.resolve_project(
            Listing([{"id": "atlas", "name": "Atlas"}, {"id": "other", "name": "atlas"}]), here)

    assert "several projects" in str(exc.value)


def test_setup_resolves_the_directory_through_the_command(tmp_path, monkeypatch, capsys):
    """Through the verb, because `resolve_project` being right and `cmd_setup` calling it are
    different claims."""
    from gban import cli as cli_mod

    here = tmp_path / "super-arc"
    here.mkdir()
    monkeypatch.chdir(here)
    monkeypatch.setenv(config.HOME_ENV, str(tmp_path / "home"))
    monkeypatch.delenv(config.PROJECT_ENV, raising=False)
    config.save_settings(url=URL, project="a-different-project")
    config.save_session("r", user="a@b.c")

    seen = {}
    monkeypatch.setattr(cli_mod, "authenticated",
                        lambda url, act="": Listing([{"id": "super-arc", "name": "Super Arc"}]))
    monkeypatch.setattr(setup_mod, "run",
                        lambda client, url, project, repo, **kw: (
                            seen.update(project=project),
                            ([_line("config", "PASS", "written", "x")], 0, {}))[1])

    assert cli_mod.main(["--server", URL, "setup"]) == 0
    assert seen["project"] == "super-arc", "used the stored default over the directory"


def test_a_directory_that_is_not_a_repository_is_flagged_not_refused(tmp_path, wired):
    """`--auto` skips non-repositories; naming a project directly did not check at all, so one
    command answered the same question two ways. Only HALF of setup needs a repository — the
    ledger entry works anywhere, and the supervisor is what cuts worktrees — so this is a
    finding, not a refusal."""
    plain, home = tmp_path / "plain", tmp_path / ".claude.json"
    plain.mkdir()

    lines, code, _ = _run(Server(), plain, home, wired=wired)

    assert code == 0, "a missing repository is not a failed setup"
    line = next(l for l in lines if l["name"] == "repository")
    assert line["status"] == "UNKNOWN"
    assert "worktree" in line["detail"]


def test_a_real_repository_says_nothing_about_it(tmp_path, wired):
    """The control. A finding that fires everywhere is noise, and noise is how a real one gets
    scrolled past."""
    repo, home = tmp_path / "repo", tmp_path / ".claude.json"
    (repo / ".git").mkdir(parents=True)

    lines, _, _ = _run(Server(), repo, home, wired=wired)

    assert not any(l["name"] == "repository" for l in lines)


# ---- a sandboxed Grok is a sandboxed fleet (GRPH-838) --------------------------------------------

def test_setup_names_a_grok_sandbox_that_every_child_will_inherit(tmp_path, wired, no_grok_config):
    """Measured 2026-09-10: `[sandbox] profile = "workspace"` reached gbfleet and every vendor
    it spawned, and four children died at exit 1 before registering, each with its own
    vendor's error. Setup cannot change the operator's sandbox, but a PASS report that leaves
    the first wave to find this out is the silence this line replaces."""
    repo, home = tmp_path / "repo", tmp_path / ".claude.json"
    repo.mkdir()
    no_grok_config.write_text('[sandbox]\nprofile = "workspace"\n')

    lines, code, _ = _run(Server(), repo, home, wired=wired)

    assert code == 0, "a finding, not a refusal — grok children still spawn"
    line = next(l for l in lines if l["name"] == "sandbox")
    assert line["status"] == "UNKNOWN"
    assert "Not logged in" in line["detail"], "the claude symptom, so it is recognised later"
    assert ".qwen" in line["detail"] and ".cursor" in line["detail"]
    assert "--sandbox off" in line["detail"]
    grok = tomllib.loads(no_grok_config.read_text())
    assert "gbfleet" in grok["mcp_servers"], "the entry is still written"


@pytest.mark.parametrize("body", ["[ui]\n", '[sandbox]\nprofile = "off"\n'])
def test_a_grok_config_without_a_live_sandbox_says_nothing_about_one(
        tmp_path, wired, no_grok_config, body):
    """The control. Off and unset are the same fact, and a line that fires for both would be
    noise on every machine — which is how the real one gets scrolled past."""
    repo, home = tmp_path / "repo", tmp_path / ".claude.json"
    repo.mkdir()
    no_grok_config.write_text(body)

    lines, _, _ = _run(Server(), repo, home, wired=wired)

    assert not any(l["name"] == "sandbox" for l in lines)


# ---- gitignore (the call, not only ensure()) -----------------------------------------------------

def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args],
                   capture_output=True, text=True, check=True)


def _git_init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "-c", "user.email=t@e.invalid", "-c", "user.name=T",
         "commit", "--allow-empty", "-qm", "init")


def _ignored(repo: Path, rel: str) -> bool:
    done = subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", "--", rel],
                          capture_output=True, text=True)
    return done.returncode == 0


def test_setup_gitignores_the_credential_it_writes(tmp_path, wired):
    """THE CALL. Scope project writes `.mcp.json`. If setup.run never asked gitignore
    to run, this is an unignored key and `git add .` ships it."""
    repo, home = tmp_path / "repo", tmp_path / ".claude.json"
    _git_init(repo)

    lines, code, _ = _run(Server(), repo, home, scope="project", wired=wired)

    assert code == 0, lines
    assert (repo / ".mcp.json").exists()
    assert _ignored(repo, ".mcp.json"), ".mcp.json is committable"
    assert _ignored(repo, ".gbfleet-instruction"), "gbfleet seats are committable"
    assert _ignored(repo, ".cursor/mcp.json")
    assert _ignored(repo, ".grok/config.toml")
    assert _ignored(repo, ".swamp/")
    assert _ignored(repo, "graphban-swamp/")
    assert any(l["name"] == "gitignore" and l["status"] == "PASS" for l in lines)


def test_setup_gitignores_seat_paths_on_user_scope_too(tmp_path, wired):
    """User-scope writes no key into the repo, but gbfleet still drops seats into
    worktrees that inherit this gitignore. Skipping the write on user scope is how
    a Cursor seat ships the first time someone runs a wave."""
    repo, home = tmp_path / "repo", tmp_path / ".claude.json"
    _git_init(repo)

    _, code, _ = _run(Server(), repo, home, scope="user", wired=wired)

    assert code == 0
    assert not (repo / ".mcp.json").exists()
    assert _ignored(repo, ".mcp.json")
    assert _ignored(repo, ".gbfleet-instruction")


def test_setup_gitignore_is_idempotent(tmp_path, wired):
    repo, home = tmp_path / "repo", tmp_path / ".claude.json"
    _git_init(repo)
    _run(Server(), repo, home, wired=wired)
    first = (repo / ".gitignore").read_text(encoding="utf-8")

    _run(Server(issued={"gb_sk_new1"}), repo, home, wired=wired)
    second = (repo / ".gitignore").read_text(encoding="utf-8")
    assert first == second
    assert second.count("Graphban — live keys") == 1


def test_an_equivalent_pattern_is_not_duplicated(tmp_path, wired):
    """Asks git, not the text of `.gitignore`. A repo that already ignores `.cursor/`
    does not need `.cursor/mcp.json` written again."""
    repo, home = tmp_path / "repo", tmp_path / ".claude.json"
    _git_init(repo)
    (repo / ".gitignore").write_text(".cursor/\n.mcp.json\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "-c", "user.email=t@e.invalid", "-c", "user.name=T",
         "commit", "-qm", "ignore")

    _run(Server(), repo, home, wired=wired)

    body = (repo / ".gitignore").read_text(encoding="utf-8")
    assert body.count(".mcp.json") == 1
    assert ".cursor/mcp.json" not in body
    assert _ignored(repo, ".cursor/mcp.json")
    assert _ignored(repo, ".gbfleet-instruction")


def test_setup_refuses_to_write_a_key_that_would_not_be_ignored(tmp_path, wired, monkeypatch):
    """Sabotage of the CALL: ensure() runs and the dest is still written even when
    git would not ignore it. The warning this replaced did exactly that."""
    repo, home = tmp_path / "repo", tmp_path / ".claude.json"
    _git_init(repo)
    monkeypatch.setattr(gitignore, "ensure",
                        lambda repo: [{"side": "config", "status": "PASS", "name": "gitignore",
                                       "detail": "pretend", "report": ""}])
    monkeypatch.setattr(gitignore, "ignored", lambda repo, rel: False)

    lines, code, _ = _run(Server(), repo, home, scope="project", wired=wired)

    assert code == 1
    assert not (repo / ".mcp.json").exists(), "wrote a key git would commit"
    assert any("would not be gitignored" in l["detail"] for l in lines)
