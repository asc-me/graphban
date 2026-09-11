"""`gban doctor` — one answer to "is this set up correctly", across both halves (PRD-40 D7).

Two halves fail in each other's terms. `gbfleet doctor` already checks the local one — repo,
workspace, adapter binary, seats file — and nothing checked the other: whether the credential
is valid, whether the project exists, whether an agent is quarantined or being refused every
call it makes. Diagnosing a stuck worker on the deployed instance needed a database query.

**Neither half may silence the other.** A half that could not be checked prints UNKNOWN with
its reason, never a pass and never a fail — the same three states `gbfleet doctor` uses, for
the same reason: "we could not tell" must never render as "nothing is wrong".
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from gban import config
from gban.client import Client, NoSession, Refused, Unreachable, authenticated

PASS, FAIL, UNKNOWN = "PASS", "FAIL", "UNKNOWN"

#: Worst first. The exit code is the worst finding across BOTH halves, never whichever ran
#: last — a local pass after a ledger failure must not report success.
SEVERITY = {PASS: 0, UNKNOWN: 1, FAIL: 2}


def _line(side: str, status: str, name: str, detail: str = "", report: str = "") -> dict:
    """One finding. `detail` is this line's own reason; `report` is another tool's output,
    kept in its own field so it can never be mistaken for one."""
    return {"side": side, "status": status, "name": name, "detail": detail, "report": report}


def ledger(url: str, project: str) -> list[dict]:
    """The half nothing checked. Every line says `ledger`, because a line that does not say
    where it came from sends a reader to the wrong machine."""
    if not url:
        return [_line("ledger", UNKNOWN, "server",
                      f"no server configured; pass --server or run `gban login`")]
    try:
        client = authenticated(url)
    except NoSession as exc:
        return [_line("ledger", FAIL, "session", f"{exc} — run `gban login`")]
    except Unreachable as exc:
        # Three distinct lines for three distinct failures, because they send a reader to
        # three different places: the network, the credential, or the project.
        return [_line("ledger", UNKNOWN, "server", str(exc))]

    out = [_line("ledger", PASS, "server", url)]
    try:
        me = client.call("GET", "/api/auth/me")
        out.append(_line("ledger", PASS, "session", me.get("email") or me.get("id", "")))
    except Refused as exc:
        return out + [_line("ledger", FAIL, "session", exc.detail)]
    except Unreachable as exc:
        return out + [_line("ledger", UNKNOWN, "session", str(exc))]

    if not project:
        out.append(_line("ledger", UNKNOWN, "project",
                         "no project configured; pass --project or set one with `gban login`"))
        return out
    try:
        fleet = client.call("GET", f"/api/fleet?project_id={project}")
    except Refused as exc:
        out.append(_line("ledger", FAIL, "project",
                         f"{project}: {exc.detail}" + (f" — {exc.hint}" if exc.hint else "")))
        return out
    except Unreachable as exc:
        out.append(_line("ledger", UNKNOWN, "project", str(exc)))
        return out

    agents = fleet.get("agents") or []
    live = [a for a in agents if a.get("state") != "offline"]
    out.append(_line("ledger", PASS, "project",
                     f"{project}: {len(live)} agent(s) online of {len(agents)}"))

    # The two states that are the whole reason a person runs this. Reported per agent, because
    # "one agent is quarantined" and "which one" are different amounts of help.
    for agent in agents:
        if agent.get("state") == "quarantined":
            out.append(_line("ledger", FAIL, f"agent {agent.get('id')}",
                             "quarantined — it kept calling tools it is not permitted"))
        refusal = agent.get("last_refusal") or {}
        if refusal.get("tool"):
            times = f" x{refusal['count']}" if refusal.get("count", 1) > 1 else ""
            out.append(_line("ledger", FAIL, f"agent {agent.get('id')}",
                             f"refused {refusal['tool']}{times} — {refusal.get('reason', '')}"))
    return out


def local(url: str, project: str, api_key: str) -> list[dict]:
    """`gbfleet doctor`, run as a subprocess (D5). Its output is passed through, not parsed:
    the supervisor's own report is the one under test."""
    binary = find_supervisor()
    if not binary:
        return [_line("local", UNKNOWN, "gbfleet",
                      f"not installed here — {INSTALL_SUPERVISOR} to check the local half")]
    argv = [binary, "doctor"]
    if url:
        argv += ["--server", url]
    if project:
        argv += ["--project", project]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=120,
                              env=child_environment(api_key))
    except (OSError, subprocess.SubprocessError) as exc:
        return [_line("local", UNKNOWN, "gbfleet", f"could not run: {exc}")]
    status = PASS if done.returncode == 0 else FAIL
    body = (done.stdout or done.stderr or "").strip()
    # The summary line says what HAPPENED; the child's report goes underneath, verbatim, in
    # its own field. Putting the body in `detail` put the child's banner where the reason
    # belongs — the walk read `FAIL local gbfleet  gbfleet 0.1.0 doctor`, which names a
    # version and explains nothing. Choosing some line of the body to promote instead would
    # be parsing the supervisor's output, which is the thing D5 is careful not to do.
    verdict = "every check passed" if status is PASS else f"exited {done.returncode}"
    return [_line("local", status, "gbfleet",
                  f"`gbfleet doctor` {verdict}; its own report follows", report=body)]


