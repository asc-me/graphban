"""Enable delegation on a project (GRPH-792, GRPH-825).

Between "logged in" and "an agent can hand an item to a child" sit three mechanical acts: mint
a credential, put it where the harness will read it, and install the supervisor. None of the
three needs a human. The two that do — authenticating, and deciding to mint at all — are the
authority gate PRD-17 D-e keeps deliberately on the human side, and this module does not move
it: it runs on a session somebody already established at a terminal.

**The credential does not expire, and that is the decision, not an oversight.** `gban keys
mint` posts to `/api/fleet/keys`, which is `mint_fleet_key`, which sets `FLEET_KEY_DAYS = 1`.
That is right for a wave credential and wrong for this: enabling delegation on a project is not
a wave, and a credential that dies overnight turns "delegation is set up" into a thing that
silently stops being true. The seat is the object with a TTL — thirty minutes, single use — and
that is where expiry belongs.

**Where the entry is written is a correctness question, not a preference.** There is more than
one parent harness. Claude Code reads `~/.claude.json`'s per-project `mcpServers` (JSON) in
preference to a repository's `.mcp.json`. Grok reads `~/.grok/config.toml`'s `mcp_servers`
(TOML, snake_case — `mcpServers` parses and loads nothing, GRPH-575). Writing only Claude's
file while a Grok session holds a different key reports success and leaves `delegate`
unadvertised, which the agent reads as the tool not existing (GRPH-825). So setup writes
every file that would actually be read, and verifies a key from those files — not only the
Claude one.

A working credential that is missing `gbfleet` is not left alone. Reuse skips a *mint*, never
a write: a PASS report with no supervisor server is a lie.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from gban import config, doctor as doctor_mod
from gban.client import Client, Refused, Unreachable
from gban.doctor import FAIL, PASS, UNKNOWN, _line

#: What the delegating agent's credential must be able to see. `fleet` carries `delegate` and
#: `mint_enrolment`; `prd` rides along because a planner that may author a spec and cannot see
#: the verbs for it fails by absence, which reads as the tools not existing.
TIERS = ["fleet", "prd"]

#: The MCP server names written into the harness config. `graphban` matches what the Settings
#: install snippets emit, so a machine set up by hand and one set up here agree.
LEDGER_SERVER = "graphban"
FLEET_SERVER = "gbfleet"


# ---- where the config goes ------------------------------------------------------------------

def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_PATH") or (Path.home() / ".claude.json"))


def grok_home() -> Path:
    """Grok's user MCP file. `GROK_CONFIG_PATH` is the test seam, matching `CLAUDE_CONFIG_PATH`.

    Grok itself has no such env; the default path is what it actually reads.
    """
    return Path(os.environ.get("GROK_CONFIG_PATH") or (Path.home() / ".grok" / "config.toml"))


def grok_workspace(repo: Path, grok_config: Path | None = None) -> Path:
    """Where Grok's gbfleet MCP cuts worktrees (GRPH-826).

    Grok's `workspace` sandbox can write `~/.grok/` and the repository, and cannot write a
    sibling of the repo. gbfleet's default (`<repo>-gbfleet`) is that sibling — right for
    Claude, unusable here. This path sits next to Grok's config, outside git.
    """
    return (grok_config or grok_home()).resolve().parent / "gbfleet-wt" / repo.name


def workspace_writable(path: Path) -> tuple[bool, str]:
    """The same probe `gbfleet doctor` uses. Setup must not PASS a dest doctor would FAIL."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".gbfleet-setup"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return True, ""
    except OSError as exc:
        return False, str(exc)


@dataclass(frozen=True)
class Dest:
    """One file a parent harness will actually read."""

    harness: str
    scope: str
    path: Path
    dialect: str  # "json" | "toml"


def shadowing_entry(repo: Path, home: Path | None = None) -> bool:
    """Does the user file already declare our server for this repository?

    If it does, the repository's `.mcp.json` cannot be read however correct it is, so a setup
    that wrote there and reported success would be reporting a fiction.
    """
    path = home or claude_home()
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    entry = (blob.get("projects") or {}).get(str(repo.resolve()), {})
    return LEDGER_SERVER in (entry.get("mcpServers") or {})


