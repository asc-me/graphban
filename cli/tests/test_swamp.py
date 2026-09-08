"""`gban swamp setup` — the gate credential, and where it must not go (GRPH-796).

The load-bearing test in this file is `test_the_gate_key_never_reaches_an_mcp_config`. Every
other failure here is loud; that one is silent. A gate key written where `gban setup` writes
the agent key hands a completion-attesting credential to the agent doing the work — and then
`update_item(status="done")` succeeds, CI is green, the board says the gate held, and it did
not. There is no error to notice.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from gban import setup as setup_mod, swamp as swamp_mod
from gban.client import Refused

URL = "http://gb.invalid"


class Server:
    def __init__(self, *, scopes=("read", "write", "gate"), refuse=None):
        self.scopes, self.refuse, self.minted = list(scopes), refuse, []

    def call(self, method, path, body=None):
        if path == "/api/api-keys":
            if self.refuse is not None:
                raise self.refuse
            self.minted.append(body)
            return {"id": "k1", "plaintext": "gb_sk_gate1"}
        raise AssertionError(f"unexpected {method} {path}")


@pytest.fixture()
def ctx(monkeypatch):
    """`get_context` through the minted key, as the deployment answers it."""
    def _wire(server):
        def call(self, method, path, body=None):
            return {"result": {"content": [{"text": json.dumps({"scopes": server.scopes})}]}}
        monkeypatch.setattr(swamp_mod.Client, "call", call)
        return server
    return _wire


class Swamp:
    """A recorded `swamp`. Answers `--json` the way the real binary does, errors included."""

    def __init__(self, *, vaults=(), keys=(), sources=(), fail=()):
        self.vaults, self.keys, self.sources = list(vaults), list(keys), list(sources)
        self.fail, self.calls, self.stdin, self.cwds = set(fail), [], [], []

    def __call__(self, argv, input=None, capture_output=True, text=True, timeout=None,
                 cwd=None):
        self.cwds.append(cwd)
        verb = " ".join(a for a in argv[1:] if not a.startswith("--"))
        self.calls.append(verb)
        if input is not None:
            self.stdin.append(input)
        for bad in self.fail:
            if verb.startswith(bad):
                return subprocess.CompletedProcess(argv, 1, "", '{"error": "nope"}')
        payload = {}
        # THE SHAPES ARE COPIED FROM THE REAL BINARY, not invented. The first version of this
        # double answered `vaults` and `keys`; `swamp 20260830` answers `results` and
        # `secretKeys`. Both readers failed silently against it and the double agreed with
        # them, because the double and the reader were written from one guess.
        if verb.startswith("vault list-keys"):
            payload = {"vaultName": "secrets", "vaultType": "local_encryption",
                       "secretKeys": list(self.keys), "count": len(self.keys)}
        elif verb.startswith("vault list"):
            payload = {"query": "", "results": [
                {"id": "v1", "name": v, "type": "local_encryption"} for v in self.vaults]}
        elif verb.startswith("extension source list"):
            payload = {"sources": [{"path": s, "expandedPaths": [s], "status": "valid"}
                                   for s in self.sources]}
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")


@pytest.fixture()
def wired(monkeypatch):
    def _wire(fake):
        monkeypatch.setattr(swamp_mod, "find", lambda: "/usr/local/bin/swamp")
        monkeypatch.setattr(swamp_mod.subprocess, "run", fake)
        return fake
    return _wire


def _tree(tmp_path):
    repo, adapter = tmp_path / "repo", tmp_path / "graphban-swamp"
    repo.mkdir()
    adapter.mkdir()
    (repo / ".swamp.yaml").write_text("{}\n")
    return repo, adapter


# ---- the one that is silent when it breaks -------------------------------------------------

def test_the_gate_key_never_reaches_an_mcp_config(tmp_path, wired, ctx, monkeypatch):
    """THE INVARIANT. `gban setup` writes the agent key into ~/.claude.json; a gate key there
    would let the building agent attest its own completion, with nothing to notice."""
    repo, adapter = _tree(tmp_path)
    wired(Swamp(vaults=["secrets"], sources=[str(adapter)]))
    monkeypatch.setattr(setup_mod, "write_entries",
                        lambda *a, **k: pytest.fail("swamp setup wrote an MCP config"))
    home = tmp_path / ".claude.json"
    monkeypatch.setattr(setup_mod, "claude_home", lambda: home)

    lines, code = swamp_mod.setup(ctx(Server()), URL, "core", repo, adapter)

    assert code == 0, lines
    assert not home.exists(), "swamp setup created a harness config"
    assert not (repo / ".mcp.json").exists()


def test_the_secret_travels_on_stdin_not_argv(tmp_path, wired, ctx):
    """`ps` is world-readable, and Swamp's own help says the same: piping via stdin is
    recommended for scripts and CI to avoid exposing secrets in the argument list."""
    repo, adapter = _tree(tmp_path)
    fake = wired(Swamp(vaults=["secrets"], sources=[str(adapter)]))

    swamp_mod.setup(ctx(Server()), URL, "core", repo, adapter)

    assert "gb_sk_gate1" in fake.stdin
    for call in fake.calls:
        assert "gb_sk_" not in call, f"a credential appeared in argv: {call}"


def test_the_key_is_minted_with_write_as_well_as_gate(tmp_path, wired, ctx):
    """`gate` alone mints fine and 403s on the first real attestation, because `attest_ci.py`
    attests through `update_item` — months later, in CI."""
    repo, adapter = _tree(tmp_path)
    wired(Swamp(vaults=["secrets"], sources=[str(adapter)]))
    server = Server()

    swamp_mod.setup(ctx(server), URL, "core", repo, adapter)

    body = server.minted[0]
    assert set(body["scopes"]) == {"read", "write", "gate"}
    assert body["project_id"] == "core", "an unpinned gate key attests everywhere"
    assert body["expires_in_days"] is None


def test_a_key_that_came_back_without_write_fails_here_not_in_ci(tmp_path, wired, ctx):
    repo, adapter = _tree(tmp_path)
    wired(Swamp(vaults=["secrets"], sources=[str(adapter)]))

    lines, code = swamp_mod.setup(ctx(Server(scopes=("read", "gate"))), URL, "core", repo, adapter)

    assert code == 1
    assert any("403 on the first real attestation" in l["detail"] for l in lines)


# ---- refusing to do a person's job ----------------------------------------------------------

def test_a_missing_swamp_is_reported_never_installed(tmp_path, monkeypatch, ctx):
    """The documented install pipes a remote script into a shell. That is not something a
    tool should do on somebody's behalf, however convenient."""
    repo, adapter = _tree(tmp_path)
    monkeypatch.setattr(swamp_mod, "find", lambda: "")
    monkeypatch.setattr(swamp_mod.subprocess, "run",
                        lambda *a, **k: pytest.fail("ran a command with no swamp installed"))

    lines, code = swamp_mod.setup(ctx(Server()), URL, "core", repo, adapter)

    assert code == 0, "a missing swamp is not a failed setup"
    assert any("curl" in l["detail"] and "install.sh" in l["detail"] for l in lines)