#: The command that actually installs the supervisor. It has been wrong twice: first naming a
#: PyPI package that did not exist, then a git spec that worked but stopped being the right
#: advice the moment `graphban-fleet` was published. A remedy a tool prints is a promise, and
#: `cli/tests/test_packaging.py` now checks this one against the same source the README uses.
INSTALL_SUPERVISOR = "uv tool install graphban-fleet"


def offer_to_install(stream=None) -> bool:
    """Ask, then install `graphban-fleet` with uv. Returns whether it is there afterwards.

    **Asked, never assumed.** Installing software is not a side effect anybody should get
    from `gban fleet ps`: it writes outside this program, it takes a minute, and a person
    who typed a read-only command did not consent to it. So it happens only at an
    interactive prompt, only on an explicit `y`, and only when the wall has actually been
    hit — a missing supervisor, on the command that needs one.

    Nothing is installed into `gban`'s OWN environment. `uv tool install` gives the
    supervisor its own, which is the only correct answer when `gban` may itself live in a
    Homebrew Cellar that brew will replace: a `pip install` next to it would be silently
    undone by the next upgrade.

    Declines, non-interactive shells and a machine without uv all return False and leave the
    caller to print the command — a prompt nobody can answer is a hang, and a hang in a
    script is worse than the error it replaced.
    """
    import sys

    out = stream or sys.stderr
    if not sys.stdin.isatty():
        return False
    if not shutil.which("uv"):
        return False
    print(f"gban: gbfleet is not installed here.", file=out)
    try:
        answer = input(f"       run `{INSTALL_SUPERVISOR}` now? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("", file=out)
        return False
    if answer not in ("y", "yes"):
        return False
    done = subprocess.run(INSTALL_SUPERVISOR.split())
    if done.returncode != 0:
        print(f"gban: that install failed ({done.returncode}); nothing changed here.",
              file=out)
        return False
    return bool(find_supervisor())


def install_supervisor(timeout: float = 600.0) -> tuple[bool, str]:
    """Install it, without asking. `(installed, why_not)`.

    `offer_to_install` prompts because it is reached from `gban fleet`, where a person typed a
    read-only command and did not consent to software being installed. `gban setup` is the
    other case entirely: it mints a credential and rewrites the harness config, and installing
    the supervisor is squarely inside what "enable delegation on this project" asks for. So
    the consent argument is satisfied by the verb rather than by a second prompt — which an
    agent driving this could not answer anyway.

    Still nothing into `gban`'s own environment: `uv tool install` gives the supervisor its
    own, which is the only correct answer when `gban` may live in a Homebrew Cellar that brew
    will replace.
    """
    if not shutil.which("uv"):
        return False, ("uv is not on PATH, so there is nothing to install with — "
                       "`brew install uv`, or see https://docs.astral.sh/uv/")
    try:
        done = subprocess.run(INSTALL_SUPERVISOR.split(), capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"`{INSTALL_SUPERVISOR}` could not run: {exc}"
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip().splitlines()
        return False, (f"`{INSTALL_SUPERVISOR}` failed ({done.returncode})"
                       + (f": {tail[-1][:160]}" if tail else ""))
    if not find_supervisor():
        # It reported success and the binary is not resolvable. Almost always uv's tool bin
        # directory missing from PATH, which is a real state and not a failed install.
        return False, ("installed, but `gbfleet` is not on PATH — run `uv tool update-shell` "
                       "and open a new shell")
    return True, ""


def _sibling_binary(name: str) -> str:
    """An executable beside this interpreter, trying the platform suffix that actually
    exists. On Windows a `pip install` of both packages drops `gbfleet.exe` into
    `Scripts\\`; on Unix it drops `gbfleet` into `bin/`. Checking the bare name on Windows
    is the same hole the Unix sibling check used to have — the file is there and the lookup
    says it is not."""
    import sys

    parent = Path(sys.executable).parent
    for candidate in (parent / name, parent / f"{name}.exe"):
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return ""


def find_supervisor() -> str:
    """`gbfleet`, from beside this interpreter first and only then from PATH.

    **A sibling install is invisible to `PATH` alone**, and that is not a corner case: a
    `graphban-cli[fleet]` extra, or a plain `pip install` of both into one virtualenv, puts
    `gbfleet` in the same `bin/` as `gban` — and `uv tool install` deliberately exposes only
    the requested package's executables, so the supervisor lands there and on no path at all.
    Measured: with the extra installed, `gban fleet` said "gbfleet is not installed here"
    while `gbfleet` sat in the very environment it was running from.

    PATH still wins for a supervisor the operator installed separately and put there on
    purpose — a sibling is a fallback for the case PATH cannot see, not an override of it.
    """
    found = shutil.which("gbfleet")
    if found:
        return found
    return _sibling_binary("gbfleet")


#: What `gbfleet` reads. `gban` names it once, here, and both paths that launch a supervisor
#: go through this function (GRPH-782).
SUPERVISOR_KEY_ENV = "GBFLEET_API_KEY"


def resolve_supervisor_key(project: str, repo: Path | None = None) -> str:
    """The agent key we hand `child_environment` as `api_key`.

    Order (GRPH-782 remaining hole): `$GRAPHBAN_API_KEY`, then the project-scoped key
    `gban setup` stored next to `session.json`, then the key setup already wrote into a
    harness dest for this repository. `$GBFLEET_API_KEY` the caller set is preserved
    inside `child_environment`, so it is not resolved here.

    Never the login session. A refresh token is a different kind of credential.
    """
    env_key = (os.environ.get(config.API_KEY_ENV) or "").strip()
    if env_key:
        return env_key
    stored = config.stored_supervisor_key(project)
    if stored:
        return stored
    return _harness_supervisor_key(repo)


def _harness_supervisor_key(repo: Path | None) -> str:
    """The key `gban setup` already wrote into a parent-harness dest.

    Lazy-imports setup: that module imports this one, and a top-level cycle would
    make `gban doctor` fail to import on a machine that has never run setup.
    """
    from gban import setup as setup_mod

    repo = (repo or Path.cwd()).resolve()
    seen: list[str] = []
    for scope in ("user", "project"):
        dests, _ = setup_mod.destinations(
            repo, scope, setup_mod.claude_home(), setup_mod.grok_home())
        for dest in dests:
            key = (setup_mod.dest_key(dest, repo) or "").strip()
            if key.startswith(config.AGENT_KEY_PREFIX) and key not in seen:
                seen.append(key)
    return seen[0] if seen else ""


def child_environment(api_key: str) -> dict:
    """The environment a `gbfleet` child is launched with, for `doctor` AND for `fleet`.

    **The two used to disagree, and that made the doctor a liar.** `doctor` translated
    `$GRAPHBAN_API_KEY` into `$GBFLEET_API_KEY` for the child; `gban fleet` was a bare
    `subprocess.run` and did not. So on a machine set up the way this tool's own premise
    assumes — one key exported, under `gban`'s name — `gban doctor` reported the local half
    green using a credential it manufactured, and `gban fleet up`, which `gban seats issue`
    sends people to by name, started the supervisor with nothing. D7's promise is
    "everything that can be checked before anything is spawned"; a local verdict that does
    not predict what the next command does is the failure that promise exists to prevent.

    In the ENVIRONMENT, never argv: `ps` shows argv to every process on the machine. And
    never over a value the caller set themselves — somebody who exported `$GBFLEET_API_KEY`
    deliberately, to run the supervisor on a different credential, means it.
    """
    import os

    env = dict(os.environ)
    if api_key and not env.get(SUPERVISOR_KEY_ENV):
        env[SUPERVISOR_KEY_ENV] = api_key
    return env


def run(url: str, project: str, api_key: str = "") -> tuple[list[dict], int]:
    lines = ledger(url, project) + local(url, project, api_key)
    worst = max((SEVERITY[l["status"]] for l in lines), default=0)
    # FAIL exits 1, UNKNOWN exits 0: "could not check" is not "broken", and a doctor that
    # failed a script because a laptop lacked `gbfleet` would stop being run.
    return lines, (1 if worst == SEVERITY[FAIL] else 0)


def render(lines: list[dict]) -> str:
    width = max((len(l["name"]) for l in lines), default=0)
    out = []
    for l in lines:
        out.append(f"{l['status']:<7} {l['side']:<6} {l['name']:<{width}}  {l['detail']}".rstrip())
        # Indented, so a reader scanning the summary column can skip a page of somebody
        # else's report without losing the two lines they came for.
        out += [f"    {row}".rstrip() for row in (l.get("report") or "").splitlines()]
    return "\n".join(out)
