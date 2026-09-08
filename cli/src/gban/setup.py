"""Enable delegation on a project (GRPH-792).

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

**Where the entry is written is a correctness question, not a preference.** Claude Code reads
`~/.claude.json`'s per-project `mcpServers` in preference to a repository's `.mcp.json`, so
writing the repository file while a stale entry shadows it leaves the agent authenticating with
the old key. That failure does not present as a permission problem: the harness reports a JSON
parse error, because it is parsing a 401 body. So the default target is the user file — which
also happens to be the one a credential cannot be committed from.
"""
from __future__ import annotations

import json
import os
import subprocess
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


# ---- the entries ----------------------------------------------------------------------------

def entries(url: str, project: str, key: str, repo: Path) -> dict:
    """The two servers a delegating planner holds: the ledger over HTTP, which arbitrates, and
    the supervisor over stdio, which does not. The supervisor takes the credential through its
    environment rather than its arguments, because `ps` is world-readable."""
    return {
        LEDGER_SERVER: {"type": "http", "url": url.rstrip("/") + "/api/mcp",
                        "headers": {"X-API-Key": key}},
        FLEET_SERVER: {"command": "gbfleet",
                       "args": ["mcp", "--repo", str(repo), "--server", url.rstrip("/"),
                                "--project", project],
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
    missing = {t.get("name") if isinstance(t, dict) else t
               for t in (ctx.get("missing_tiers") or [])}
    out.append(_line("ledger", PASS if "fleet" not in missing else FAIL, "fleet tools",
                     "delegate and mint_enrolment are in the manifest" if "fleet" not in missing
                     else "the fleet tier is missing, so `delegate` is not advertised — an "
                          "agent reads that as the tool not existing"))
    return out


def existing_key(path: Path, scope: str, repo: Path) -> str:
    """The key already configured here, if any. Idempotence rests on this: a second run that
    re-minted would leave a live orphan credential behind on every invocation."""
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    servers = ((blob.get("projects") or {}).get(str(repo.resolve()), {}).get("mcpServers")
               if scope == "user" else blob.get("mcpServers")) or {}
    return ((servers.get(LEDGER_SERVER) or {}).get("headers") or {}).get("X-API-Key", "")


# ---- the act ---------------------------------------------------------------------------------

def run(client: Client, url: str, project: str, repo: Path, *, scope: str = "user",
        install: bool = True, home: Path | None = None) -> tuple[list[dict], int, dict]:
    """Enable delegation, and report what is now true rather than what was attempted."""
    lines: list[dict] = []
    scope, path, why = target(repo, scope, home)
    if why:
        lines.append(_line("config", UNKNOWN, "scope", why))

    if scope == "project" and tracked_by_git(path):
        # REFUSED, not warned. This command's whole job is to put a credential in a file; a
        # version-controlled target turns that into "commit a key", and doing it with a
        # warning attached still does it.
        lines.append(_line("config", FAIL, "target",
                           f"git tracks {path} — writing a key there would commit it. "
                           "Re-run with --scope user, which writes outside the repository"))
        return lines, 1, {}

    key = existing_key(path, scope, repo)
    reused = bool(key)
    if reused:
        checks = verify(url, key, project)
        if all(c["status"] == PASS for c in checks):
            lines.append(_line("config", PASS, "credential",
                               f"already configured in {path} and it works"))
            lines += checks
            lines += skill(repo)
            lines += supervisor(install)
            worst = max((doctor_mod.SEVERITY[l["status"]] for l in lines), default=0)
            return lines, (1 if worst == doctor_mod.SEVERITY[FAIL] else 0), {"reused": True}
        # A key that cannot do the job is replaced, never patched: `tool_tiers` are fixed at
        # mint and there is no route that changes them.
        lines.append(_line("config", UNKNOWN, "credential",
                           "the configured key cannot delegate, so a new one is being minted; "
                           "the old one is still live — remove it in Settings → API keys"))

    try:
        minted = mint(client, project)
    except Refused as exc:
        lines.append(_line("ledger", FAIL, "mint", str(exc)))
        return lines, 1, {}
    key = minted.get("plaintext") or ""
    if not key:
        lines.append(_line("ledger", FAIL, "mint", "the server returned no key"))
        return lines, 1, {}

    write_entries(path, scope, repo, entries(url, project, key, repo))
    lines.append(_line("config", PASS, "written",
                       f"{LEDGER_SERVER} and {FLEET_SERVER} in {path} ({scope} scope)"))
    if scope == "project":
        lines.append(_line("config", UNKNOWN, "secret",
                           f"{path} now holds a credential. Make sure it is gitignored"))
    lines += verify(url, key, project)
    lines += skill(repo)
    lines += supervisor(install)
    worst = max((doctor_mod.SEVERITY[l["status"]] for l in lines), default=0)
    return lines, (1 if worst == doctor_mod.SEVERITY[FAIL] else 0), {"minted": minted.get("id")}


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