def test_an_existing_secret_is_left_alone(tmp_path, wired, ctx):
    """Overwriting would strand a live credential on the server, and the one there may be
    deliberate."""
    repo, adapter = _tree(tmp_path)
    wired(Swamp(vaults=["secrets"], keys=["graphban-api-key"], sources=[str(adapter)]))
    server = Server()

    lines, code = swamp_mod.setup(ctx(server), URL, "core", repo, adapter)

    assert code == 0
    assert server.minted == [], "minted a second gate key over a working one"
    assert any("left alone" in l["detail"] for l in lines)


def test_an_initialised_repo_is_never_reinitialised(tmp_path, wired, ctx):
    """`docs/swamp.md`: do not `repo init --force` a tree that already has a vault."""
    repo, adapter = _tree(tmp_path)
    fake = wired(Swamp(vaults=["secrets"], sources=[str(adapter)]))

    swamp_mod.setup(ctx(Server()), URL, "core", repo, adapter)

    assert not any(c.startswith("repo init") for c in fake.calls)
    assert not any("--force" in c for c in fake.calls)


def test_a_nested_adapter_is_flagged(tmp_path, wired, ctx):
    """The runbook asks for a sibling. A copy inside the tree is one `git add` from being
    committed into a repository whose licence forbids carrying Swamp source."""
    repo, _ = _tree(tmp_path)
    inside = repo / "graphban-swamp"
    inside.mkdir()
    wired(Swamp(vaults=["secrets"], sources=[str(inside)]))

    lines, _ = swamp_mod.setup(ctx(Server()), URL, "core", repo, inside)

    assert any("INSIDE this checkout" in l["detail"] for l in lines)


def test_a_missing_adapter_refuses_before_minting(tmp_path, wired, ctx):
    repo, _ = _tree(tmp_path)
    wired(Swamp(vaults=["secrets"]))
    server = Server()

    lines, code = swamp_mod.setup(ctx(server), URL, "core", repo, tmp_path / "nope")

    assert code == 1
    assert server.minted == [], "minted a key for a wiring it could not finish"


