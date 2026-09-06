"""`gb` — the client for a human at a terminal (PRD-40).

PR 1: the package, the session, and the config precedence. `doctor`, `seats` and `agents`
arrive in PR 2 and PR 3; the parser names them now only so `gb --help` is not a lie about
what exists.
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


COMMANDS = {"login": cmd_login, "logout": cmd_logout, "whoami": cmd_whoami,
            "doctor": cmd_doctor, "fleet": cmd_fleet}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except NoSession:
        # Never a 401 traceback: the person needs one instruction, and this is it.
        print(f"{PROG}: session expired, run `{PROG} login`", file=sys.stderr)
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