def tracked_by_git(path: Path) -> bool:
    """Is this file under version control? A credential written into a tracked file is one
    commit from a public repository, and the fact that it is not committed YET is not a
    property anybody maintains on purpose."""
    try:
        done = subprocess.run(["git", "-C", str(path.parent), "ls-files", "--error-unmatch",
                               str(path)], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def target(repo: Path, scope: str, home: Path | None = None) -> tuple[str, Path, str]:
    """`(scope, path, why)` — where the entries will be written, and the reason.

    `--scope project` is honoured unless the user file already shadows it, in which case the
    write would be invisible and the shadow is repaired instead. That is not overriding the
    flag: the flag asks for a working configuration, and there is only one place to put one.
    """
    user = home or claude_home()
    if scope == "project" and shadowing_entry(repo, home):
        return ("user", user,
                f"{user} already declares {LEDGER_SERVER} for this repository and outranks "
                ".mcp.json, so the project file could not be read")
    if scope == "project":
        return "project", repo / ".mcp.json", ""
    return "user", user, ""


def destinations(repo: Path, scope: str, home: Path, grok: Path) -> tuple[list[Dest], list[dict]]:
    """Every parent-harness file that would actually be read, plus findings about the ones
    that will not.

    Claude is always a destination (created if missing) — that is the original setup. Grok's
    user file is a destination only when it already exists: creating `~/.grok/config.toml` for
    a machine that has never run Grok would be inventing a harness. `--scope project` also
    writes `repo/.grok/config.toml` unless git tracks it, the same refusal as `.mcp.json`.
    """
    lines: list[dict] = []
    dests: list[Dest] = []

    claude_scope, claude_path, why = target(repo, scope, home)
    if why:
        lines.append(_line("config", UNKNOWN, "scope", why))
    if claude_scope == "project" and tracked_by_git(claude_path):
        lines.append(_line("config", FAIL, "target",
                           f"git tracks {claude_path} — writing a key there would commit it. "
                           "Re-run with --scope user, which writes outside the repository"))
    else:
        dests.append(Dest("claude", claude_scope, claude_path, "json"))

    if grok.exists():
        dests.append(Dest("grok", "user", grok, "toml"))

    if scope == "project":
        grok_proj = repo / ".grok" / "config.toml"
        if tracked_by_git(grok_proj):
            lines.append(_line("config", FAIL, "target",
                               f"git tracks {grok_proj} — writing a key there would commit it. "
                               "Re-run with --scope user, which writes ~/.grok/config.toml"))
        elif not any(d.path == grok_proj for d in dests):
            dests.append(Dest("grok", "project", grok_proj, "toml"))
    return dests, lines


# ---- the entries ----------------------------------------------------------------------------

def entries(url: str, project: str, key: str, repo: Path,
            workspace: Path | None = None) -> dict:
    """The two servers a delegating planner holds: the ledger over HTTP, which arbitrates, and
    the supervisor over stdio, which does not. The supervisor takes the credential through its
    environment rather than its arguments, because `ps` is world-readable.

    `workspace` is Grok-only (GRPH-826). Claude can write gbfleet's sibling default; Grok's
    sandbox cannot. Passing it for Claude would move Claude's worktrees for no reason.
    """
    args = ["mcp", "--repo", str(repo), "--server", url.rstrip("/"), "--project", project]
    if workspace is not None:
        args += ["--workspace", str(workspace)]
    return {
        LEDGER_SERVER: {"type": "http", "url": url.rstrip("/") + "/api/mcp",
                        "headers": {"X-API-Key": key}},
        FLEET_SERVER: {"command": "gbfleet",
                       "args": args,
                       "env": {doctor_mod.SUPERVISOR_KEY_ENV: key}},
    }


def write_entries(path: Path, scope: str, repo: Path, servers: dict) -> None:
    """Merge, never replace. These files hold other people's servers and, in the user file's
    case, everything else the harness keeps."""
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        blob = {}
    if not isinstance(blob, dict):
        blob = {}
    if scope == "user":
        projects = blob.setdefault("projects", {})
        entry = projects.setdefault(str(repo.resolve()), {})
        entry.setdefault("mcpServers", {}).update(servers)
    else:
        blob.setdefault("mcpServers", {}).update(servers)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(blob, indent=2, sort_keys=True) + "\n", encoding="utf-8")