def test_a_key_minted_and_not_stored_says_to_revoke_it(tmp_path, wired, ctx):
    """The one genuinely bad state: live on the server, present in nothing. It must not be
    reported as a generic failure."""
    repo, adapter = _tree(tmp_path)
    wired(Swamp(vaults=["secrets"], sources=[str(adapter)], fail=["vault put"]))

    lines, code = swamp_mod.setup(ctx(Server()), URL, "core", repo, adapter)

    assert code == 1
    said = " ".join(l["detail"] for l in lines)
    assert "revoke it" in said and "Settings" in said


def test_a_refused_mint_stores_nothing(tmp_path, wired, ctx):
    repo, adapter = _tree(tmp_path)
    fake = wired(Swamp(vaults=["secrets"], sources=[str(adapter)]))

    lines, code = swamp_mod.setup(ctx(Server(refuse=Refused(403, "no", ""))), URL, "core",
                                  repo, adapter)

    assert code == 1
    assert not any(c.startswith("vault put") for c in fake.calls)


def test_every_swamp_call_is_scoped_to_the_repository(tmp_path, wired, ctx):
    """By `cwd`, because `--repo-dir` does not exist. Swamp's own error text recommends that
    flag and `swamp 20260830` rejects it on every verb — found by running the real binary,
    which is the only place a tool's advice about its own flags can be checked.

    Scoping matters either way: a caller standing somewhere else must not wire a different
    checkout, silently."""
    repo, adapter = _tree(tmp_path)
    fake = wired(Swamp(vaults=["secrets"], sources=[str(adapter)]))

    swamp_mod.setup(ctx(Server()), URL, "core", repo, adapter)

    assert fake.cwds, "no swamp call was made"
    assert all(c == str(repo) for c in fake.cwds), fake.cwds
    assert not any("--repo-dir" in c for c in fake.calls)


def test_a_pretty_printed_error_is_read_as_one(tmp_path, wired, ctx):
    """Swamp formats its JSON across lines. Reading line by line found nothing and fell back
    to the last line of stderr — which for a formatted object is `}`, an error report that
    looks like a real message."""
    repo, adapter = _tree(tmp_path)

    class Formatted(Swamp):
        def __call__(self, argv, **kw):
            done = super().__call__(argv, **kw)
            if "put" in argv:
                return subprocess.CompletedProcess(
                    argv, 1, "", '{\n  "error": "vault is sealed"\n}\n')
            return done

    wired(Formatted(vaults=["secrets"], sources=[str(adapter)]))
    lines, code = swamp_mod.setup(ctx(Server()), URL, "core", repo, adapter)

    assert code == 1
    said = " ".join(l["detail"] for l in lines)
    assert "vault is sealed" in said, said


def test_every_route_swamp_setup_calls_is_documented(tmp_path, wired, monkeypatch):
    """The check `test_acts.py` makes for the verbs it drives, made here for the one it
    cannot: `swamp` needs a checkout and a binary of its own."""
    repo, adapter = _tree(tmp_path)
    wired(Swamp(vaults=["secrets"], sources=[str(adapter)]))
    reference = (Path(__file__).resolve().parents[2] / "docs" / "api-reference.md"
                 ).read_text(encoding="utf-8")
    seen = []

    class Watching(Server):
        def call(self, method, path, body=None):
            seen.append(path)
            return super().call(method, path, body)

    def context(self, method, path, body=None):
        seen.append(path)
        return {"result": {"content": [{"text": json.dumps({"scopes": list(swamp_mod.GATE_SCOPES)})}]}}

    monkeypatch.setattr(swamp_mod.Client, "call", context)
    swamp_mod.setup(Watching(), URL, "core", repo, adapter)

    assert seen, "swamp setup made no HTTP call"
    for path in seen:
        assert f"`{path}`" in reference, f"{path} is not in docs/api-reference.md"


def test_the_readers_match_what_swamp_actually_returns(tmp_path, wired, ctx):
    """A shape pinned against the recorded output of `swamp 20260830`.

    This exists because the alternative already happened: the readers looked for `vaults` and
    `keys`, the real binary answers `results` and `secretKeys`, and the double was written
    from the same guess as the readers — so the suite was green while `secret_present` could
    only ever return False. That failure mints another live gate credential on every run.
    """
    repo, adapter = _tree(tmp_path)
    wired(Swamp(vaults=["secrets", "other"], keys=["graphban-api-key"], sources=[str(adapter)]))

    assert swamp_mod.vaults(repo) == ["secrets", "other"]
    assert swamp_mod.secret_present(repo) is True
    assert swamp_mod.sources(repo) == [str(adapter)]
