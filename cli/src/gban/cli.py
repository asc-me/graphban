"""`gban` — the client for a human at a terminal (PRD-40).

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
from pathlib import Path

from gban import config, doctor as doctor_mod, setup as setup_mod
from gban.client import (EXIT_NO_SESSION, EXIT_NO_SUPERVISOR, EXIT_REFUSED, EXIT_UNREACHABLE,
                       Client, NoSession,
                       Refused, Unreachable, authenticated, login)

PROG = "gban"


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

    setup_cmd = sub.add_parser(
        "setup", help="enable delegation on a project",
        description=("Mints a project-scoped credential that does NOT expire, writes the "
                     "graphban and gbfleet MCP entries where the harness will actually read "
                     "them, and installs the supervisor. Only seats expire; a credential that "
                     "died overnight would make 'delegation is set up' quietly stop being "
                     "true. Re-running is safe: a working configuration is left alone."))
    setup_cmd.add_argument(
        "--scope", choices=["user", "project"], default="user",
        help="user (default) writes ~/.claude.json, which OUTRANKS a repo .mcp.json and "
             "cannot be committed; project writes .mcp.json beside the repo")
    setup_cmd.add_argument(
        "--auto", action="store_true",
        help="every project with a repository here: matches this directory, what is in it and "
             "its siblings against your projects BY NAME, and refuses any ambiguity rather "
             "than guessing. A project carries no repository link, so this is a match, not a "
             "lookup")
    setup_cmd.add_argument("--no-install", action="store_true",
                           help="do not install the supervisor")

    fleet = sub.add_parser(
        "fleet", help="hand off to gbfleet (the supervisor)",
        description=("Passes everything through to `gbfleet` and returns its exit code "
                     "unchanged — 75 is stuck, 69 an unreachable model endpoint, 55 a spent "
                     "budget, and folding those into one code would destroy a taxonomy the "
                     "supervisor's own tests pin."),
        add_help=False)
    fleet.add_argument("rest", nargs=argparse.REMAINDER,
                       help="arguments for gbfleet; try `gban fleet --help`")
    sub.add_parser("whoami", help="who this session belongs to, and where it points")

    seats = sub.add_parser(
        "seats", help="the seats a wave was issued, and issuing more",
        description=("A SEAT is an enrolment code: one agent's right to register once, for "
                     "half an hour. It is not a credential — the credential is the API key "
                     "the child authenticates with, and `gban keys` is where those live."))
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
    mint = keys_do.add_parser(
        "mint", help="mint a credential narrowed to a role, or to several",
        description=("Repeat --role to mint a credential an agent can be RE-TASKED within. "
                     "One role is the default and is a real bound: it is what stops a client "
                     "config from registering a worker as a planner. But a role change cannot "
                     "climb past the credential the agent already holds, so an agent minted "
                     "for one role can never be promoted without being restarted."))
    mint.add_argument("--role", required=True, action="append", metavar="ROLE",
                      help="repeatable; the first is the role the agent registers into")
    mint.add_argument("--wave", default="wave-1")
    mint.add_argument("--label", default="")
    return parser


def _out(payload: dict, human: str, as_json: bool) -> None:
    print(json.dumps(payload, indent=1, sort_keys=True) if as_json else human)


def _server(args) -> str:
    url = config.resolve(args.server, config.URL_ENV, "url")
    if not url:
        print(f"{PROG}: no server. Pass --server, set ${config.URL_ENV}, or run `gban login "
              f"--server …` once.", file=sys.stderr)
        raise SystemExit(EXIT_REFUSED)
    return url


def cmd_login(args) -> int:
    url = _server(args)
    # A TERMINAL, OR NOTHING. `getpass` falls back to a plain echoing read when it cannot
    # turn echo off, and warns about it — which is the wrong trade for a password: the
    # warning arrives after the person has already decided to type. Without a tty the
    # password would land in the scrollback, in a transcript, or in whatever captured the
    # session. Refusing is the only safe branch, and it is not a limitation somebody can
    # work around by trying harder.
    if not sys.stdin.isatty():
        print(f"{PROG}: `{PROG} login` needs a terminal. Without one, the prompt cannot turn "
              f"off echo and your password would be written to the scrollback.\n"
              f"     Run it in a shell, not through a pipe, a heredoc or an editor's "
              f"command runner.", file=sys.stderr)
        return EXIT_REFUSED
    try:
        email = args.email or input("email: ").strip()
        # Prompted, never an argument: argv is world-readable in `ps`, and a password in
        # shell history is a credential nobody remembers leaving there.
        password = getpass.getpass("password: ")
    except (EOFError, KeyboardInterrupt):
        # Somebody pressed ctrl-C or the input ended. One line, not a traceback — the
        # same rule criterion 4 applies to an expired session applies to a cancelled login.
        print(f"\n{PROG}: cancelled", file=sys.stderr)
        return EXIT_REFUSED
    pair = login(url, email, password)
    refresh = pair.get("refresh_token") or ""
    if not refresh:
        print(f"{PROG}: the server returned no refresh token", file=sys.stderr)
        return EXIT_REFUSED
    path = config.save_session(refresh, user=email)
    asked = config.resolve(args.project, config.PROJECT_ENV, "project")
    chosen, projects, why = _default_project(url, asked)
    config.save_settings(url=url, project=chosen)
    human = [f"{PROG}: signed in to {url} as {email}",
             f"     session stored at {path} (mode 600)"]
    if chosen:
        human.append(f"     project {chosen}" + (" (the only one you can read)"
                                                 if not asked and len(projects) == 1 else ""))
        human.append(f"     next: `{PROG} setup` enables delegation on it")
    elif len(projects) > 1:
        human.append(f"     {len(projects)} projects — none set as default:")
        human += [f"       {p.get('id', ''):<20} {p.get('name', '')}" for p in projects]
        human.append(f"     pick one: `{PROG} setup --project <id>`")
    elif why:
        # Discovery failed, the login did not. Reporting this as a failed login would send
        # somebody to re-enter a password that was already accepted.
        human.append(f"     WARNING: could not list your projects ({why}).")
        human.append(f"     Name one when you need it: `{PROG} setup --project <id>`")
    else:
        human.append("     you can read no projects yet — create one in the web app first")
    _out({"server": url, "session": str(path), "user": email, "project": chosen,
          "projects": [p.get("id") for p in projects]}, "\n".join(human), args.as_json)
    return 0


def _default_project(url: str, asked: str) -> tuple[str, list[dict], str]:
    """What `login` should store as the default, the projects it saw, and why it saw none.

    ONE project is chosen for you; several are listed and none is chosen. Picking the first of
    several would be a coin toss whose result is invisible until a key lands in the wrong
    project, and the list is short enough to read.

    An explicit `--project` is never overridden — the person naming one has said the thing
    this function exists to guess.
    """
    if asked:
        return asked, [], ""
    try:
        rows = authenticated(url, act="login").call("GET", "/api/projects")
    except (Unreachable, Refused, NoSession) as exc:
        return "", [], str(exc)
    projects = [p for p in (rows if isinstance(rows, list) else []) if isinstance(p, dict)]
    return (projects[0].get("id", "") if len(projects) == 1 else ""), projects, ""


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
                                     "web app, or run `gban logout` again when the network is "
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


def cmd_setup(args) -> int:
    """Everything between a session and a delegating agent, in one act (GRPH-792)."""
    url = _server(args)
    # THE SESSION FIRST, and the order is the point. Resolving the project first told somebody
    # who had never logged in that they had "no project" — true about the wrong thing, with
    # the wrong exit code (1 rather than 3), and it sends a reader hunting for a project id
    # when the remedy is a person at a terminal. Found by running the built wheel rather than
    # by a test, because every test here starts from a stored session.
    client = authenticated(url, act="setup")
    if args.auto:
        return _setup_auto(args, url, client)
    project = config.resolve(args.project, config.PROJECT_ENV, "project")
    if not project:
        print(f"{PROG}: no project. Pass --project, or run `{PROG} login` again — it names the "
              f"one you can read, or lists them when there are several.", file=sys.stderr)
        return EXIT_REFUSED
    lines, code, made = setup_mod.run(client, url, project, Path.cwd(), scope=args.scope,
                                      install=not args.no_install)
    human = [doctor_mod.render(lines)]
    if code == 0:
        human.append(f"\n     delegation is enabled on {project}. An agent can now "
                     f"`delegate(id=…, lane=…, tier=…, seat=true)` then `spawn`.")
        human.append(f"     restart the harness so it reads the new config.")
    _out({"lines": lines, "ok": code == 0, "project": project, **made},
         "\n".join(human), args.as_json)
    return code


def _setup_auto(args, url: str, client) -> int:
    """Every project that has a repository here, each in its own.

    Reports what it did NOT match as loudly as what it did. A sweep that silently skipped a
    project would leave somebody believing delegation is enabled everywhere.
    """
    projects = client.call("GET", "/api/projects")
    projects = [p for p in (projects if isinstance(projects, list) else []) if isinstance(p, dict)]
    found, notes = setup_mod.match(projects, Path.cwd())
    if not found:
        print(doctor_mod.render(notes) if notes else
              f"{PROG}: no projects to match", file=sys.stderr)
        print(f"{PROG}: matched no repository here. `{PROG} setup --project <id>` from inside "
              f"one names it directly.", file=sys.stderr)
        return EXIT_REFUSED
    lines, worst, done = list(notes), 0, {}
    for pid, repo in sorted(found.items()):
        lines.append(doctor_mod._line("match", doctor_mod.PASS, pid, str(repo)))
        got, code, made = setup_mod.run(client, url, pid, repo, scope=args.scope,
                                        install=not args.no_install)
        lines += [{**l, "name": f"{pid}: {l['name']}"} for l in got]
        done[pid], worst = {"repo": str(repo), "ok": code == 0, **made}, max(worst, code)
    human = [doctor_mod.render(lines),
             f"\n     {sum(1 for d in done.values() if d['ok'])} of {len(done)} enabled. "
             f"Restart the harness so it reads the new config."]
    _out({"lines": lines, "ok": worst == 0, "projects": done}, "\n".join(human), args.as_json)
    return worst


def cmd_fleet(args) -> int:
    """A subprocess, never an import (D5).

    Keeps `gban` free of the supervisor's dependencies, keeps the Apache-2.0 boundary intact,
    and leaves `gbfleet --help` authoritative about its own commands.
    """
    binary = doctor_mod.find_supervisor()
    if not binary:
        # At a terminal, offer to fix it here rather than making the person read a command,
        # copy it, run it and type this one again. Declined or unavailable, the message is
        # what it always was.
        if doctor_mod.offer_to_install():
            binary = doctor_mod.find_supervisor()
    if not binary:
        print(f"{PROG}: gbfleet is not installed here. Install it with:\n"
              f"     {doctor_mod.INSTALL_SUPERVISOR}\n"
              f"     …or `brew install asc-me/tap/gban` installs this client only — the "
              f"supervisor is a separate package.", file=sys.stderr)
        return EXIT_NO_SUPERVISOR
    argv = [binary, *[a for a in args.rest if a != "--"]]
    url = config.resolve(args.server, config.URL_ENV, "url")
    if url and "--server" not in argv:
        argv += ["--server", url]
    project = config.resolve(args.project, config.PROJECT_ENV, "project")
    if project and "--project" not in argv:
        argv += ["--project", project]
    # Its exit code, unchanged. `gban` adds nothing and explains nothing: the supervisor's
    # message is the one its own tests pin.
    #
    # The ENVIRONMENT goes through the same function `doctor` uses (GRPH-782). It used not
    # to, so the doctor certified a local half this command could not reproduce.
    return subprocess.run(argv, env=doctor_mod.child_environment(
        os.environ.get(config.API_KEY_ENV, ""))).returncode


def _project(args, act: str) -> str:
    project = config.resolve(args.project, config.PROJECT_ENV, "project")
    if not project:
        print(f"{PROG}: `{PROG} {act}` needs a project. Pass --project, set "
              f"${config.PROJECT_ENV}, or run `gban login --project …` once.", file=sys.stderr)
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
        lines.append("     feed them to `gban fleet up --seats-file`, one per line.")
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
        # WHAT IT COULD BE MOVED TO, next to what it is. Without the ceiling on the row,
        # `agents role` is a coin flip against a bound nothing shows (GRPH-780).
        ceiling = a.get("credential_roles") or []
        human.append(f"     {a.get('key') or a.get('id'):<12} {a.get('active_role', ''):<8} "
                     f"{a.get('state', ''):<10} {a.get('credential') or ''}"
                     + (f"  [{'|'.join(ceiling)}]" if len(ceiling) > 1 else ""))
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
        first, *also = args.role
        out = authenticated(url, act="keys mint").call(
            "POST", "/api/fleet/keys",
            {"project_id": project, "role": first, "also": also, "wave": args.wave,
             "label": args.label})
        ceiling = out.get("roles") or [out.get("role")]
        _out(out, f"{PROG}: {out.get('plaintext')}"
                  f"\n     {out.get('role')} on {out.get('wave')}, shown once, expires "
                  f"{out.get('expires_at')}"
                  + (f"\n     re-taskable within {', '.join(ceiling)}" if len(ceiling) > 1
                     else "\n     one role only: an agent on this key cannot be re-tasked"),
             args.as_json)
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
            "doctor": cmd_doctor, "setup": cmd_setup, "fleet": cmd_fleet, "seats": cmd_seats,
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
