"""`gb doctor` — one answer to "is this set up correctly", across both halves (PRD-40 D7).

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

from gb import config
from gb.client import Client, NoSession, Refused, Unreachable, authenticated

PASS, FAIL, UNKNOWN = "PASS", "FAIL", "UNKNOWN"

#: Worst first. The exit code is the worst finding across BOTH halves, never whichever ran
#: last — a local pass after a ledger failure must not report success.
SEVERITY = {PASS: 0, UNKNOWN: 1, FAIL: 2}


def _line(side: str, status: str, name: str, detail: str = "") -> dict:
    return {"side": side, "status": status, "name": name, "detail": detail}


def ledger(url: str, project: str) -> list[dict]:
    """The half nothing checked. Every line says `ledger`, because a line that does not say
    where it came from sends a reader to the wrong machine."""
    if not url:
        return [_line("ledger", UNKNOWN, "server",
                      f"no server configured; pass --server or run `gb login`")]
    try:
        client = authenticated(url)
    except NoSession as exc:
        return [_line("ledger", FAIL, "session", f"{exc} — run `gb login`")]
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
                         "no project configured; pass --project or set one with `gb login`"))
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
    env = {}
    if api_key:
        # In the ENVIRONMENT, never argv: `ps` shows argv to every process on the machine.
        env["GBFLEET_API_KEY"] = api_key
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=120,
                              env={**_environ(), **env})
    except (OSError, subprocess.SubprocessError) as exc:
        return [_line("local", UNKNOWN, "gbfleet", f"could not run: {exc}")]
    status = PASS if done.returncode == 0 else FAIL
    body = (done.stdout or done.stderr or "").strip()
    return [_line("local", status, "gbfleet", body)]


def _environ() -> dict:
    import os

    return dict(os.environ)


def run(url: str, project: str, api_key: str = "") -> tuple[list[dict], int]:
    lines = ledger(url, project) + local(url, project, api_key)
    worst = max((SEVERITY[l["status"]] for l in lines), default=0)
    # FAIL exits 1, UNKNOWN exits 0: "could not check" is not "broken", and a doctor that
    # failed a script because a laptop lacked `gbfleet` would stop being run.
    return lines, (1 if worst == SEVERITY[FAIL] else 0)


def render(lines: list[dict]) -> str:
    width = max((len(l["name"]) for l in lines), default=0)
    return "\n".join(
        f"{l['status']:<7} {l['side']:<6} {l['name']:<{width}}  {l['detail']}".rstrip()
        for l in lines)
