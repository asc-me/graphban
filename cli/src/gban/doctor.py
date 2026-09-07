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

import shutil
import subprocess

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
    binary = shutil.which("gbfleet")
    if not binary:
        return [_line("local", UNKNOWN, "gbfleet",
                      "not installed here — `uv pip install graphban-fleet` to check the "
                      "local half")]
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


#: What `gbfleet` reads. `gban` names it once, here, and both paths that launch a supervisor
#: go through this function (GRPH-782).
SUPERVISOR_KEY_ENV = "GBFLEET_API_KEY"


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