_TABLE_HEADER = re.compile(r"^\[([^\[\]]+)\]\s*$")
_BARE_TOML_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_string(value: str) -> str:
    """A TOML basic string. The api key and url arrive from the server; a `"` in either
    would emit a file Grok fails to parse, which reads as having no tools."""
    out = ['"']
    for ch in value:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch < " " or ch == "\x7f":
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _drop_grok_servers(text: str, names: set[str]) -> str:
    """Strip `[mcp_servers.<name>]` and nested subtables, leaving every other table.

    Split on lines that *start* a table. `args = ["mcp", ...]` contains `[` and is not a
    header — walking until the next `[` would stop in the middle of the array and leave garbage.
    """
    out: list[str] = []
    skipping = False
    for line in text.splitlines(keepends=True):
        matched = _TABLE_HEADER.match(line)
        if matched:
            parts = matched.group(1).split(".")
            skipping = (len(parts) >= 2 and parts[0] == "mcp_servers" and parts[1] in names)
        if not skipping:
            out.append(line)
    return "".join(out)


def _render_grok_server(name: str, spec: dict) -> str:
    table = f"mcp_servers.{name}"
    if spec.get("url"):
        lines = [f"[{table}]", f"url = {_toml_string(str(spec['url']))}", "enabled = true"]
        headers = spec.get("headers") or {}
        if headers:
            lines.append("")
            lines.append(f"[{table}.headers]")
            for header, value in headers.items():
                key = header if _BARE_TOML_KEY.match(header) else _toml_string(header)
                lines.append(f"{key} = {_toml_string(str(value))}")
        return "\n".join(lines)
    lines = [f"[{table}]"]
    if spec.get("command"):
        lines.append(f"command = {_toml_string(str(spec['command']))}")
    args = spec.get("args") or []
    if args:
        lines.append("args = [" + ", ".join(_toml_string(str(a)) for a in args) + "]")
    env = spec.get("env") or {}
    if env:
        inner = ", ".join(f"{k} = {_toml_string(str(v))}" for k, v in env.items())
        lines.append(f"env = {{ {inner} }}")
    lines.append("enabled = true")
    return "\n".join(lines)


