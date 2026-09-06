"""`gb` — the client for a human at a terminal (PRD-40).

PR 1: the package, the session, and the config precedence. `doctor`, `seats` and `agents`
arrive in PR 2 and PR 3; the parser names them now only so `gb --help` is not a lie about
what exists.
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys

from gb import config
from gb.client import (EXIT_NO_SESSION, EXIT_REFUSED, EXIT_UNREACHABLE, Client, NoSession,
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


COMMANDS = {"login": cmd_login, "logout": cmd_logout, "whoami": cmd_whoami}


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
