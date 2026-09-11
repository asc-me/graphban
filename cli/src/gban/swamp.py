"""Wire a checkout to Swamp's Graphban adapter (GRPH-796).

`docs/swamp.md` §3 and §5 are five commands and one credential, and the credential is the
part worth automating carefully rather than the commands.

**The gate key is not the agent key, and this is the whole reason to be careful.** `gban
setup` mints an agent credential and writes it into the harness MCP config, where the agent
doing the work reads it. A gate key attests completion: `update_item(status="done")` refuses
without one. Put a gate key where the building agent can read it and the completion gate stops
being a gate — an agent would attest its own work, and nothing would error. So this mints a
SECOND, separate credential and puts it in exactly one place: the Swamp vault. It never writes
to an MCP config, and `test_swamp.py` asserts that rather than trusting the code to stay that
way.

**It does not install Swamp.** The documented install is `curl -fsSL … | sh`, and running a
remote script is categorically different from `uv tool install graphban-fleet`, which names a
package in a package manager. A missing `swamp` is reported with the command to run, for a
person to run.

**It shells out; it does not wrap.** Same shape as `gban fleet` → `gbfleet`, and here it is
also a licensing boundary: Swamp is AGPL-3.0 with an extension exception that forbids copies
of its source. Calling a binary is not copying one.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from gban import gitignore
from gban.client import Client, Refused, Unreachable
from gban.doctor import FAIL, PASS, UNKNOWN, _line

#: What `docs/swamp.md` §5 names. Changing either renames somebody's existing wiring, so they
#: are constants rather than options.
VAULT = "secrets"
SECRET_KEY = "graphban-api-key"
VAULT_TYPE = "local_encryption"

#: The adapter repository, a SIBLING of the checkout — `docs/swamp.md` says "sibling, not this
#: tree", because a nested copy is one `git add` from being committed into a repository whose
#: licence forbids carrying it.
DEFAULT_ADAPTER = "../graphban-swamp"

#: Reported, never run. See the module docstring.
INSTALL = "curl -fsSL https://swamp-club.com/install.sh | sh    # review it first"

#: `read` and `write` ride along with `gate` deliberately: `attest_ci.py` attests through
#: `update_item`, which `mcp_server` refuses without `write`, so a `gate`-only key mints
#: successfully and 403s on the first real attestation. `fleet.mint` carries all three for the
#: same reason.
GATE_SCOPES = ["read", "write", "gate"]


def find() -> str:
    return shutil.which("swamp") or ""


def run(repo: Path, *args: str, stdin: str | None = None,
        timeout: float = 120.0) -> subprocess.CompletedProcess:
    """One swamp invocation, always `--json`, always scoped to `repo` by WORKING DIRECTORY.

    Not `--repo-dir`. Swamp's own "Not a swamp repository" error recommends that flag —
    "specify an existing repository with `swamp <command> --repo-dir /path/to/repo`" — and
    `swamp 20260830` rejects it on every verb tried, `repo init`, `vault list` and
    `extension source list` alike: *Unknown option "--repo-dir"*. Taking a tool's advice about
    its own flags is exactly the kind of assumption that survives a test suite and dies on
    contact, which is what happened here.

    `cwd` is what swamp actually uses to find a repository, so it is what scopes this. The
    caller's own directory is never relied on.
    """
    return subprocess.run(["swamp", *args, "--json"], input=stdin, cwd=str(repo),
                          capture_output=True, text=True, timeout=timeout)


def spoke(done: subprocess.CompletedProcess) -> dict:
    """Swamp answers in JSON on both paths, including its errors — PRETTY-PRINTED across
    several lines, which is why this parses whole streams rather than lines.

    Reading line by line found nothing and left the caller falling back to "the last line of
    stderr", which for a formatted object is `}`. An error report of `}` is worse than none:
    it looks like a real message.
    """
    for stream in (done.stdout, done.stderr):
        text = (stream or "").strip()
        if not text:
            continue
        try:
            got = json.loads(text)
        except ValueError:
            start = text.find("{")
            try:
                got = json.loads(text[start:]) if start >= 0 else None
            except ValueError:
                got = None
        if isinstance(got, dict):
            return got
    return {}


def initialised(repo: Path) -> bool:
    """A `.swamp.yaml` is the repository. Asked of the FILE rather than by running a command,
    so a swamp that is missing or broken does not read as an uninitialised checkout."""
    return (repo / ".swamp.yaml").exists()


# The response keys below are what `swamp 20260830` ACTUALLY returns, captured by running it.
# The first draft guessed `vaults` and `keys`; the real names are `results` and `secretKeys`,
# and the guesses failed silently in the worst direction — `secret_present` would have returned
# False forever, so every run would mint another live gate credential and overwrite the vault
# entry. The test double agreed, because it was built from the same guess.

def vaults(repo: Path) -> list[str]:
    got = spoke(run(repo, "vault", "list"))
    rows = got.get("results") if isinstance(got, dict) else None
    if not isinstance(rows, list):
        return []
    return [str(r.get("name") or "") for r in rows if isinstance(r, dict)]


def secret_present(repo: Path) -> bool:
    """`list-keys` names the keys and NOT their values, which is why idempotence can be
    decided without reading a credential into this process at all."""
    got = spoke(run(repo, "vault", "list-keys", VAULT))
    keys = got.get("secretKeys") if isinstance(got, dict) else None
    if isinstance(keys, list):
        return any(SECRET_KEY == (k if isinstance(k, str) else str((k or {}).get("key", "")))
                   for k in keys)
    return False


def sources(repo: Path) -> list[str]:
    got = spoke(run(repo, "extension", "source", "list"))
    rows = got.get("sources") if isinstance(got, dict) else None
    if not isinstance(rows, list):
        return []
    return [str(r.get("path") or r) if isinstance(r, dict) else str(r) for r in rows]


def mint_gate(client: Client, project: str) -> dict:
    """A gate credential, pinned to one project because the server insists and because the
    insistence is right: `key_gate_ids` falls back to every writable project when
    `project_id` is null, so one leaked CI secret would attest completions across all of them.

    No expiry, for the same reason `gban setup`'s key has none — a credential that dies
    overnight makes "CI can attest" quietly stop being true, and the failure surfaces as a
    completion refusal nobody connects to a key.
    """
    return client.call("POST", "/api/api-keys",
                       {"name": f"{project} swamp gate", "project_id": project,
                        "expires_in_days": None, "scopes": GATE_SCOPES})


def verify(url: str, key: str, project: str) -> list[dict]:
    """Ask the credential what it is, rather than trusting the mint.

    A gate key that cannot WRITE is the specific dead key this checks for: `attest_ci.py`
    attests through `update_item`, so `gate` alone mints fine and 403s on the first real
    attestation — months later, in CI.
    """
    try:
        got = Client(url, api_key=key).call(
            "POST", "/api/mcp",
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "get_context", "arguments": {"project_id": project}}})
        ctx = json.loads(got["result"]["content"][0]["text"])
    except (Unreachable, Refused) as exc:
        return [_line("ledger", FAIL, "gate key", f"minted, but unusable: {exc}")]
    except (KeyError, IndexError, TypeError, ValueError):
        return [_line("ledger", UNKNOWN, "gate key",
                      "the server answered get_context in a shape this version cannot read")]
    scopes = set(ctx.get("scopes") or [])
    missing = [s for s in ("write", "gate") if s not in scopes]
    if missing:
        return [_line("ledger", FAIL, "gate key",
                      f"minted without {', '.join(missing)} — it would 403 on the first real "
                      "attestation rather than here")]
    return [_line("ledger", PASS, "gate key",
                  f"read+write+gate on {project}, and it is not the agent's key")]


def setup(client: Client, url: str, project: str, repo: Path, adapter: Path,
          *, init: bool = True) -> tuple[list[dict], int]:
    """Wire this checkout. Every step is skipped when already done and says which.

    Nothing here writes an MCP config. The gate key goes into the vault and nowhere else.
    """
    lines: list[dict] = []
    if not find():
        return [_line("local", UNKNOWN, "swamp",
                      f"not installed. Install it yourself — this does not run remote "
                      f"scripts:\n{INSTALL}")], 0

    if not initialised(repo):
        if not init:
            return lines + [_line("local", FAIL, "repository",
                                  f"{repo} has no .swamp.yaml and --no-init was given")], 1
        done = run(repo, "repo", "init", "--tool", "none")
        if done.returncode != 0:
            return lines + [_line("local", FAIL, "repository",
                                  f"swamp repo init failed: {_why(done)}")], 1
        lines.append(_line("local", PASS, "repository", f"initialised {repo/'.swamp.yaml'}"))
    else:
        # NEVER --force. `docs/swamp.md`: do not re-init a tree that already has a vault.
        lines.append(_line("local", PASS, "repository", ".swamp.yaml already here"))
    # `.swamp/` is the vault (ciphertext) and `graphban-swamp/` a nested adapter clone.
    # docs/swamp.md says commit `.swamp.yaml` and neither of those; setup writes the
    # ignore lines so a later `git add` cannot. Idempotent if `gban setup` already did.
    lines += gitignore.ensure(repo)

    if not adapter.exists():
        lines.append(_line("local", FAIL, "adapter",
                           f"{adapter} does not exist — clone asc-me/graphban-swamp beside "
                           "this checkout, or pass --adapter"))
        return lines, 1
    if adapter.resolve().is_relative_to(repo.resolve()):
        # The runbook says "sibling, not this tree", and the reason is a licence: a nested
        # copy is one `git add` from being committed into a repository that may not carry it.
        lines.append(_line("local", UNKNOWN, "adapter",
                           f"{adapter} is INSIDE this checkout; the runbook asks for a "
                           "sibling, because a nested copy can be committed by accident"))
    known = sources(repo)
    if any(Path(s).resolve() == adapter.resolve() for s in known if s):
        lines.append(_line("local", PASS, "adapter", f"{adapter} already a source"))
    else:
        done = run(repo, "extension", "source", "add", str(adapter))
        lines.append(_line("local", PASS if done.returncode == 0 else FAIL, "adapter",
                           str(adapter) if done.returncode == 0
                           else f"source add failed: {_why(done)}"))
        if done.returncode != 0:
            return lines, 1

    if VAULT in vaults(repo):
        lines.append(_line("local", PASS, "vault", f"{VAULT!r} already exists"))
    else:
        done = run(repo, "vault", "create", VAULT_TYPE, VAULT)
        if done.returncode != 0:
            return lines + [_line("local", FAIL, "vault",
                                  f"vault create failed: {_why(done)}")], 1
        lines.append(_line("local", PASS, "vault", f"created {VAULT!r} ({VAULT_TYPE})"))

    if secret_present(repo):
        # Left alone rather than replaced. Overwriting would strand a credential that is
        # still live on the server, and the person may have put a deliberate one there.
        lines.append(_line("local", PASS, "secret",
                           f"{VAULT}/{SECRET_KEY} already set — left alone. Delete it first "
                           "to mint a new one"))
        return lines, _worst(lines)

    try:
        minted = mint_gate(client, project)
    except Refused as exc:
        return lines + [_line("ledger", FAIL, "mint", str(exc))], 1
    key = minted.get("plaintext") or ""
    if not key:
        return lines + [_line("ledger", FAIL, "mint", "the server returned no key")], 1

    # STDIN, never argv. Swamp's own help says so: "Piping via stdin is recommended for
    # scripts and CI to avoid exposing secrets in the process argument list."
    done = run(repo, "vault", "put", VAULT, SECRET_KEY, stdin=key)
    if done.returncode != 0:
        return lines + [_line("local", FAIL, "secret",
                              f"the key was MINTED and not stored ({_why(done)}) — revoke it "
                              "in Settings → API keys, it is live and in nothing")], 1
    lines.append(_line("local", PASS, "secret", f"{VAULT}/{SECRET_KEY} stored from stdin"))
    lines += verify(url, key, project)
    return lines, _worst(lines)


def _why(done: subprocess.CompletedProcess) -> str:
    got = spoke(done)
    if isinstance(got, dict) and got.get("error"):
        return str(got["error"])[:200]
    tail = (done.stderr or done.stdout or "").strip().splitlines()
    return (tail[-1][:200] if tail else f"exit {done.returncode}")


def _worst(lines: list[dict]) -> int:
    from gban.doctor import SEVERITY

    return 1 if max((SEVERITY[l["status"]] for l in lines), default=0) == SEVERITY[FAIL] else 0
