"""`gb` — the client for a human at a terminal (PRD-40).

The v1 verb list is short on purpose (D11). Every verb here is one HTTP call to one route
that already exists, named one-to-one, because a client that composes several calls into a
new act becomes a second place the rules live.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys

from gb import config, doctor as doctor_mod
from gb.client import (EXIT_NO_SESSION, EXIT_NO_SUPERVISOR, EXIT_REFUSED, EXIT_UNREACHABLE,
                       Client, NoSession,
                       Refused, Unreachable, authenticated, login)

PROG = "gb"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=("A Graphban client for a human at a terminal. The web app, `gbfleet` and "
                     "`graphban` all still exist; this is for the acts that otherwise need a "
                     "browser, and for finding out why something is stuck."),
    )
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="machine-readable output; the human format is never parsed")
    parser.add_argument("--server", default=None,
                        help=f"Graphban base URL (or ${config.URL_ENV}, or {config.SETTINGS_FILE})")
    parser.add_argument("--project", default=None,
                        help=f"project id (or ${config.PROJECT_ENV}, or {config.SETTINGS_FILE})")
    sub = parser.add_subparsers(dest="command", required=True)

    login_cmd = sub.add_parser(
        "login", help="start a session on this machine",
        description=("Exchanges an email and password for a session. The refresh token is "
                     "written to session.json at mode 600; the access token is never stored, "
                     "because it is renewed on every invocation anyway."))
    login_cmd.add_argument("--email", default=None)

    sub.add_parser("logout", help="end the session, here and on the server")
    sub.add_parser(
        "doctor", help="check both halves: the ledger, and the local fleet",
        description=("Everything that can be checked before anything is spawned. The ledger "
                     "half runs here; the local half is `gbfleet doctor`, run as a subprocess. "
                     "Neither half silences the other: what could not be checked prints "
                     "UNKNOWN with its reason, never a pass."))

    fleet = sub.add_parser(
        "fleet", help="hand off to gbfleet (the supervisor)",
        description=("Passes everything through to `gbfleet` and returns its exit code "
                     "unchanged — 75 is stuck, 69 an unreachable model endpoint, 55 a spent "
                     "budget, and folding those into one code would destroy a taxonomy the "
                     "supervisor's own tests pin."),
        add_help=False)
    fleet.add_argument("rest", nargs=argparse.REMAINDER,
                       help="arguments for gbfleet; try `gb fleet --help`")
    sub.add_parser("whoami", help="who this session belongs to, and where it points")

    seats = sub.add_parser(
        "seats", help="the seats a wave was issued, and issuing more",
        description=("A SEAT is an enrolment code: one agent's right to register once, for "
                     "half an hour. It is not a credential — the credential is the API key "
                     "the child authenticates with, and `gb keys` is where those live."))
    seats_do = seats.add_subparsers(dest="act")
    issue = seats_do.add_parser("issue", help="issue seats, one role per agent")
    issue.add_argument("roles", nargs="+", metavar="ROLE",
                       help="one entry per agent, repeats included: worker worker planner")
    issue.add_argument("--wave", default="",
                       help="blank means the next one, computed server-side")
    revoke = seats_do.add_parser("revoke-unused", help="throw away seats nobody redeemed")
    revoke.add_argument("--wave", default="")

    agents = sub.add_parser(
        "agents", help="the roster, and re-tasking one",
        description=("Who is live, what each is doing, and — the reason this verb exists — "
                     "what any of them was last refused and why."))
    agents_do = agents.add_subparsers(dest="act")
    role = agents_do.add_parser(
        "role", help="re-task a live agent, as the human who owns its credential",
        description=("The credential ceiling still decides. A role the agent's key does not "
                     "permit is refused by the server, and widening a ceiling means minting "
                     "a different credential — keeping those two acts apart is the point of "
                     "having a ceiling. Lands on the agent's next poll."))
    role.add_argument("agent_id")
    role.add_argument("role", metavar="ROLE")
    role.add_argument("--reason", default="", help="recorded on the event, and told to the agent")

    keys = sub.add_parser("keys", help="the credentials this project's agents authenticate with")
    keys_do = keys.add_subparsers(dest="act")
    mint = keys_do.add_parser("mint", help="mint a credential narrowed to one role")
    mint.add_argument("--role", required=True)
    mint.add_argument("--wave", default="wave-1")
    mint.add_argument("--label", default="")
    return parser


def _out(payload: dict, human: str, as_json: bool) -> None:
    print(json.dumps(payload, indent=1, sort_keys=True) if as_json else human)


def _server(args) -> str:
    url = config.resolve(args.server, config.URL_ENV, "url")
    if not url:
        print(f"{PROG}: no server. Pass --server, set ${config.URL_ENV}, or run `gb login "
              f"--server …` once.", file=sys.stderr)
        raise SystemExit(EXIT_REFUSED)
    return url


def cmd_login(args) -> int:
    url = _server(args)
    email = args.email or input("email: ").strip()
    # Prompted, never an argument: argv is world-readable in `ps`, and a password in shell
    # history is a credential nobody remembers leaving there.
    password = getpass.getpass("password: ")
    pair = login(url, email, password)
    refresh = pair.get("refresh_token") or ""
    if not refresh:
        print(f"{PROG}: the server returned no refresh token", file=sys.stderr)
        return EXIT_REFUSED
    config.save_settings(url=url, project=config.resolve(args.project, config.PROJECT_ENV,
                                                         "project"))
    path = config.save_session(refresh, user=email)
    _out({"server": url, "session": str(path), "user": email},
         f"{PROG}: signed in to {url} as {email}\n     session stored at {path} (mode 600)",
         args.as_json)
    return 0


def cmd_logout(args) -> int:
    url = _server(args)
    told_server, why = False, ""
    try:
        authenticated(url).call("POST", "/api/auth/logout")
        told_server = True
    except NoSession:
        # Already dead server-side. Not an error: the person asked to be logged out and they
        # are, which is the outcome they wanted.
        told_server, why = True, "the session was already ended"
    except (Unreachable, Refused) as exc:
        why = str(exc)
    removed = config.clear_session()
    # The file goes EITHER WAY. Leaving it because the server could not be told would leave a
    # live token on disk belonging to somebody who believes they logged out, which is the one
    # outcome worth avoiding here.
    human = f"{PROG}: signed out" + ("" if told_server else
                                     f"\n     WARNING: the server was not told ({why}).\n"
                                     "     The local session is gone; the server-side one "
                                     "stays valid until it expires.\n     Revoke it from the "
                                     "web app, or run `gb logout` again when the network is "
                                     "back.")
    if not removed and told_server:
        human = f"{PROG}: no local session to remove"
    _out({"signed_out": True, "server_told": told_server, "local_removed": removed,
          "reason": why}, human, args.as_json)
    return 0


def cmd_whoami(args) -> int:
    url = _server(args)
    me = authenticated(url).call("GET", "/api/auth/me")
    project = config.resolve(args.project, config.PROJECT_ENV, "project")
    _out({"server": url, "project": project, **me},
         f"{PROG}: {me.get('email') or me.get('id')} at {url}"
         + (f"\n     project {project}" if project else "\n     no default project set"),
         args.as_json)
    return 0


def cmd_doctor(args) -> int:
    url = config.resolve(args.server, config.URL_ENV, "url")
    project = config.resolve(args.project, config.PROJECT_ENV, "project")
    api_key = os.environ.get(config.API_KEY_ENV, "")
    lines, code = doctor_mod.run(url, project, api_key)
    _out({"lines": lines, "ok": code == 0}, doctor_mod.render(lines), args.as_json)
    return code


def cmd_fleet(args) -> int:
    """A subprocess, never an import (D5).

    Keeps `gb` free of the supervisor's dependencies, keeps the Apache-2.0 boundary intact,
    and leaves `gbfleet --help` authoritative about its own commands.
    """
    binary = shutil.which("gbfleet")
    if not binary:
        print(f"{PROG}: gbfleet is not installed here. `uv pip install graphban-fleet`, or run "
              f"it from the repository's fleet/ directory.", file=sys.stderr)
        return EXIT_NO_SUPERVISOR
    argv = [binary, *[a for a in args.rest if a != "--"]]
    url = config.resolve(args.server, config.URL_ENV, "url")
    if url and "--server" not in argv:
        argv += ["--server", url]
    project = config.resolve(args.project, config.PROJECT_ENV, "project")
    if project and "--project" not in argv:
        argv += ["--project", project]
    # Its exit code, unchanged. `gb` adds nothing and explains nothing: the supervisor's
    # message is the one its own tests pin.
    return subprocess.run(argv).returncode


def _project(args, act: str) -> str:
    project = config.resolve(args.project, config.PROJECT_ENV, "project")
    if not project:
        print(f"{PROG}: `{PROG} {act}` needs a project. Pass --project, set "
              f"${config.PROJECT_ENV}, or run `gb login --project …` once.", file=sys.stderr)
        raise SystemExit(EXIT_REFUSED)
    return project


def _fleet_read(args, act: str) -> tuple[dict, str]:
    """The one read behind `seats`, `agents` and `keys` (D11).

    `GET /api/fleet` already returns the roster, the seats and the credentials together,
    because they are read together. Three verbs over one route is not three routes.
    """
    url, project = _server(args), _project(args, act)
    client = authenticated(url, act=act)
    return client.call("GET", f"/api/fleet?project_id={project}"), project


def cmd_seats(args) -> int:
    if args.act == "issue":
        url, project = _server(args), _project(args, "seats issue")
        out = authenticated(url, act="seats issue").call(
            "POST", "/api/fleet/seats",
            {"project_id": project, "roles": args.roles, "wave": args.wave})
        # The codes are returned ONCE, by the server, and nothing here stores them. A CLI
        # that helpfully wrote them to a file would be inventing a second credential at rest
        # that no route and no test knows about.
        lines = [f"{PROG}: {len(out.get('seats', []))} seats on {out.get('wave')}",
                 "     each code is shown once and is not stored anywhere:"]
        lines += [f"     {s['code']}  {s['role']}" for s in out.get("seats", [])]
        lines.append("     feed them to `gb fleet up --seats-file`, one per line.")
        _out(out, "\n".join(lines), args.as_json)
        return 0
    if args.act == "revoke-unused":
        url, project = _server(args), _project(args, "seats revoke-unused")
        out = authenticated(url, act="seats revoke-unused").call(
            "POST", "/api/fleet/seats/revoke-unused",
            {"project_id": project, "wave": args.wave or None})
        _out(out, f"{PROG}: {out.get('revoked', 0)} unused seats revoked"
                  f"\n     consumed seats are untouched: they record which agent took what.",
             args.as_json)
        return 0
    fleet, _ = _fleet_read(args, "seats")
    seats = fleet.get("seats", [])
    human = [f"{PROG}: {len(seats)} seats"] + [
        f"     {s.get('wave', ''):<10} {s.get('role', ''):<8} {s.get('state', '')}"
        + (f"  taken by {s['consumed_by']}" if s.get("consumed_by") else "")
        for s in seats] or [f"{PROG}: no seats"]
    _out({"seats": seats}, "\n".join(human), args.as_json)
    return 0


def cmd_agents(args) -> int:
    if args.act == "role":
        url = _server(args)
        # No project in the path: the agent id already names one, and the server resolves it.
        # Asking for a project here would let a person name one the agent is not on.
        out = authenticated(url, act="agents role").call(
            "PUT", f"/api/fleet/agents/{args.agent_id}/role",
            {"role": args.role, "reason": args.reason})
        _out(out, f"{PROG}: {out.get('agent_id')} is now {out.get('active_role')}"
                  f"\n     {out.get("takes_effect") or "on the agent's next poll"}", args.as_json)
        return 0
    fleet, _ = _fleet_read(args, "agents")
    agents = fleet.get("agents", [])
    human = [f"{PROG}: {len(agents)} agents"]
    for a in agents:
        human.append(f"     {a.get('key') or a.get('id'):<12} {a.get('active_role', ''):<8} "
                     f"{a.get('state', ''):<10} {a.get('credential') or ''}")
        # THE LINE THIS VERB EXISTS FOR (criterion 8). A roster that says "idle worker" for an
        # agent being refused every call it makes is the thing that cost an afternoon and a
        # database query on Super-Arc.
        refusal = a.get("last_refusal") or {}
        if refusal.get("tool"):
            human.append(f"       last refused {refusal['tool']} x{refusal.get('count', 1)}: "
                         f"{refusal.get('reason', '')}")
    _out({"agents": agents}, "\n".join(human) if agents else f"{PROG}: no agents", args.as_json)
    return 0


def cmd_keys(args) -> int:
    if args.act == "mint":
        url, project = _server(args), _project(args, "keys mint")
        out = authenticated(url, act="keys mint").call(
            "POST", "/api/fleet/keys",
            {"project_id": project, "role": args.role, "wave": args.wave, "label": args.label})
        _out(out, f"{PROG}: {out.get('plaintext')}"
                  f"\n     {out.get('role')} on {out.get('wave')}, shown once, expires "
                  f"{out.get('expires_at')}", args.as_json)
        return 0
    fleet, _ = _fleet_read(args, "keys")
    creds = fleet.get("credentials", [])
    human = [f"{PROG}: {len(creds)} credentials"] + [
        f"     {c.get('prefix', ''):<14} {c.get('name', ''):<20} {c.get('wave') or '-':<10}"
        + ("  REVOKED" if c.get("revoked") else "")
        + ("  all-in-one" if c.get("posture") == "single" else "")
        for c in creds]
    _out({"credentials": creds}, "\n".join(human) if creds else f"{PROG}: no credentials",
         args.as_json)
    return 0


COMMANDS = {"login": cmd_login, "logout": cmd_logout, "whoami": cmd_whoami,
            "doctor": cmd_doctor, "fleet": cmd_fleet, "seats": cmd_seats,
            "agents": cmd_agents, "keys": cmd_keys}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except NoSession as exc:
        # Never a 401 traceback: the person needs one instruction, and this is it.
        print(f"{PROG}: {exc.advice(PROG)}", file=sys.stderr)
        return EXIT_NO_SESSION
    except Unreachable as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return EXIT_UNREACHABLE
    except Refused as exc:
        # The server's own words, unedited (D8). Its `hint` is already the machine-readable
        # next step, and re-wording it here would be a second definition of the rule.
        print(f"{PROG}: {exc.detail}", file=sys.stderr)
        if exc.hint:
            print(f"     {exc.hint}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