def write_grok_toml(path: Path, servers: dict) -> None:
    """Merge our two servers into Grok's user/project config without rewriting the rest.

    The file holds UI, hooks, sandbox, and other people's servers. `json.dumps` of a parsed
    blob would drop comments and turn `mcp_servers` into the wrong shape. Replace only the
    tables we own.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    text = _drop_grok_servers(text, set(servers)).rstrip()
    blocks = [_render_grok_server(name, spec) for name, spec in servers.items()]
    body = ("\n\n".join(blocks) + "\n") if not text else (text + "\n\n" + "\n\n".join(blocks) + "\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    os.chmod(path, 0o600)


def write_dest(dest: Dest, repo: Path, servers: dict) -> None:
    if dest.dialect == "toml":
        write_grok_toml(dest.path, servers)
        return
    write_entries(dest.path, dest.scope, repo, servers)


def _key_from_servers(servers: dict) -> str:
    """The minted agent key, from the ledger header first and the supervisor env second.

    Setup writes both. The field walk that reopened GRPH-782 scraped
    `mcp_servers.gbfleet.env.GBFLEET_API_KEY` because that is what `gbfleet` actually
    reads; a lookup that only knows the ledger header would miss a dest that has the
    supervisor entry and not the ledger one.
    """
    header = str(((servers.get(LEDGER_SERVER) or {}).get("headers") or {}).get("X-API-Key") or "")
    if header:
        return header
    env = (servers.get(FLEET_SERVER) or {}).get("env") or {}
    return str(env.get(doctor_mod.SUPERVISOR_KEY_ENV) or "")


def grok_key(path: Path) -> str:
    try:
        blob = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return ""
    return _key_from_servers(blob.get("mcp_servers") or {})


def grok_sandbox_profile(path: Path) -> str:
    """Grok's `[sandbox] profile`, or "" when unset or `off` (GRPH-838).

    Read for what it does to CHILDREN: the profile is applied to the Grok process, `gbfleet
    mcp` is its child, and every vendor spawned from there inherits it. Setup cannot fix that
    — it is the operator's sandbox — but a setup that reports PASS and leaves the first wave
    to die at exit 1 with four different vendor errors is the silence this line replaces.
    """
    try:
        blob = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return ""
    profile = str(((blob.get("sandbox") or {}).get("profile")) or "")
    return "" if profile == "off" else profile


def dest_key(dest: Dest, repo: Path) -> str:
    if dest.dialect == "toml":
        return grok_key(dest.path)
    return existing_key(dest.path, dest.scope, repo)


# ---- the ledger side -------------------------------------------------------------------------

def mint(client: Client, project: str, label: str = "") -> dict:
    """A project-scoped agent credential that can delegate, and does not expire."""
    return client.call("POST", "/api/api-keys",
                       {"name": label or f"{project} delegation",
                        "project_id": project,
                        # NULL, deliberately. See the module docstring: only seats expire.
                        "expires_in_days": None,
                        "tool_tiers": TIERS})


def verify(url: str, key: str, project: str) -> list[dict]:
    """Ask the credential what it can see, rather than assuming the mint did what it said.

    `get_context` is the only call that answers all three questions at once, and each of them
    has failed silently in this product before: a key pinned to the wrong project, a key
    without write, and a manifest missing a tier — which an agent reads as the tool not
    existing rather than as a grant it lacks.
    """
    try:
        got = Client(url, api_key=key).call(
            "POST", "/api/mcp",
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "get_context", "arguments": {"project_id": project}}})
    except (Unreachable, Refused) as exc:
        return [_line("ledger", FAIL, "credential", f"minted, but unusable: {exc}")]
    try:
        ctx = json.loads(got["result"]["content"][0]["text"])
    except (KeyError, IndexError, TypeError, ValueError):
        return [_line("ledger", UNKNOWN, "credential",
                      "the server answered get_context in a shape this version cannot read")]
    out = []
    writable = ctx.get("writable_projects") or []
    out.append(_line("ledger", PASS if project in writable else FAIL, "write",
                     f"{project} is writable" if project in writable else
                     f"the key cannot write {project} — you can read it but not write it, so "
                     "an agent on this key can claim nothing"))
    missing = {_tier_id(t) for t in (ctx.get("missing_tiers") or [])}
    out.append(_line("ledger", PASS if "fleet" not in missing else FAIL, "fleet tools",
                     "delegate and mint_enrolment are in the manifest" if "fleet" not in missing
                     else "the fleet tier is missing, so `delegate` is not advertised — an "
                          "agent reads that as the tool not existing"))
    return out


def _tier_id(t) -> str:
    """Live `get_context` spells it `tier`. A mock that used `name` made verify PASS against
    a key that could not see `delegate` — the Grok case, wearing test clothes (GRPH-825)."""
    if isinstance(t, dict):
        return str(t.get("tier") or t.get("name") or "")
    return str(t)


def existing_key(path: Path, scope: str, repo: Path) -> str:
    """The key already configured here, if any. Idempotence rests on this: a second run that
    re-minted would leave a live orphan credential behind on every invocation."""
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    servers = ((blob.get("projects") or {}).get(str(repo.resolve()), {}).get("mcpServers")
               if scope == "user" else blob.get("mcpServers")) or {}
    return _key_from_servers(servers)


# ---- the act ---------------------------------------------------------------------------------

def run(client: Client, url: str, project: str, repo: Path, *, scope: str = "user",
        install: bool = True, home: Path | None = None,
        grok: Path | None = None) -> tuple[list[dict], int, dict]:
    """Enable delegation, and report what is now true rather than what was attempted."""
    lines: list[dict] = []
    if not is_repo(repo):
        # UNKNOWN rather than a refusal, because only HALF of this needs a repository. The
        # ledger entry works anywhere; the supervisor cuts worktrees, and `--repo` pointed at
        # something git does not know is a spawn that fails later, on the first child, with a
        # git error rather than a setup one. `--auto` already skips non-repositories, so
        # without this the same command answered the same question two ways.
        lines.append(_line("config", UNKNOWN, "repository",
                           f"{repo} is not a git repository — the ledger half is fine, but "
                           "gbfleet cuts a worktree per child and will refuse here"))
    dests, dest_lines = destinations(repo, scope, home or claude_home(), grok or grok_home())
    lines += dest_lines
    if not dests:
        return lines, 1, {}

    seen: list[str] = []
    for dest in dests:
        found = dest_key(dest, repo)
        if found and found not in seen:
            seen.append(found)

    key, reused, minted_id = "", False, None
    for candidate in seen:
        checks = verify(url, candidate, project)
        if all(c["status"] == PASS for c in checks):
            key, reused = candidate, True
            break
    if seen and not reused:
        # A key that cannot do the job is replaced, never patched: `tool_tiers` are fixed at
        # mint and there is no route that changes them.
        lines.append(_line("config", UNKNOWN, "credential",
                           "the configured key cannot delegate, so a new one is being minted; "
                           "the old one is still live — remove it in Settings → API keys"))
    if not key:
        try:
            minted = mint(client, project)
        except Refused as exc:
            lines.append(_line("ledger", FAIL, "mint", str(exc)))
            return lines, 1, {}
        key = minted.get("plaintext") or ""
        minted_id = minted.get("id")
        if not key:
            lines.append(_line("ledger", FAIL, "mint", "the server returned no key"))
            return lines, 1, {}

    # So `gban fleet` can find this key without an env var and without scraping a harness
    # dest. Stored even on reuse: a machine that ran setup before this file existed still
    # gets one the next time setup runs. Never a refresh token — save_supervisor_key
    # refuses anything that is not `gb_sk_…`.
    config.save_supervisor_key(project, key)

    for dest in dests:
        workspace = None
        if dest.harness == "grok":
            workspace = grok_workspace(repo, grok or grok_home())
            if tracked_by_git(workspace):
                lines.append(_line("config", FAIL, "target",
                                   f"git tracks {workspace} — worktrees there would be "
                                   "committed. Re-run with --scope user"))
                continue
            ok, why = workspace_writable(workspace)
            if not ok:
                lines.append(_line("config", FAIL, dest.harness,
                                   f"workspace {workspace} is not writable ({why}). "
                                   "spawn would fail as git worktree add; pass --workspace "
                                   "at a path this harness can write (Grok's sandbox allows "
                                   "~/.grok/ and the repository, not a sibling)"))
                continue
            profile = grok_sandbox_profile(grok or grok_home())
            if profile:
                home = Path.home()
                lines.append(_line("config", UNKNOWN, "sandbox",
                                   f"Grok's [sandbox] profile = {profile!r} is inherited by "
                                   "gbfleet and every child it spawns: a claude child reads "
                                   "'Not logged in' (the Keychain is unreachable from inside), "
                                   "qwen-code and cursor-agent die writing ~/.qwen and "
                                   "~/.cursor unless a custom profile grants them (extends = "
                                   f'"workspace", read_write = ["{home / ".qwen"}", '
                                   f'"{home / ".cursor"}"]), and a grok child runs --sandbox '
                                   "off inside this one. `gbfleet doctor --adapter <vendor>` "
                                   "gives the verdict per vendor (GRPH-838)"))
        servers = entries(url, project, key, repo, workspace=workspace)
        try:
            write_dest(dest, repo, servers)
        except OSError as exc:
            lines.append(_line("config", FAIL, dest.harness,
                               f"could not write {dest.path}: {exc}"))
            continue
        extra = f" workspace {workspace}" if workspace else ""
        lines.append(_line("config", PASS, "written",
                           f"{LEDGER_SERVER} and {FLEET_SERVER} in {dest.path} "
                           f"({dest.harness} {dest.scope}){extra}"))
        if dest.scope == "project":
            lines.append(_line("config", UNKNOWN, "secret",
                               f"{dest.path} now holds a credential. Make sure it is gitignored"))
    if reused:
        paths = ", ".join(str(d.path) for d in dests)
        lines.append(_line("config", PASS, "credential",
                           f"already configured in {paths} and it works"))
    lines += verify(url, key, project)
    lines += skill(repo)
    lines += supervisor(install)
    worst = max((doctor_mod.SEVERITY[l["status"]] for l in lines), default=0)
    extra = {"reused": True} if reused else {"minted": minted_id}
    return lines, (1 if worst == doctor_mod.SEVERITY[FAIL] else 0), extra


def supervisor(install: bool) -> list[dict]:
    """The local half, installed rather than offered.

    Absent is not broken — a laptop that only ever delegates to a hosted harness needs no
    supervisor — so a failure to install is UNKNOWN, never FAIL. What it must never be is
    silent: the reason is carried, because "no supervisor" and "uv is missing" and "installed
    but not on PATH" are three different next actions.
    """
    found = doctor_mod.find_supervisor()
    if found:
        return [_line("local", PASS, "supervisor", found)]
    if not install:
        return [_line("local", UNKNOWN, "supervisor",
                      f"not installed, and --no-install was given — "
                      f"`{doctor_mod.INSTALL_SUPERVISOR}` when you want one")]
    ok, why = doctor_mod.install_supervisor()
    if ok:
        return [_line("local", PASS, "supervisor",
                      f"{doctor_mod.find_supervisor()} (installed just now)")]
    return [_line("local", UNKNOWN, "supervisor",
                  f"{why}. Delegation records fine without it; the supervisor is what runs "
                  "the child on this machine")]


# ---- the skill --------------------------------------------------------------------------------
#
# Shipped as package data and copied, never templated. The path and shape are the product's own:
# `backend/app/services/artifacts.py` maps the `skill` tier to `.claude/skills/{slug}/SKILL.md`,
# `file_additive`, frontmatter with `name` and `description` followed by numbered steps. A skill
# written here in a different shape would be one the inventory scan reports and the learning loop
# cannot reason about.

SKILL_SLUG = "graphban-delegation"


def skill_source() -> Path:
    return Path(__file__).resolve().parent / "skills" / SKILL_SLUG / "SKILL.md"


def install_skill(repo: Path) -> tuple[str, Path]:
    """`(status, path)`. Never overwrites: a person may have edited theirs, and a setup command
    that silently replaced it would take that edit away without saying so."""
    dest = repo / ".claude" / "skills" / SKILL_SLUG / "SKILL.md"
    body = skill_source().read_text(encoding="utf-8")
    if dest.exists():
        return ("same" if dest.read_text(encoding="utf-8") == body else "differs"), dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(body, encoding="utf-8")
    return "written", dest


def skill(repo: Path) -> list[dict]:
    """Teach the agent the two calls, once, in the place its harness looks."""
    try:
        status, dest = install_skill(repo)
    except OSError as exc:
        return [_line("local", UNKNOWN, "skill", f"could not write it: {exc}")]
    if status == "differs":
        return [_line("local", UNKNOWN, "skill",
                      f"{dest} exists and differs from the one shipped here — left alone")]
    return [_line("local", PASS, "skill",
                  f"{dest}" + ("" if status == "written" else " (already current)"))]


# ---- finding the repositories a deployment's projects belong to (GRPH-794) ---------------------
#
# There is no link to follow. A `Project` carries id, tag, name and description and nothing
# about a repository, so this matches on NAME and says so — a guess presented as a lookup would
# be the worse failure, because it would be trusted.

def slug(text: str) -> str:
    """`Super Arc` and `super-arc` and `SUPER_ARC` are one name written three ways."""
    out = [c.lower() if c.isalnum() else "-" for c in (text or "")]
    return "-".join(part for part in "".join(out).split("-") if part)


def names(project: dict) -> set[str]:
    """Every spelling of a project that a directory could plausibly be named after.

    The TAG is deliberately not one of them. It is a two-to-four letter code — `SA`, `GRPH` —
    and matching on it would claim a directory called `sa` for a project called Super Arc on
    the strength of a coincidence. Ids and names are what people name directories after.
    """
    return {slug(str(project.get(k) or "")) for k in ("id", "name")} - {""}


def candidates(here: Path) -> list[Path]:
    """Where to look: this directory, what is in it, and its siblings.

    Both layouts people actually use. Someone inside one repository wants that repository;
    someone in the directory that HOLDS their repositories wants the ones beside it. Nothing
    deeper — a recursive walk of a home directory is slow, surprising, and would start matching
    vendored copies and worktrees.
    """
    seen, out = set(), []
    for path in [here, *sorted(here.iterdir()), *sorted(here.parent.iterdir())]:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen or not path.is_dir():
            continue
        seen.add(resolved)
        out.append(path)
    return out


def is_repo(path: Path) -> bool:
    """`.git` is a directory in a clone and a FILE in a worktree, and a fleet spends its life
    in worktrees — testing for a directory would skip exactly the working copies this tool
    exists to serve."""
    return (path / ".git").exists()


def match(projects: list[dict], here: Path) -> tuple[dict, list[dict]]:
    """`({project_id: repo}, notes)` — what to set up, and everything ambiguous or missing.

    AMBIGUITY IS REFUSED, never broken by a rule. Two directories named for one project, or
    one directory that answers to two projects, both mean the guess would be a coin toss, and
    a credential minted into the wrong repository is not a mistake anybody notices quickly.
    """
    repos = [p for p in candidates(here) if is_repo(p)]
    by_project, notes = {}, []
    claimed: dict[Path, list[str]] = {}
    for project in projects:
        pid = str(project.get("id") or "")
        if not pid:
            continue
        hits = [r for r in repos if slug(r.name) in names(project)]
        if not hits:
            notes.append(_line("match", UNKNOWN, pid, "no directory here is named for it"))
            continue
        if len(hits) > 1:
            notes.append(_line("match", UNKNOWN, pid,
                               "several directories answer to that name, so none was chosen: "
                               + ", ".join(str(h) for h in hits)))
            continue
        by_project[pid] = hits[0]
        claimed.setdefault(hits[0].resolve(), []).append(pid)
    for repo, pids in claimed.items():
        if len(pids) > 1:
            notes.append(_line("match", UNKNOWN, str(repo),
                               "answers to several projects, so none was chosen: "
                               + ", ".join(sorted(pids))))
            for pid in pids:
                by_project.pop(pid, None)
    return by_project, notes


class Unresolved(Exception):
    """The directory does not name a project on this deployment (GRPH-795)."""


def resolve_project(client, here: Path, asked: str = "", stored: str = "") -> tuple[str, str]:
    """Which project this directory belongs to. `(project_id, how)`.

    **The directory decides, and a stored default does not.** `gban login` records a default so
    that reading verbs have one, and using it here would mean "I logged in once inside project
    A" silently mints a credential for A while you are standing in B's repository. A key in the
    wrong project is not a mistake anybody notices quickly, and the fix — pass `--project` —
    costs a person nothing next to the failure it prevents.

    So: an explicit `--project` or `$GRAPHBAN_PROJECT` wins, because naming one says the thing
    this function exists to work out. Otherwise the directory is matched against the projects
    the deployment says you can read, by name. Anything else is refused, including a stored
    default that happens to exist — which is named in the refusal so it does not look ignored.
    """
    if asked:
        return asked, "named"
    rows = client.call("GET", "/api/projects")
    projects = [p for p in (rows if isinstance(rows, list) else []) if isinstance(p, dict)]
    hits = [p for p in projects if slug(here.name) in names(p)]
    if len(hits) == 1:
        return str(hits[0].get("id") or ""), f"matched the directory name {here.name!r}"
    if len(hits) > 1:
        raise Unresolved(
            f"{here.name!r} matches several projects ({', '.join(sorted(str(p.get('id')) for p in hits))}), "
            "so none was chosen — name one with --project")
    known = ", ".join(sorted(str(p.get("id") or "") for p in projects)) or "none"
    raise Unresolved(
        f"unable to resolve a project: no project here is named {slug(here.name)!r} "
        f"(you can read: {known})"
        + (f". A default of {stored!r} is stored from `gban login`, and is deliberately NOT "
           f"used here — this directory is not its repository. Pass --project {stored} if you "
           "mean it" if stored else "")
        + (". Name one with --project" if not stored else ""))
